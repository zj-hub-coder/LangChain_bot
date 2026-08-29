"""互联网搜索工具封装（默认 DuckDuckGo）。

用于检索 Kubernetes / 云原生相关知识：不确定的 API 字段、报错含义、
版本兼容性、最佳实践等。延迟导入 duckduckgo_search，未安装时给出清晰提示
而非直接崩溃。
"""
from langchain_core.tools import tool


@tool
def search_k8s_knowledge(query: str) -> str:
    """在互联网上检索 Kubernetes / 云原生相关知识、报错含义与最佳实践。

    Args:
        query: 检索关键词，中文或英文均可，建议聚焦具体问题。

    Returns:
        检索到的前若干条结果的标题、链接与摘要；失败时返回原因说明。
    """
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        return "搜索工具未就绪：请先安装依赖 `pip install duckduckgo-search` 后重试。"

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
    except Exception as e:  # 检索失败不应打断 Agent 流程
        return f"搜索失败：{type(e).__name__}: {e}"

    if not results:
        return f"未检索到与「{query}」相关的内容。"

    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "") or ""
        href = r.get("href") or r.get("url", "") or ""
        body = (r.get("body", "") or "").strip()
        lines.append(f"{i}. {title}\n   链接：{href}\n   摘要：{body}")
    return "\n\n".join(lines)
