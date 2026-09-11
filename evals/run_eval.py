"""批量评估入口：对冻结数据集跑 Agent，用双 Judge 评分，产出可复现的评估报告。

用法：
  # Rule Judge（零 token 成本）评估全部数据集
  python -m evals.run_eval

  # 只跑回归集（CI 门禁用）
  python -m evals.run_eval --datasets regression

  # 加 LLM Judge 语义评分（耗少量 token）
  python -m evals.run_eval --llm-judge

  # 门禁：平均分低于阈值时以非零码退出（供 CI / 变更门禁使用）
  python -m evals.run_eval --datasets regression --fail-under 80

每条样本使用独立 thread_id（避免样本间上下文污染），并把 trace 落盘
（tags 带数据集/类别/样本 id），评估结论可回连到原始 trace 证据。
"""
import argparse
import asyncio
import json
import logging
import subprocess
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from langchain_core.messages import HumanMessage

from agent import build_agent
from config import get_settings
from evals.dataset import DATASET_NAMES, MANIFEST_PATH, load_dataset
from evals.evaluators import RuleJudge, Verdict, build_llm_judge
from observability import build_callbacks

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("run_eval")

REPORT_DIR = Path(__file__).parent / "reports"


def _git_commit() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
        )
        return out.decode().strip()
    except Exception:
        return "unknown"


def _manifest_version() -> str:
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8")).get("version", "unknown")
    except Exception:
        return "unknown"


async def _run_one_sample(agent, sample, dataset_name: str, run_tag: str,
                          rule_judge: RuleJudge, llm_judge, timeout: int) -> dict:
    """对单条样本执行 Agent + 双 Judge，返回结构化结果。"""
    thread_id = f"eval-{run_tag}-{sample.id}"
    trace, callbacks = build_callbacks(
        source="eval",
        session_id=thread_id,
        question=sample.question,
        user_id="evaluator",
        tags=["eval", dataset_name, sample.category, sample.id, run_tag],
        mode=f"eval:{dataset_name}",
    )
    started = time.time()
    timed_out = False
    try:
        response = await asyncio.wait_for(
            agent.ainvoke(
                {"messages": [HumanMessage(content=sample.question)]},
                config={
                    "configurable": {"thread_id": thread_id},
                    "callbacks": callbacks,
                },
            ),
            timeout=timeout,
        )
        messages = response.get("messages", [])
        answer = messages[-1].content if messages else ""
        trace.finish(output=answer)
    except asyncio.TimeoutError:
        timed_out = True
        trace.fail(TimeoutError(f"单样本执行超过 {timeout}s"))
    except Exception as e:
        trace.fail(e)

    trace_data = trace.data
    if timed_out:
        trace_data["output"] = trace_data.get("output") or f"[TIMEOUT >{timeout}s]"

    rule_v: Verdict = rule_judge.judge(sample, trace_data)
    result = {
        "sample_id": sample.id,
        "dataset": dataset_name,
        "category": sample.category,
        "question": sample.question,
        "answer": trace_data.get("output", ""),
        "run_id": trace_data["run_id"],
        "latency_ms": trace_data.get("latency_ms", int((time.time() - started) * 1000)),
        "total_tokens": trace_data.get("usage", {}).get("total_tokens", 0),
        "tool_rounds": trace_data.get("tool_rounds", 0),
        "tools_called": [s["name"] for s in trace_data.get("steps", []) if s["type"] == "tool"],
        "tools_failed": [
            s["name"] for s in trace_data.get("steps", [])
            if s["type"] == "tool" and s.get("error")
        ],
        "rule": rule_v.to_dict(),
    }
    if llm_judge is not None:
        result["llm"] = llm_judge.judge(sample, trace_data).to_dict()
    return result


def _aggregate(results: list[dict], judge_key: str = "rule") -> dict:
    scores = [r[judge_key]["score"] for r in results]
    passed = [r[judge_key]["passed"] for r in results]
    dim_scores: dict[str, list[float]] = {}
    attributions = Counter()
    for r in results:
        for c in r[judge_key].get("checks", []):
            dim_scores.setdefault(c["dimension"], []).append(100.0 if c["passed"] else 0.0)
        for a in r[judge_key].get("attribution", []):
            attributions[a] += 1
    return {
        "judge": judge_key,
        "count": len(results),
        "avg_score": round(sum(scores) / len(scores), 1) if scores else 0.0,
        "pass_rate": round(sum(passed) / len(passed) * 100, 1) if passed else 0.0,
        "dimension_avg": {k: round(sum(v) / len(v), 1) for k, v in dim_scores.items()},
        "total_tokens": sum(r["total_tokens"] for r in results),
        "avg_latency_ms": int(sum(r["latency_ms"] for r in results) / len(results)) if results else 0,
        "tool_failure_rate": round(
            sum(1 for r in results if r["tools_failed"]) / len(results) * 100, 1
        ) if results else 0.0,
        "attribution_dist": dict(attributions.most_common()),
    }


