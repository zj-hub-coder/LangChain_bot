"""飞书交互层 —— 卡片占位 / 流式更新 / 最终卡片 / 降级容错。

核心职责：消息去重、卡片渲染、三级降级容错。

与旧版差异：
- 流式：用 langgraph 的 astream_events 获取 token 级流式，替代 Dify run_stream
- 会话：用 SessionManager 维护 user_id -> thread_id，由 langgraph checkpointer 自动管理历史
- 其余卡片操作、去重、降级逻辑保持不变
"""

import json
import logging
import time
from collections import OrderedDict

from lark_oapi.api.im.v1 import (
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from langchain_core.messages import HumanMessage

from agent import build_agent
from config import get_settings
from session_manager import SessionManager

logger = logging.getLogger(__name__)

CARD_TITLE = "🔍 K8s 运维助手"


def _extract_total_tokens(usage_metadata) -> int:
    """从 usage_metadata 中提取总 token 数，缺失时返回 0。"""
    if not usage_metadata:
        return 0
    total = usage_metadata.get("total_tokens")
    if total:
        return int(total)
    # 部分接口不返回 total_tokens，退化为 input + output 求和
    return int(usage_metadata.get("input_tokens", 0) or 0) + int(
        usage_metadata.get("output_tokens", 0) or 0
    )


class LarkBot:
    """飞书机器人交互层：消息去重 + 流式卡片 + 三级降级"""

    def __init__(
        self,
        lark_client,
        sessions: SessionManager,
        update_interval_ms: int = 800,
        dedup_capacity: int = 1000,
    ):
        self._lark_client = lark_client
        self._sessions = sessions
        self._update_interval = update_interval_ms / 1000.0
        self._dedup: OrderedDict[str, bool] = OrderedDict()
        self._dedup_capacity = dedup_capacity
        self._agent = None

    async def _ensure_agent(self):
        """懒加载 agent，避免 import 时就拉起 MCP 子进程。"""
        if self._agent is None:
            self._agent = await build_agent()
        return self._agent

    # ------------------------------------------------------------
    # 对外入口：处理一条用户问题
    # ------------------------------------------------------------
    async def handle_question(
        self, chat_id: str, msg_id: str, user_id: str, question: str
    ) -> None:
        if self._is_duplicate(msg_id):
            logger.info("重复消息，跳过: %s", msg_id)
            return

        thread_id = self._sessions.get_or_create(user_id)

        placeholder_msg_id = self._send_placeholder_card(msg_id)
        if placeholder_msg_id is None:
            logger.warning("占位卡片发送失败，降级为 blocking 模式 user=%s", user_id)
            await self._fallback_blocking(
                msg_id, user_id, question, thread_id
            )
            return

        accumulated_text = ""
        total_tokens = 0
        last_update_time = 0.0
        start = time.time()
        agent = await self._ensure_agent()
        settings = get_settings()

        # 统一 Trace：自动记录流式过程中的 LLM/工具 step 并落盘
        trace, callbacks = build_callbacks(
            source="lark",
            session_id=thread_id,
            question=question,
            user_id=user_id,
            tags=["lark", "streaming"],
            mode="streaming",
        )

        try:
            async for event in agent.astream_events(
                {"messages": [HumanMessage(content=question)]},
                config={
                    "configurable": {"thread_id": thread_id},
                    "callbacks": callbacks,
                },
                version="v2",
            ):
                if event["event"] == "on_chat_model_end":
                    # 从流式结束事件累加 token 用量，无需额外发起一次空消息调用
                    output = event["data"].get("output")
                    total_tokens += _extract_total_tokens(
                        getattr(output, "usage_metadata", None)
                    )
                    continue

                if event["event"] != "on_chat_model_stream":
                    continue

                chunk = event["data"]["chunk"]
                if not hasattr(chunk, "content") or not chunk.content:
                    continue

                accumulated_text += chunk.content
                now = time.time()
                if (now - last_update_time) >= self._update_interval:
                    self._update_card_streaming(
                        placeholder_msg_id, accumulated_text
                    )
                    last_update_time = now

            latency_ms = int((time.time() - start) * 1000)

            if not accumulated_text:
                logger.warning("流式返回为空，降级 blocking user=%s", user_id)
                trace.finish(output="", status="degraded_empty_stream")
                await self._fallback_blocking(
                    msg_id, user_id, question, thread_id,
                    placeholder_msg_id=placeholder_msg_id,
                )
                return

            self._update_card_final(
                placeholder_msg_id, accumulated_text,
                model=settings.llm_model,
                latency_ms=latency_ms,
                total_tokens=total_tokens,
                feedback=self._feedback_payload(trace, thread_id, question),
            )
            trace.finish(output=accumulated_text)
            logger.info(
                "回复完成 user=%s chars=%d latency=%dms tokens=%d",
                user_id, len(accumulated_text), latency_ms, total_tokens,
            )

        except Exception as e:
            logger.exception("流式回复失败 user=%s", user_id)
            if accumulated_text:
                latency_ms = int((time.time() - start) * 1000)
                self._update_card_final(
                    placeholder_msg_id, accumulated_text,
                    model=settings.llm_model,
                    latency_ms=latency_ms,
                    total_tokens=0,
                )
                # 已产出部分内容：标记降级成功而非纯失败
                trace.fail(e, output=accumulated_text)
            else:
                trace.fail(e)
                await self._fallback_blocking(
                    msg_id, user_id, question, thread_id,
                    placeholder_msg_id=placeholder_msg_id, error=e,
                )

    # ------------------------------------------------------------
    # 消息去重
    # ------------------------------------------------------------
    def _is_duplicate(self, msg_id: str) -> bool:
        if not msg_id:
            return False
        if msg_id in self._dedup:
            self._dedup.move_to_end(msg_id)
            return True
        self._dedup[msg_id] = True
        self._dedup.move_to_end(msg_id)
        if len(self._dedup) > self._dedup_capacity:
            self._dedup.popitem(last=False)
        return False

    # ------------------------------------------------------------
    # 降级：blocking 模式
    # ------------------------------------------------------------
    async def _fallback_blocking(
        self, msg_id, user_id, question,
        thread_id: str = "",
        placeholder_msg_id=None, error=None,
    ):
        """降级：blocking 一次性获取完整响应；再失败则文本降级。"""
        trace, callbacks = build_callbacks(
            source="lark",
            session_id=thread_id,
            question=question,
            user_id=user_id,
            tags=["lark", "blocking_fallback"],
            mode="blocking_fallback",
        )
        try:
            agent = await self._ensure_agent()
            start = time.time()
            result = await agent.ainvoke(
                {"messages": [HumanMessage(content=question)]},
                config={
                    "configurable": {"thread_id": thread_id},
                    "callbacks": callbacks,
                },
            )
            latency_ms = int((time.time() - start) * 1000)
            messages = result.get("messages", [])
            answer = messages[-1].content if messages else "（无内容）"
            usage = getattr(messages[-1], "usage_metadata", None) or {} if messages else {}
            total_tokens = usage.get("total_tokens", 0)
            trace.finish(output=answer, status="success_fallback")

            if placeholder_msg_id:
                self._update_card_final(
                    placeholder_msg_id, answer,
                    model=get_settings().llm_model,
                    latency_ms=latency_ms,
                    total_tokens=total_tokens,
                    feedback=self._feedback_payload(trace, thread_id, question),
                )
            else:
                self._reply_card(msg_id, answer, latency_ms, total_tokens,
                                 feedback=self._feedback_payload(trace, thread_id, question))
        except Exception as e:
            logger.exception("blocking 降级也失败")
            trace.fail(e)
            tip = f"⚠️ AI 服务不可用：{str(e)[:200]}"
            if placeholder_msg_id:
                self._update_card_final(
                    placeholder_msg_id, tip,
                    model=get_settings().llm_model,
                    latency_ms=0,
                    total_tokens=0,
                )
            else:
                self._reply_text(msg_id, tip)

    # ------------------------------------------------------------
    # 卡片操作
    # ------------------------------------------------------------
    def _send_placeholder_card(self, reply_msg_id: str) -> str | None:
        """发送"正在思考..."占位卡片，返回卡片的 message_id。"""
        card = self._build_card("⏳ 正在思考中...", is_streaming=True)
        request = (
            ReplyMessageRequest.builder()
            .message_id(reply_msg_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .content(json.dumps(card))
                .msg_type("interactive")
                .build()
            )
            .build()
        )
        response = self._lark_client.im.v1.message.reply(request)
        if not response.success():
            logger.warning(
                "发送占位卡片失败: code=%s msg=%s", response.code, response.msg
            )
            return None
        return response.data.message_id

    def _update_card_streaming(self, message_id: str, content: str) -> None:
        """流式态：浅蓝色标题 + 已收到内容 + 流式注脚。"""
        card = self._build_card(content, is_streaming=True)
        self._patch_card(message_id, card)

    def _update_card_final(
        self, message_id: str, content: str,
        model: str = "", latency_ms: int = 0, total_tokens: int = 0,
    ) -> None:
        """最终态：蓝色标题 + 完整内容 + token 统计注脚。"""
        card = self._build_card(
            content, is_streaming=False,
            model=model, latency_ms=latency_ms, total_tokens=total_tokens,
        )
        self._patch_card(message_id, card)

    def _patch_card(self, message_id: str, card: dict) -> bool:
        """PATCH 更新卡片内容。"""
        request = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(json.dumps(card))
                .build()
            )
            .build()
        )
        response = self._lark_client.im.v1.message.patch(request)
        if not response.success():
            logger.warning(
                "更新卡片失败: code=%s msg=%s", response.code, response.msg
            )
            return False
        return True

    def _reply_card(
        self, reply_msg_id: str, content: str,
        latency_ms: int = 0, total_tokens: int = 0,
    ) -> None:
        """blocking 降级时一次性回复一张最终卡片。"""
        card = self._build_card(
            content, is_streaming=False,
            model=get_settings().llm_model,
            latency_ms=latency_ms, total_tokens=total_tokens,
        )
        request = (
            ReplyMessageRequest.builder()
            .message_id(reply_msg_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .content(json.dumps(card))
                .msg_type("interactive")
                .build()
            )
            .build()
        )
        self._lark_client.im.v1.message.reply(request)

    def _reply_text(self, reply_msg_id: str, text: str) -> None:
        """发送纯文本消息（最终降级方案）。"""
        request = (
            ReplyMessageRequest.builder()
            .message_id(reply_msg_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .content(json.dumps({"text": text}))
                .msg_type("text")
                .build()
            )
            .build()
        )
        self._lark_client.im.v1.message.reply(request)

    # ------------------------------------------------------------
    # 卡片构建
    # ------------------------------------------------------------
    def _build_card(
        self, content: str, is_streaming: bool,
        model: str = "", latency_ms: int = 0, total_tokens: int = 0,
        feedback: dict | None = None,
    ) -> dict:
        """构建飞书交互卡片。

        is_streaming=True  → 流式态（浅蓝色 wathet 标题）
        is_streaming=False → 最终态（蓝色 blue 标题 + 信息来源 + 调用指标注脚）
        feedback 非空时，最终态追加 👍/👎 按钮（点击回流到 feedback 数据集）
        """
        header_color = "wathet" if is_streaming else "blue"
        elements: list[dict] = [
            {"tag": "markdown", "content": content},
            {"tag": "hr"},
        ]
        if is_streaming:
            elements.append({
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": "⏳ 流式输出中..."}],
            })
        else:
            elements.append({
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": "📖 回答基于 K8s 运维知识库"}
                ],
            })
            metrics_note = (
                f"模型: {model} | 耗时: {latency_ms}ms | tokens: {total_tokens}"
            )
            elements.append({
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": metrics_note}],
            })
            if feedback:
                # 反馈回流：value 随按钮点击事件回传，凭 run_id 回连 trace
                elements.append({"tag": "hr"})
                elements.append({
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "👍 回答准确"},
                            "type": "primary",
                            "value": json.dumps(
                                {"action": "feedback", "feedback": "up", **feedback},
                                ensure_ascii=False,
                            ),
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "👎 回答有误"},
                            "type": "danger",
                            "value": json.dumps(
                                {"action": "feedback", "feedback": "down", **feedback},
                                ensure_ascii=False,
                            ),
                        },
                    ],
                })

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": CARD_TITLE},
                "template": header_color,
            },
            "elements": elements,
        }
