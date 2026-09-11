"""经验库（AgentLoop 优化杠杆之一：Experience）。

把已验证的有效做法沉淀为"有来源、有边界、有版本"的运行时上下文：
- 每条经验在 index.json 注册（适用场景 applies_when / 版本 / 来源 / 状态）
- 正文为 markdown playbook，仅 status=active 时加载
- 装配 Agent 时把经验摘要注入系统提示词；经验不匹配场景时其边界条款
  明确要求"不使用"（不匹配，就不使用）

当前经验数量少，采用全量静态注入；未来经验增多后可替换为基于场景的
语义匹配检索（接口保持 render_active_experiences() 不变）。
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_EXP_DIR = Path(__file__).parent
_INDEX_PATH = _EXP_DIR / "index.json"


def render_active_experiences() -> str:
    """渲染所有 active 经验为可追加到系统提示词的文本块；无经验时返回空串。"""
    if not _INDEX_PATH.is_file():
        return ""
    try:
        index = json.loads(_INDEX_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("经验库 index.json 解析失败，跳过经验注入: %s", e)
        return ""

    blocks = []
    for item in index:
        if item.get("status") != "active":
            continue
        md_path = _EXP_DIR / item["file"]
        if not md_path.is_file():
            logger.warning("经验 %s 的正文文件缺失: %s", item["id"], item["file"])
            continue
        try:
            body = md_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("经验 %s 正文读取失败: %s", item["id"], e)
            continue
        applies = "；".join(item.get("applies_when", []))
        blocks.append(
            f"### 经验 {item['id']}（v{item.get('version', '?')}，来源：{item.get('source', '未知')}）\n"
            f"适用场景：{applies}\n不匹配该场景时不要套用本经验。\n\n{body}"
        )

    if not blocks:
        return ""
    return (
        "# 已验证经验库（仅在匹配场景下使用，每条都有适用边界与版本）\n"
        "以下经验来自历史真实任务与评估回归，请在匹配场景优先复用；\n"
        "场景不匹配时不要生搬硬套。\n\n" + "\n\n".join(blocks)
    )
