# K8s Ops Agent（K8s 集群智能运维助手）

面向 SRE 的 Kubernetes 智能运维助手：通过 **MCP 协议**接入已有 K8s MCP Server 查询集群状态，结合 **LangGraph Agent** 编排 LLM 与工具，支持 **CLI** 与 **飞书机器人** 双端交互，实现"一句话完成集群排障"。

本项目是上层 Agent 编排层；底层 K8s MCP Server（把 `kubectl get / describe / logs / events --watch` 封装成只读 MCP 工具）在独立仓库 [k8s-mcp-server](https://github.com/zj-hub-coder/k8s-mcp-server)。

## 架构

```
┌─────────────┐   ┌──────────────────────┐   ┌─────────────────┐
│  交互层      │   │  Agent 编排层          │   │  工具层          │
│  CLI / 飞书  │──▶│  LangGraph + LLM      │──▶│  MCP 工具(动态)  │
│  WebSocket  │   │  记忆 / 流式 / ReAct   │   │  内置工具(自动发现)│
└─────────────┘   └──────────────────────┘   └─────────────────┘
```

- **MCP 工具层**：配置驱动（`mcp_servers.json`）动态接入多个 MCP Server（stdio / Streamable HTTP），新增 server 零代码改动
- **Agent 编排层**：LangGraph `create_agent` 组装 LLM + 工具集 + 系统提示词；`MemorySaver` Checkpointer 按 `thread_id` 维护多轮上下文；支持 token 级流式输出
- **死循环防护**：`loop_guard` 中间件限制最大工具调用轮数 + 检测连续重复调用，防止 Agent 反复调用同一工具无法收敛（见下文「防死循环」）
- **内置工具**：`tools/` 目录自动发现注册（如 DuckDuckGo 搜索），新增工具只需加一个 `@tool` 文件
- **交互层**：CLI（rich 终端交互）/ 飞书 WebSocket 长连接机器人（流式卡片 + 三级降级容错 + 消息去重）

## 项目结构

```
langchain_bot/
├── pyproject.toml        # 项目配置与依赖
├── config.py             # pydantic-settings 配置管理（全部从 .env 加载）
├── mcp_servers.json      # MCP Server 连接配置
├── agent.py              # Agent 核心装配（build_agent）
├── loop_guard.py         # 死循环防护中间件（轮数上限 + 重复检测）
├── prompts.py            # 系统提示词
├── cli.py                # CLI 交互入口
├── start_lark.py         # 飞书 WebSocket 机器人入口
├── lark_bot.py           # 飞书交互层（卡片/流式/降级/去重）
├── session_manager.py    # user_id -> thread_id 会话映射
└── tools/
    ├── __init__.py       # 内置工具自动发现
    └── search.py         # DuckDuckGo 搜索工具
```

## 快速开始

### 1. 安装依赖

```powershell
python -m venv .venv
.venv\Scripts\pip install -e .
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env` 并填写：

```ini
# LLM（OpenAI 兼容接口）
OPENAI_API_KEY=sk-xxx
OPENAI_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-plus
LLM_TEMPERATURE=0.7
LLM_MAX_TOKENS=500

# MCP
MCP_SERVERS_FILE=mcp_servers.json
```

### 3. 配置 MCP Server

编辑 `mcp_servers.json`，key 为 server 名称：

```json
{
  "k8s_mcp": {
    "transport": "stdio",
    "command": "E:/path/to/k8s_mcp_server/.venv/Scripts/python.exe",
    "args": ["E:/path/to/k8s_mcp_server/k8s_server.py"]
  }
}
```

远程 server 使用 SSE / Streamable HTTP：

```json
{
  "remote_mcp": { "url": "http://127.0.0.1:8081/mcp" }
}
```

### 4. 启动

```powershell
# CLI 交互（本地排障）
.venv\Scripts\python.exe cli.py

# 飞书机器人（需在 .env 配置 LARK_APP_ID / LARK_APP_SECRET）
.venv\Scripts\python.exe start_lark.py
```

## 防死循环

ReAct 循环中 LLM 可能反复调用同一工具、拿到相同结果却无法收敛，既浪费 token 也给集群接口带来压力。`loop_guard` 中间件提供两层防护：

- **最大工具调用轮数**：超过 `MAX_TOOL_ROUNDS`（默认 8）后强制停止调用工具，基于已获取的信息直接给出结论。
- **连续重复调用检测**：连续两轮以相同参数调用同一工具，判定为死循环提前终止。

轮数与重复检测直接从对话历史推导，天然按会话隔离，多用户并发互不影响。阈值可通过 `.env` 的 `MAX_TOOL_ROUNDS` 调整。

## 扩展方式

**新增 MCP Server**：在 `mcp_servers.json` 加一项即可，无需改代码。

**新增内置工具**：在 `tools/` 下新建文件，用 `@tool` 装饰函数即可被自动注册：

```python
from langchain_core.tools import tool

@tool
def my_tool(query: str) -> str:
    """工具描述（LLM 据此决定何时调用）"""
    ...
```

## 技术栈

Python / LangChain / LangGraph / MCP（langchain-mcp-adapters）/ lark-oapi / pydantic-settings / rich / duckduckgo-search
