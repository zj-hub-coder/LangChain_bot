"""统一 Trace 采集层（AgentLoop 第 1 环：数据接入 / 统一 Trace）。

设计原则：
1. 双写：每次运行默认落盘本地 JSONL（离线评估、挖 bad case 的数据底座）；
   配置 LANGFUSE_ENABLED=true 后，同一次运行额外上报自托管 Langfuse（可视化
   三级下钻：全局指标 → 对象（Agent/模型/工具）→ 异常 Step 原始证据）。
2. 无侵入：RunTrace 本身是一个 LangChain BaseCallbackHandler，挂到
   ainvoke / astream_events 的 config["callbacks"] 即可，调用方只需在运行
   前后显式 start/finish，业务代码零感知。
3. 容错：Trace 采集的任何异常都不得影响主对话流程；Langfuse 不可达或未装
   langfuse 包时静默降级为仅本地落盘。

JSONL 每行一条完整 run 记录（traces/traces-YYYYMMDD.jsonl）：
  run 级：输入/输出/状态/总时延/总 token/工具轮数/来源/用户/会话
  step 级：每次 LLM 调用、每次工具调用的耗时、入参、结果摘要、错误
"""
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler

from config import get_settings

logger = logging.getLogger(__name__)

# 工具结果 / 入参写入 trace 前的最大长度（超出截断），防止单条 trace 过大
_MAX_PREVIEW_CHARS = 2000

# 多线程（飞书 WebSocket 每条消息一个线程）并发追加文件时的写锁
_write_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _preview(value: Any) -> Any:
    """把任意值转成适合写入 trace 的紧凑形式，超长截断。"""
    try:
        if isinstance(value, BaseException):
            return f"{type(value).__name__}: {value}"[:_MAX_PREVIEW_CHARS]
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        value = str(value)
    if len(value) > _MAX_PREVIEW_CHARS:
        return value[:_MAX_PREVIEW_CHARS] + f"...[truncated {len(value) - _MAX_PREVIEW_CHARS} chars]"
    return value


def _extract_usage(message_or_result: Any) -> dict:
    """从 ChatModel 响应中提取 token 用量，兼容 usage_metadata / llm_output 两种形态。"""
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    # 新链路：AIMessage.usage_metadata
    meta = getattr(message_or_result, "usage_metadata", None)
    if isinstance(meta, dict):
        usage["input_tokens"] = int(meta.get("input_tokens", 0) or 0)
        usage["output_tokens"] = int(meta.get("output_tokens", 0) or 0)
        usage["total_tokens"] = int(meta.get("total_tokens", 0) or 0)
        return usage
    # ChatResult：generations -> message
    generations = getattr(message_or_result, "generations", None)
    if generations:
        try:
            msg = generations[0][0].message
            meta = getattr(msg, "usage_metadata", None)
            if isinstance(meta, dict):
                usage["input_tokens"] = int(meta.get("input_tokens", 0) or 0)
                usage["output_tokens"] = int(meta.get("output_tokens", 0) or 0)
                usage["total_tokens"] = int(meta.get("total_tokens", 0) or 0)
                return usage
        except Exception:
            pass
    # 旧链路：llm_output.token_usage
    llm_output = getattr(message_or_result, "llm_output", None)
    if isinstance(llm_output, dict):
        token_usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
        usage["input_tokens"] = int(token_usage.get("prompt_tokens", 0) or 0)
        usage["output_tokens"] = int(token_usage.get("completion_tokens", 0) or 0)
        usage["total_tokens"] = int(
            token_usage.get("total_tokens", 0)
            or usage["input_tokens"] + usage["output_tokens"]
        )
    return usage


