"""冻结数据集管理（AgentLoop 第 4 环：把线上 Trace 变成可复用、可版本化的数据集资产）。

对应课程要求的 Dataset Snapshot 四要素：
- 选择条件：baseline（核心能力基线）/ test（边界与 Bad Case）/ regression（变更门禁）
- 样本顺序：严格按 jsonl 行序加载，不 shuffle，防止随机漂移
- 版本与 Hash：manifest.json 记录每个集合文件的 sha256，运行前可校验未被篡改
- Lineage：每条样本保留 source_trace_id，可回溯到线上原始 Trace / Step
"""
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

DATASET_DIR = Path(__file__).parent / "datasets"
MANIFEST_PATH = DATASET_DIR / "manifest.json"

DATASET_NAMES = ("baseline", "test", "regression")


@dataclass
class Expectation:
    """Rule Judge 的确定性断言（全部可零成本机器校验）。"""
    must_call_any: list[str] = field(default_factory=list)
    forbidden_tools: list[str] = field(default_factory=list)
    keywords_any: list[str] = field(default_factory=list)
    keywords_all: list[str] = field(default_factory=list)
    must_refuse: bool = False
    grounding: bool = False
    max_tool_rounds: int = 8

    @classmethod
    def from_dict(cls, d: dict) -> "Expectation":
        return cls(
            must_call_any=d.get("must_call_any", []),
            forbidden_tools=d.get("forbidden_tools", []),
            keywords_any=d.get("keywords_any", []),
            keywords_all=d.get("keywords_all", []),
            must_refuse=d.get("must_refuse", False),
            grounding=d.get("grounding", False),
            max_tool_rounds=d.get("max_tool_rounds", 8),
        )


@dataclass
class Sample:
    id: str
    category: str
    question: str
    expect: Expectation
    source_trace_id: str | None
    note: str
    requires_cluster: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "Sample":
        missing = [k for k in ("id", "category", "question", "expect") if k not in d]
        if missing:
            raise ValueError(f"样本缺少必需字段 {missing}: {d}")
        return cls(
            id=d["id"],
            category=d["category"],
            question=d["question"],
            expect=Expectation.from_dict(d["expect"]),
            source_trace_id=d.get("source_trace_id"),
            note=d.get("note", ""),
            requires_cluster=d.get("requires_cluster", True),
        )


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_dataset(name: str, verify: bool = True) -> list[Sample]:
    """按行序加载一个数据集（不 shuffle）。

    verify=True 时先校验文件 sha256 与 manifest 一致，防止快照被悄悄改动
    导致"同样本对比"失真。
    """
    if name not in DATASET_NAMES:
        raise ValueError(f"未知数据集 {name}，可选：{DATASET_NAMES}")
    path = DATASET_DIR / f"{name}.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"数据集文件不存在：{path}")

    if verify and MANIFEST_PATH.is_file():
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        expected = manifest["datasets"].get(name, {}).get("sha256")
        if expected and expected != _file_sha256(path):
            raise RuntimeError(
                f"数据集 {name} 与冻结快照不一致（sha256  mismatch）。"
                f"如确需变更样本，请重新运行: python -m evals.dataset build"
            )

    samples: list[Sample] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            samples.append(Sample.from_dict(json.loads(line)))
        except Exception as e:
            raise ValueError(f"{path.name} 第 {lineno} 行样本解析失败: {e}") from e
    return samples


def load_all(verify: bool = True) -> dict[str, list[Sample]]:
    return {name: load_dataset(name, verify=verify) for name in DATASET_NAMES}


def build_manifest() -> dict:
    """重新冻结快照：扫描三个 jsonl，计算 sha256 / 样本数 / id 列表，写 manifest。

    任何对数据集的增删改都必须显式执行本函数（重新冻结），保证评估对比
    永远基于同一批可追溯数据。
    """
    from datetime import datetime, timezone

    datasets = {}
    for name in DATASET_NAMES:
        path = DATASET_DIR / f"{name}.jsonl"
        if not path.is_file():
            continue
        samples = load_dataset(name, verify=False)
        ids = [s.id for s in samples]
        if len(ids) != len(set(ids)):
            dupes = {i for i in ids if ids.count(i) > 1}
            raise ValueError(f"数据集 {name} 存在重复 id: {dupes}")
        datasets[name] = {
            "file": f"{name}.jsonl",
            "count": len(samples),
            "sha256": _file_sha256(path),
            "ids": ids,
        }

    manifest = {
        "version": datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "datasets": datasets,
    }
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "build":
        m = build_manifest()
        print("数据集快照已冻结：")
        for name, info in m["datasets"].items():
            print(f"  {name}: {info['count']} 条, sha256={info['sha256'][:16]}...")
        print(f"manifest version: {m['version']}")
    else:
        print("用法: python -m evals.dataset build")
