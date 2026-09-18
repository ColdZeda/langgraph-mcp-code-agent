"""对比两份 STAR JSON 评估结果，输出每个维度的提升幅度。

用法: uv run python evals/compare.py runtime/runs/baseline.json runtime/runs/optimized.json
"""

import json
import sys
from pathlib import Path


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def compare(before: dict, after: dict) -> None:
    print(f"\n{'='*55}")
    print(f"Baseline: {before.get('run_id','?')}  →  Optimized: {after.get('run_id','?')}")
    print(f"{'='*55}")

    dims = ["tool_selection", "task_completion", "multi_step",
            "cross_tool", "error_recovery", "safety"]

    for dim in dims:
        b = before.get("by_dimension", {}).get(dim, {}).get("score", 0)
        a = after.get("by_dimension", {}).get(dim, {}).get("score", 0)
        delta = a - b
        bar = "+" * max(0, int(delta * 20)) + "-" * max(0, int((1 - a) * 10))
        print(f"  {dim:20s}  {b:.2f} → {a:.2f}  ({delta:+.2f})  {bar}")

    b_overall = before.get("overall", {}).get("score", 0)
    a_overall = after.get("overall", {}).get("score", 0)
    delta = a_overall - b_overall
    print(f"  {'─'*50}")
    print(f"  {'overall':20s}  {b_overall:.2f} → {a_overall:.2f}  ({delta:+.2f})")

    # 额外指标
    print(f"\n  额外指标:")
    for key in ["avg_steps", "avg_latency_sec", "avg_tool_calls"]:
        bv = before.get("overall", {}).get(key, "N/A")
        av = after.get("overall", {}).get(key, "N/A")
        if isinstance(bv, (int, float)) and isinstance(av, (int, float)):
            print(f"    {key}: {bv:.1f} → {av:.1f}  ({av - bv:+.1f})")
    print()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: uv run python evals/compare.py <before.json> <after.json>")
        sys.exit(1)

    before = load(sys.argv[1])
    after = load(sys.argv[2])
    compare(before, after)