def _try_build_langfuse_handler(
    user_id: str, session_id: str, tags: list[str]
):
    """配置开启且依赖可用时，返回 Langfuse CallbackHandler；否则返回 None。

    Langfuse 为可选可观测后端（自托管，docker compose 见 deploy/langfuse/）。
    未 pip install langfuse 或未填配置时静默跳过，不影响本地 JSONL 采集。
    """
    s = get_settings()
    if not s.langfuse_enabled:
        return None
    if not (s.langfuse_host and s.langfuse_public_key and s.langfuse_secret_key):
        logger.warning(
            "LANGFUSE_ENABLED=true 但 LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY 未配齐，"
            "本次仅本地落盘 trace"
        )
        return None
    try:
        from langfuse.callback import CallbackHandler
    except ImportError:
        logger.warning(
            "未安装 langfuse 包，本次仅本地落盘 trace；"
            "如需上报请执行: pip install langfuse"
        )
        return None
    try:
        return CallbackHandler(
            host=s.langfuse_host,
            public_key=s.langfuse_public_key,
            secret_key=s.langfuse_secret_key,
            user_id=user_id,
            session_id=session_id,
            tags=tags,
        )
    except Exception as e:  # 构造失败绝不影响主流程
        logger.warning("Langfuse handler 初始化失败，本次仅本地落盘: %s", e)
        return None


