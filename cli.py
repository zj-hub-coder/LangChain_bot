"""CLI 交互入口：多轮对话 REPL。

启动流程：build_agent() 装配 LLM + MCP 工具 + 内置工具（含 MemorySaver
checkpointer）-> 进入循环读取用户输入 -> 以 thread_id 标识会话，每次只传新的
HumanMessage，上下文由 checkpointer 自动累积 -> 渲染回答。

交互命令：help 帮助、reset 开启新会话、exit/quit 退出。
"""
import asyncio
import uuid

from langchain_core.messages import HumanMessage
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from agent import build_agent

console = Console()

WELCOME = """[bold]k8s-ops-agent[/bold] · 面向 SRE 的 Kubernetes 运维助手已就绪。

可用能力：
• 集群查询：节点 / Pod 状态、资源用量、事件、日志等（经 MCP 工具）
• 知识检索：互联网检索 K8s 相关知识（search_k8s_knowledge）
• 直接计算：资源申请量、副本数等简单运算

输入 [bold]help[/bold] 查看命令，[bold]exit[/bold] 退出。"""

HELP = """可用命令：
  help / h      显示本帮助
  reset         开启新会话（清空上下文）
  exit / quit   退出
其它输入将作为问题发送给 Agent。"""


async def chat():
    """异步对话主循环。

    多轮上下文由 langgraph checkpointer 维护：同一 thread_id 下的历史自动累积，
    reset 通过更换 thread_id 开启全新会话。无需在本地维护消息列表。
    """
    console.print(Panel(WELCOME, border_style="cyan", title="k8s-ops-agent"))
    console.rule()

    # 装配 Agent：会拉起 MCP server 子进程并获取工具
    try:
        with console.status("[cyan]正在装配 Agent（连接 MCP Server）...[/cyan]", spinner="dots"):
            agent = await build_agent()
    except Exception as e:
        console.print(f"[red]Agent 初始化失败：{type(e).__name__}: {e}[/red]")
        console.print("[yellow]请检查 .env 的 LLM 配置与 mcp_servers.json。[/yellow]")
        return

    session_id = uuid.uuid4().hex
    while True:
        try:
            user = console.input("\n[bold cyan]你>[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[red]再见。[/red]")
            break

        if not user:
            continue
        low = user.lower()
        if low in {"exit", "quit", "q", "退出"}:
            console.print("[red]再见。[/red]")
            break
        if low in {"help", "h", "帮助"}:
            console.print(HELP)
            continue
        if low in {"reset", "重置"}:
            session_id = uuid.uuid4().hex
            console.print("[green]已开启新会话（上下文已清空）。[/green]")
            continue

        # 只传新消息，历史由 checkpointer 按 thread_id 自动维护
        try:
            with console.status("[cyan]助手思考中...[/cyan]", spinner="dots"):
                response = await agent.ainvoke(
                    {"messages": [HumanMessage(content=user)]},
                    config={"configurable": {"thread_id": session_id}},
                )
        except KeyboardInterrupt:
            console.print("\n[red]已中断本轮回答。[/red]")
            continue
        except Exception as e:
            console.print(f"[red]调用失败：{type(e).__name__}: {e}[/red]")
            continue

        messages = response.get("messages", [])
        answer = messages[-1].content if messages else ""
        console.print(Panel(Markdown(answer), title="助手", border_style="green"))


def chat_sync():
    """同步入口，供 pyproject scripts 与 `python cli.py` 使用。"""
    asyncio.run(chat())


if __name__ == "__main__":
    chat_sync()
