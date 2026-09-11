"""可观测层：统一 Trace 采集。

- RunTrace：单次 Agent 运行的追踪器（同时是 LangChain CallbackHandler），
  自动收集 LLM / 工具每一步的耗时、token、入参、结果摘要、错误，运行结束
  落盘为一行 JSONL，并可按需双写 Langfuse。
- build_callbacks：供 CLI / 飞书 / 评估入口统一构造回调列表。
"""
from .tracer import RunTrace, build_callbacks

__all__ = ["RunTrace", "build_callbacks"]