class RunTrace(AsyncCallbackHandler):
    """单次 Agent 运行的追踪器，同时作为 LangChain 异步回调挂载。

    用法：
        trace = RunTrace(source="cli", session_id=tid, question=q)
        try:
            resp = await agent.ainvoke(
                {"messages": [HumanMessage(content=q)]},
                config={"configurable": {"thread_id": tid},
                        "callbacks": [trace]},
            )
            trace.finish(output=最后一条消息文本)
        except Exception as e:
            trace.fail(e)
    """

    def __init__(
        self,
        source: str,
        session_id: str = "",
        question: str = "",
        user_id: str = "",
        tags: list[str] | None = None,
        mode: str = "",
    ):
        self.run_id = uuid.uuid4().hex
        self._start_ts = time.time()
        self.started_at = _now_iso()
        self.source = source
        self.session_id = session_id
        self.user_id = user_id or (source if source in {"cli", "eval"} else "unknown")
        self.tags = list(tags or [])
        self.mode = mode  # streaming / blocking_fallback 等子模式标记

        self.data: dict[str, Any] = {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "source": source,
            "mode": mode,
            "user_id": self.user_id,
            "session_id": session_id,
            "tags": self.tags,
            "model": get_settings().llm_model,
            "input": _preview(question),
            "output": "",
            "status": "running",
            "error": None,
            "latency_ms": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "tool_rounds": 0,
            "steps": [],
        }
        # run_id -> step 索引，用于配对 start/end
        self._llm_starts: dict[str, int] = {}
        self._tool_starts: dict[str, int] = {}
        self._langfuse_trace_id: str | None = None

    # ------------------------------------------------------------
    # 显式收口
    # ------------------------------------------------------------
    def finish(self, output: str = "", status: str = "success") -> dict:
        """运行成功收口：汇总指标并落盘，返回完整 trace dict。"""
        self.data["status"] = status
        self.data["output"] = _preview(output)
        self._finalize()
        return self.data

    def fail(self, error: Exception, output: str = "") -> dict:
        """运行异常收口：记录错误并落盘。"""
        self.data["status"] = "error"
        self.data["output"] = _preview(output)
        self.data["error"] = {"type": type(error).__name__, "message": str(error)[:500]}
        self._finalize()
        return self.data

    def _finalize(self) -> None:
        self.data["latency_ms"] = int((time.time() - self._start_ts) * 1000)
        self.data["finished_at"] = _now_iso()
        self._persist()

    def _persist(self) -> None:
        settings = get_settings()
        if not settings.trace_enabled:
            return
        try:
            trace_dir = Path(settings.trace_dir)
            trace_dir.mkdir(parents=True, exist_ok=True)
            day = datetime.now().strftime("%Y%m%d")
            path = trace_dir / f"traces-{day}.jsonl"
            line = json.dumps(self.data, ensure_ascii=False, default=str)
            with _write_lock:
                with path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception as e:  # 落盘失败只记录日志，绝不抛出
            logger.warning("trace 落盘失败: %s", e)

    # ------------------------------------------------------------
    # LangChain 回调：LLM step
    # ------------------------------------------------------------
    async def on_chat_model_start(
        self, serialized, messages, run_id, **kwargs
    ) -> None:
        step = {
            "type": "llm",
            "run_id": run_id,
            "name": (serialized or {}).get("name", "chat_model"),
            "started_at": _now_iso(),
            "latency_ms": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "error": None,
        }
        self._llm_starts[run_id] = len(self.data["steps"])
        self.data["steps"].append(step)

    async def on_llm_end(self, response, run_id, **kwargs) -> None:
        idx = self._llm_starts.pop(run_id, None)
        if idx is None:
            return
        step = self.data["steps"][idx]
        step["latency_ms"] = self._elapsed_since(step["started_at"])
        usage = _extract_usage(response)
        step["usage"] = usage
        for k in ("input_tokens", "output_tokens", "total_tokens"):
            self.data["usage"][k] += usage[k]

    async def on_llm_error(self, error, run_id, **kwargs) -> None:
        idx = self._llm_starts.pop(run_id, None)
        if idx is None:
            return
        step = self.data["steps"][idx]
        step["latency_ms"] = self._elapsed_since(step["started_at"])
        step["error"] = _preview(error)

    # ------------------------------------------------------------
    # LangChain 回调：Tool step（含 MCP 工具与内置工具）
    # ------------------------------------------------------------
    async def on_tool_start(self, serialized, input_str, run_id, **kwargs) -> None:
        args = kwargs.get("inputs", input_str)
        step = {
            "type": "tool",
            "run_id": run_id,
            "name": (serialized or {}).get("name", "unknown_tool"),
            "started_at": _now_iso(),
            "args": _preview(args),
            "result_preview": "",
            "latency_ms": 0,
            "error": None,
        }
        self._tool_starts[run_id] = len(self.data["steps"])
        self.data["steps"].append(step)

    async def on_tool_end(self, output, run_id, **kwargs) -> None:
        idx = self._tool_starts.pop(run_id, None)
        if idx is None:
            return
        step = self.data["steps"][idx]
        step["latency_ms"] = self._elapsed_since(step["started_at"])
        step["result_preview"] = _preview(output)
        self.data["tool_rounds"] += 1

    async def on_tool_error(self, error, run_id, **kwargs) -> None:
        idx = self._tool_starts.pop(run_id, None)
        if idx is None:
            return
        step = self.data["steps"][idx]
        step["latency_ms"] = self._elapsed_since(step["started_at"])
        step["error"] = _preview(error)
        self.data["tool_rounds"] += 1

    # ------------------------------------------------------------
    @staticmethod
    def _elapsed_since(started_iso: str) -> int:
        try:
            start = datetime.fromisoformat(started_iso)
            return int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
        except Exception:
            return 0


def build_callbacks(
    source: str,
    session_id: str = "",
    question: str = "",
    user_id: str = "",
    tags: list[str] | None = None,
    mode: str = "",
) -> tuple[RunTrace, list]:
    """统一构造一次运行的 callbacks。

    返回 (trace, callbacks)：调用方持有 trace 用于 finish/fail，callbacks
    直接放进 ainvoke / astream_events 的 config 中。Langfuse 开启时
    callbacks 内含两个 handler（本地 + Langfuse），否则只有本地 RunTrace。
    """
    trace = RunTrace(
        source=source,
        session_id=session_id,
        question=question,
        user_id=user_id,
        tags=tags,
        mode=mode,
    )
    callbacks: list[Any] = [trace]
    lf = _try_build_langfuse_handler(
        user_id=trace.user_id, session_id=session_id, tags=trace.tags
    )
    if lf is not None:
        callbacks.append(lf)
        trace._langfuse_trace_id = getattr(lf, "trace_id", None)
        trace.data["langfuse_trace_id"] = trace._langfuse_trace_id
    return trace, callbacks
