"""K8s 运维助手 Agent 核心逻辑（装配编排）。

本模块只负责“装配”：初始化 LLM、按配置接入所有 MCP server、收集内置工具，
再通过 langchain.agents.create_agent 组装成可调用的 Agent。

多轮对话上下文由 langgraph checkpointer（MemorySaver）维护：调用方每次只
传入新的 HumanMessage 并附带 config={"configurable": {"thread_id": ...}}，
历史消息由 checkpointer 自动累积，无需自行管理消息列表。

具体 MCP server 配置见 mcp_servers.json，内置工具见 tools/ 包（自动发现）。
新增 MCP server 或内置工具均无需改动本文件。

对外主入口：build_agent() —— 异步返回一个 CompiledStateGraph，
调用方 await agent.ainvoke({"messages": [...]}, config={...}) 即可。
"""
from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver

from config import get_mcp_servers, get_settings
from loop_guard import LoopGuardMiddleware
from prompts import SYSTEM_PROMPT
from tools import get_builtin_tools


def init_llm() -> ChatOpenAI:
    """初始化大语言模型，含必要环境变量校验。"""
    s = get_settings()
    if not s.llm_ready:
        raise ValueError(
            "请在 .env 中配置 OPENAI_API_KEY、OPENAI_API_BASE 和 LLM_MODEL"
        )
    return ChatOpenAI(
        api_key=s.openai_api_key,
        base_url=s.openai_api_base,
        model=s.llm_model,
        temperature=s.llm_temperature,
        max_tokens=s.llm_max_tokens,
        # 流式时回传 token 用量，供飞书卡片直接统计，无需额外一次阻塞调用
        # （DashScope 兼容端点已验证支持 include_usage）
        stream_usage=True,
    )


async def get_mcp_tools():
    """加载 mcp_servers.json 并接入所有 MCP server，聚合其注册的工具。"""
    servers = get_mcp_servers()
    if not servers:
        return []
    client = MultiServerMCPClient(servers)
    return await client.get_tools()


async def build_agent():
    """构建并返回可调用的 Agent。

    工具来源：
      1. MCP 工具（按 mcp_servers.json 动态接入，支持多 server）
      2. 内置工具（tools/ 下自动发现）
    多轮记忆：通过 MemorySaver checkpointer 实现，调用方用 thread_id 区分会话。
    如需跨进程持久化记忆，可替换为 SqliteSaver / PostgresSaver。
    """
    llm = init_llm()
    mcp_tools = await get_mcp_tools()
    tools = list(mcp_tools) + get_builtin_tools()
    if not tools:
        print("警告：未装配任何工具，Agent 将仅依赖模型自身能力作答。")
    settings = get_settings()
    return create_agent(
        llm,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        checkpointer=MemorySaver(),
        # ReAct 死循环防护：最大工具轮数 + 连续重复调用检测
        middleware=[LoopGuardMiddleware(max_tool_rounds=settings.max_tool_rounds)],
    )
