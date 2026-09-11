"""用户反馈回流存储（AgentLoop 最后一环：运行结果与用户反馈 → 样本池）。

飞书卡片上的 👍/👎 点击事件统一追加到 feedback/feedback-YYYYMMDD.jsonl。
原始反馈不自动进回归集（可能含噪声/敏感信息），由维护者定期评审：
挑出真实 bad case，脱敏后转为 regression.jsonl 中的冻结样本（并回填
source_trace_id 完成 Lineage 闭环）。
"""
import json
import logging
import threading
from datetime import datetime
from pathlib import Path

from config import get_settings

logger = logging.getLogger(__name__)
_lock = threading.Lock()


def append_feedback(payload: dict) -> Path:
    """追加一条反馈记录，返回写入文件路径。"""
    settings = get_settings()
    fb_dir = Path(settings.feedback_dir)
    fb_dir.mkdir(parents=True, exist_ok=True)
    path = fb_dir / f"feedback-{datetime.now().strftime('%Y%m%d')}.jsonl"
    record = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        **payload,
    }
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    logger.info("用户反馈已记录: %s -> %s", payload.get("feedback"), path)
    return path
