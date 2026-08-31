"""ReAct 死循环防护中间件（AgentMiddleware）。

针对 LLM 反复调用同一工具、拿到相同结果却无法收敛的场景，提供两层防护：

1. 最大工具调用轮数上限：超过上限后强制模型停止调用工具，基于已获取的
   信息直接给出结论，避免无限消耗 token 与集群接口压力。
2. 连续重复调用检测：连续两轮以相同参数调用同一工具，判定为死循环提前终止。

轮数与重复检测都直接从 state["messages"] 推导（统计带 tool_calls 的 AIMessage
数量、比较最后两轮的调用签名），不引入额外状态字段，因此天然按 thread_id
隔离，多个并发会话互不干扰。

作为 create_agent 的 middleware 注入，在每次模型调用前拦截：
    request.override(tools=[], messages=...+提示)  → 清空工具让模型只能文本总结
"""
import json
import logging

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, SystemMessage

logger = logging.getLogger(__name__)

_STOP_PROMPT = (
    "{reason}。请停止继续调用工具，基于目前已获取的全部信息直接给出诊断结论与"
    "排查建议；信息不足的部分请如实说明，不要臆造。"
)


class LoopGuardMiddleware(AgentMiddleware):
    """限制工具调用轮数并检测连续重复调用的中间件。"""

    name = "loop_guard"

    def __init__(self, max_tool_rounds: int = 8):
        self.max_tool_rounds = max_tool_rounds

    # ------------------------------------------------------------
    # 判定逻辑（同步纯函数，供 sync/async 共用）
    # ------------------------------------------------------------
    @staticmethod
    def _tool_rounds(messages) -> int:
        """统计已完成的工具调用轮数（每产生一次 tool_calls 的 AIMessage 计一轮）。"""
        return sum(
            1
            for m in messages
            if isinstance(m, AIMessage) and getattr(m, "tool_calls", None)
        )

    @staticmethod
    def _sig(tool_call: dict) -> str:
        name = tool_call.get("name", "")
        args = json.dumps(
            tool_call.get("args", {}), sort_keys=True, ensure_ascii=False
        )
        return f"{name}({args})"

    def _round_sigs(self, messages) -> list[list[str]]:
        """按轮分组返回每轮的工具调用签名（一轮可能并行调用多个工具）。"""
        rounds: list[list[str]] = []
        for m in messages:
            if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
                rounds.append([self._sig(tc) for tc in m.tool_calls])
        return rounds

    def _is_repeating(self, messages) -> bool:
        """连续两轮调用签名完全一致 → 判定为死循环。"""
        rounds = self._round_sigs(messages)
        return len(rounds) >= 2 and rounds[-1] == rounds[-2]

    def _force_stop_request(self, request, reason: str):
        """构造强制总结请求：清空工具并追加一条总结指令。"""
        prompt = SystemMessage(content=_STOP_PROMPT.format(reason=reason))
        return request.override(
            tools=[],
            messages=list(request.messages) + [prompt],
        )

    # ------------------------------------------------------------
    # 同步 / 异步包裹（在模型调用前拦截）
    # ------------------------------------------------------------
    def wrap_model_call(self, request, handler):
        messages = request.messages
        if self._tool_rounds(messages) >= self.max_tool_rounds:
            logger.info("达到最大工具轮数 %d，强制总结", self.max_tool_rounds)
            return handler(self._force_stop_request(request, "已达到最大工具调用轮数"))
        if self._is_repeating(messages):
            logger.info("检测到连续重复调用同一工具，强制总结")
            return handler(self._force_stop_request(request, "检测到连续重复调用同一工具"))
        return handler(request)

    async def awrap_model_call(self, request, handler):
        messages = request.messages
        if self._tool_rounds(messages) >= self.max_tool_rounds:
            logger.info("达到最大工具轮数 %d，强制总结", self.max_tool_rounds)
            return await handler(
                self._force_stop_request(request, "已达到最大工具调用轮数")
            )
        if self._is_repeating(messages):
            logger.info("检测到连续重复调用同一工具，强制总结")
            return await handler(
                self._force_stop_request(request, "检测到连续重复调用同一工具")
            )
        return await handler(request)
