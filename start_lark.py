"""飞书智能问答机器人 —— WebSocket 长连接模式入口。

启动方式：
    python start_lark.py

说明：
- 使用飞书 lark.ws.Client 长连接，无需公网 URL / 内网穿透。
- 消息处理委托给 LarkBot（卡片/流式/三级降级），自身只负责事件分发。
- Agent 能力由 langchain + MCP 工具 + 内置搜索提供，异步处理。
"""

import asyncio
import json
import logging
import threading

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from config import get_settings
from feedback_store import append_feedback
from lark_bot import LarkBot
from session_manager import SessionManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ============================================================
# 1. 初始化配置 & 客户端
# ============================================================
settings = get_settings()
sessions = SessionManager()

lark_client = (
    lark.Client.builder()
    .app_id(settings.lark_app_id)
    .app_secret(settings.lark_app_secret)
    .build()
)

bot = LarkBot(
    lark_client=lark_client,
    sessions=sessions,
    update_interval_ms=settings.lark_card_update_interval_ms,
)


# ============================================================
# 2. 消息事件回调
# ============================================================
def do_p2_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
    """接收飞书消息事件。"""
    msg = data.event.message
    sender = data.event.sender

    if msg.message_type != "text":
        return

    try:
        content = json.loads(msg.content)
        question = content.get("text", "").strip()
    except (json.JSONDecodeError, AttributeError):
        return

    if not question:
        return

    user_id = sender.sender_id.open_id if sender.sender_id else "unknown"
    logger.info("收到消息 from %s: %s", user_id, question[:80])

    # 在独立线程中运行异步处理
    threading.Thread(
        target=_run_async,
        args=(bot.handle_question, msg.chat_id, msg.message_id, user_id, question),
        daemon=True,
    ).start()


def _run_async(coro_func, *args):
    """在新线程中运行 async 函数。"""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro_func(*args))
    finally:
        loop.close()


# ============================================================
# 3. 卡片按钮回调（👍/👎 反馈回流 → feedback/feedback-*.jsonl）
# ============================================================
def do_p2_card_action_trigger(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    """记录用户对回答的反馈，凭 run_id 与 traces 双向回连。"""
    try:
        value = data.event.action.value or {}
        if isinstance(value, str):  # 兼容 SDK 未解析的形态
            value = json.loads(value)
        if value.get("action") != "feedback":
            return P2CardActionTriggerResponse({})

        operator = getattr(data.event, "operator", None)
        append_feedback({
            "feedback": value.get("feedback"),
            "run_id": value.get("run_id"),
            "thread_id": value.get("thread_id"),
            "question": value.get("question"),
            "user_id": getattr(operator, "open_id", None) if operator else None,
            "chat_id": getattr(data.event.context, "open_chat_id", None)
            if getattr(data.event, "context", None) else None,
            "message_id": getattr(data.event.context, "open_message_id", None)
            if getattr(data.event, "context", None) else None,
        })
        is_down = value.get("feedback") == "down"
        toast = (
            "已记录问题反馈，我们会据此改进 🙏"
            if is_down else "感谢认可 👍"
        )
        return P2CardActionTriggerResponse({
            "toast": {"type": "success", "content": toast},
        })
    except Exception:
        logger.exception("处理卡片反馈回调失败")
        return P2CardActionTriggerResponse({
            "toast": {"type": "error", "content": "反馈记录失败，请稍后重试"},
        })


# ============================================================
# 4. 事件处理器 & WebSocket 客户端
# ============================================================
event_handler = (
    lark.EventDispatcherHandler.builder("", "")
    .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
    .register_p2_card_action_trigger(do_p2_card_action_trigger)
    .build()
)

ws_client = lark.ws.Client(
    app_id=settings.lark_app_id,
    app_secret=settings.lark_app_secret,
    event_handler=event_handler,
    log_level=lark.LogLevel.INFO,
)


def main() -> None:
    """启动 WebSocket 长连接。"""
    if not settings.lark_ready:
        logger.error(
            "LARK_APP_ID 或 LARK_APP_SECRET 未设置！请在 .env 中配置"
        )
        return
    if not settings.llm_ready:
        logger.error(
            "LLM 配置不完整！请在 .env 中配置 OPENAI_API_KEY / BASE / MODEL"
        )
        return

    logger.info("Starting Feishu bot (WebSocket mode)...")
    logger.info("App ID: %s", settings.lark_app_id)
    print("✅ 机器人启动成功，等待接收消息...")
    ws_client.start()


if __name__ == "__main__":
    main()