def _render_markdown(meta: dict, results: list[dict]) -> str:
    lines = [
        f"# Agent 评估报告 {meta['run_tag']}",
        "",
        f"- 生成时间：{meta['generated_at']}",
        f"- 代码版本（git）：`{meta['git_commit']}`",
        f"- 数据集快照版本：`{meta['dataset_version']}`",
        f"- 模型：`{meta['model']}`  | 温度：{meta['temperature']}",
        f"- 数据集：{', '.join(meta['datasets'])}（共 {len(results)} 条）",
        "",
        "## 总体指标",
        "",
        "| Judge | 平均分 | 通过率 | 任务结果 | 执行过程 | 成本效率 | 安全可靠 | 总Token | 平均时延 | 工具失败率 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for agg in meta["aggregates"]:
        d = agg["dimension_avg"]
        lines.append(
            f"| {agg['judge']} | {agg['avg_score']} | {agg['pass_rate']}% | "
            f"{d.get('quality', '-')} | {d.get('process', '-')} | {d.get('cost', '-')} | "
            f"{d.get('safety', '-')} | {agg['total_tokens']} | {agg['avg_latency_ms']}ms | "
            f"{agg['tool_failure_rate']}% |"
        )

    lines += ["", "## 逐样本结果", "",
              "| 样本 | 集合 | 类别 | 分数 | 通过 | 工具轮数 | Token | 调用工具 | 失败工具 |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        rule = r["rule"]
        mark = "✅" if rule["passed"] else "❌"
        lines.append(
            f"| {r['sample_id']} | {r['dataset']} | {r['category']} | "
            f"{rule['score']} | {mark} | {r['tool_rounds']} | {r['total_tokens']} | "
            f"{', '.join(r['tools_called']) or '—'} | "
            f"{', '.join(r['tools_failed']) or '—'} |"
        )

    fails = [r for r in results if not r["rule"]["passed"]]
    if fails:
        lines += ["", "## 未通过样本的证据与归因", ""]
        for r in fails:
            rule = r["rule"]
            lines.append(f"### ❌ {r['sample_id']}（{r['category']}）score={rule['score']}")
            lines.append(f"- **问题**：{r['question']}")
            lines.append(f"- **回答（节选）**：{str(r['answer'])[:200]}")
            for fatal in rule["fatals"]:
                lines.append(f"- 🔴 **致命**：{fatal}")
            bad = [c for c in rule["checks"] if not c["passed"]]
            for c in bad:
                lines.append(f"- [{c['dimension']}] **{c['name']}**：{c['evidence']}")
            if rule["attribution"]:
                lines.append(f"- 🎯 **归因层级**：{', '.join(rule['attribution'])}")
            lines.append(f"- 🔗 trace run_id：`{r['run_id']}`（可在 traces/ 或 Langfuse 中下钻）")
            lines.append("")
    return "\n".join(lines)


async def main_async(args):
    run_tag = datetime.now().strftime("%Y%m%d-%H%M%S")
    names = [n for n in args.datasets.split(",") if n in DATASET_NAMES]
    samples = []
    skipped_cluster = 0
    for name in names:
        ds = load_dataset(name)
        for s in ds:
            if args.offline_only and s.requires_cluster:
                skipped_cluster += 1
                continue
            samples.append((name, s))
    if args.limit:
        samples = samples[: args.limit]
    if skipped_cluster:
        print(f"--offline-only：跳过 {skipped_cluster} 条依赖真实集群的样本")

    print(f"装配 Agent（数据集快照 v{_manifest_version()}）...")
    agent = await build_agent()
    rule_judge = RuleJudge()
    llm_judge = build_llm_judge() if args.llm_judge else None

    results = []
    for i, (ds_name, sample) in enumerate(samples, 1):
        print(f"[{i}/{len(samples)}] {sample.id} ({ds_name}/{sample.category}) ...", end=" ", flush=True)
        result = await _run_one_sample(
            agent, sample, ds_name, run_tag, rule_judge, llm_judge, args.timeout
        )
        results.append(result)
        print(f"score={result['rule']['score']} pass={result['rule']['passed']}")

    settings = get_settings()
    aggregates = [_aggregate(results, "rule")]
    if args.llm_judge:
        aggregates.append(_aggregate(results, "llm"))

    meta = {
        "run_tag": run_tag,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "git_commit": _git_commit(),
        "dataset_version": _manifest_version(),
        "model": settings.llm_model,
        "temperature": settings.llm_temperature,
        "datasets": names,
        "aggregates": aggregates,
    }

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / f"eval-{run_tag}.json").write_text(
        json.dumps({"meta": meta, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md = _render_markdown(meta, results)
    md_path = REPORT_DIR / f"eval-{run_tag}.md"
    md_path.write_text(md, encoding="utf-8")

    print("\n" + "=" * 60)
    for agg in aggregates:
        print(
            f"[{agg['judge']}] avg={agg['avg_score']} pass={agg['pass_rate']}% "
            f"tokens={agg['total_tokens']} 归因={agg['attribution_dist']}"
        )
    print(f"报告：{md_path}")

    if args.fail_under is not None and aggregates[0]["avg_score"] < args.fail_under:
        print(f"❌ 平均分 {aggregates[0]['avg_score']} < 门禁 {args.fail_under}，退出码 1")
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description="K8s Ops Agent 批量评估")
    parser.add_argument("--datasets", default="baseline,test,regression",
                        help="逗号分隔：baseline,test,regression")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（调试用）")
    parser.add_argument("--offline-only", action="store_true",
                        help="只跑不依赖真实集群的样本（CI 环境用）")
    parser.add_argument("--llm-judge", action="store_true", help="额外启用 LLM Judge 语义评分")
    parser.add_argument("--timeout", type=int, default=120, help="单样本超时秒数")
    parser.add_argument("--fail-under", type=float, default=None,
                        help="Rule 平均分低于该值时以非零码退出（CI 门禁）")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
