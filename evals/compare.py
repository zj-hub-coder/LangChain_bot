"""版本 A/B 对比（AgentLoop 第 6 环：实验 —— 同一样本对比，证明"改了之后到底好不好"）。

对两份评估报告（evals/reports/eval-*.json）按 sample_id join，对比
平均分 / 通过率 / token / 工具轮数，并列出改善与退化样本。

用法：
  python -m evals.compare                      # 自动对比最近两份报告
  python -m evals.compare A.json B.json        # 指定报告
  python -m evals.compare A.json B.json --check-regression   # 存在退化样本则非零退出（门禁用）
"""
import argparse
import json
import sys
from pathlib import Path

REPORT_DIR = Path(__file__).parent / "reports"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _latest_two() -> tuple[Path, Path]:
    reports = sorted(REPORT_DIR.glob("eval-*.json"))
    if len(reports) < 2:
        raise SystemExit("reports/ 下报告不足两份，请先各跑一次评估（改动前 / 改动后）")
    return reports[-2], reports[-1]


def _result_index(report: dict) -> dict[str, dict]:
    return {r["sample_id"]: r for r in report["results"]}


def compare(path_a: Path, path_b: Path) -> int:
    a, b = _load(path_a), _load(path_b)
    print(f"对比报告：")
    print(f"  A（改动前）: {path_a.name}  git={a['meta']['git_commit']}  数据集v={a['meta']['dataset_version']}")
    print(f"  B（改动后）: {path_b.name}  git={b['meta']['git_commit']}  数据集v={b['meta']['dataset_version']}")
    if a["meta"]["dataset_version"] != b["meta"]["dataset_version"]:
        print("⚠️ 两份报告基于不同数据集快照版本，对比结论可能失真！")

    agg_a, agg_b = a["meta"]["aggregates"][0], b["meta"]["aggregates"][0]

    def diff(x, y, suffix=""):
        d = y - x
        arrow = "→" if abs(d) < 1e-9 else ("↑" if d > 0 else "↓")
        return f"{x}{suffix} {arrow} {y}{suffix} ({d:+.1f}{suffix})"

    print("\n== 总体（Rule Judge）==")
    print(f"  平均分：{diff(agg_a['avg_score'], agg_b['avg_score'])}")
    print(f"  通过率：{diff(agg_a['pass_rate'], agg_b['pass_rate'], '%')}")
    print(f"  总Token：{agg_a['total_tokens']} → {agg_b['total_tokens']} "
          f"({agg_b['total_tokens'] - agg_a['total_tokens']:+d})")
    print(f"  平均时延：{agg_a['avg_latency_ms']}ms → {agg_b['avg_latency_ms']}ms")

    ia, ib = _result_index(a), _result_index(b)
    common = sorted(set(ia) & set(ib))
    improved, regressed, unchanged = [], [], []
    for sid in common:
        sa, sb = ia[sid]["rule"]["score"], ib[sid]["rule"]["score"]
        if sb > sa:
            improved.append((sid, sa, sb))
        elif sb < sa:
            regressed.append((sid, sa, sb))
        else:
            unchanged.append(sid)

    print(f"\n== 逐样本（共 {len(common)} 条可比）==")
    print(f"  改善 {len(improved)} / 退化 {len(regressed)} / 持平 {len(unchanged)}")
    for sid, sa, sb in improved:
        print(f"  ✅ {sid}: {sa} → {sb}")
    for sid, sa, sb in regressed:
        print(f"  ❌ {sid}: {sa} → {sb}  ← 退化！")

    only_a = sorted(set(ia) - set(ib))
    only_b = sorted(set(ib) - set(ia))
    if only_a:
        print(f"  仅 A 含：{only_a}")
    if only_b:
        print(f"  仅 B 含：{only_b}")

    return 0 if not regressed else 2


def main():
    parser = argparse.ArgumentParser(description="两份评估报告 A/B 对比")
    parser.add_argument("report_a", nargs="?")
    parser.add_argument("report_b", nargs="?")
    parser.add_argument("--check-regression", action="store_true",
                        help="存在退化样本时以非零码退出")
    args = parser.parse_args()

    if args.report_a and args.report_b:
        path_a, path_b = Path(args.report_a), Path(args.report_b)
    else:
        path_a, path_b = _latest_two()

    code = compare(path_a, path_b)
    if args.check_regression and code != 0:
        sys.exit(code)
    if not args.check_regression:
        sys.exit(0)


if __name__ == "__main__":
    main()
