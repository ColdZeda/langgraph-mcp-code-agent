"""阶段 6 评估入口（旧 30 题集已在阶段 5 删除，这里是从零重建的那一套）。

用法::

    uv run python evals/run_e2e.py --list                    # 只看题集结构（不跑、不烧 token）
    uv run python evals/run_e2e.py --all --mode single --run-id v3-single
    uv run python evals/run_e2e.py --all --mode multi  --run-id v3-multi
    uv run python evals/run_e2e.py --task E001 --task E002 --mode single   # 抽查几题
    uv run python evals/run_e2e.py --all --mode single --run-id smoke --limit 2   # 先试跑两题

结果落 `runtime/runs/{run_id}.json`（gitignore）；加 `--archive` 会再复制一份到
`docs/evidence/`（纳入版本控制，**正式结果才用它**）。

⚠️ 两轮之间**必须换 run-id**：thread_id 里带 run-id 与 mode，
复用同一个 run-id 会让第二轮读到第一轮的 checkpoint。
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.config import PROJECT_ROOT  # noqa: E402
from evals import verifiers as V  # noqa: E402
from evals.runner import run_all, save_run, task_inventory  # noqa: E402
from evals.tasks import TASKS  # noqa: E402


def _print_inventory() -> None:
    inventory = task_inventory(TASKS)
    print("=" * 78)
    print(f"题集：{inventory['total']} 题")
    print(f"  类型分布: {inventory['kinds']}")
    print(f"  维度分布: {inventory['dimensions']}")
    print(f"  缺状态断言的题: {inventory['without_state_assertion'] or '（无）'}")
    print("-" * 78)
    print(f"{'题号':<7}{'维度':<20}{'类型':<13}{'难度':<8}判定器（档位）")
    tier_short = {
        V.TIER_STATE: "状态",
        V.TIER_TRACE: "轨迹",
        V.TIER_TEXT: "文本",
        V.TIER_JUDGE: "评分",
    }
    for task in TASKS:
        marks = [tier_short.get(getattr(f, "__tier__", "?"), "?") for f in task.checks]
        flag = (
            ""
            if V.TIER_STATE in [getattr(f, "__tier__", None) for f in task.checks]
            else " ⚠️无状态断言"
        )
        print(
            f"{task.id:<7}{task.dimension:<20}{task.kind:<13}{task.difficulty:<8}"
            f"{len(task.checks)} 条 {'/'.join(sorted(set(marks)))}"
            f"{'' if task.permission_mode is None else f' [权限={task.permission_mode}]'}{flag}"
        )
    print("=" * 78)


def _print_summary(payload: dict) -> None:
    totals = payload["totals"]
    print()
    print("=" * 78)
    print(f"run_id={payload['run_id']}  模式={payload['mode']}  墙钟={payload['wall_sec']}s")
    print(
        f"通过 {totals['passed']}/{totals['available']}（通过率 {totals['pass_rate']}）"
        f"；部分分 {totals['partial']}；未测 {totals['unavailable']}；"
        f"超时 {totals['timeout']}；异常 {totals['error']}"
    )
    print(
        f"平均得分 {totals['score_avg']} ｜ token {totals['tokens']} ｜ "
        f"工具调用 {totals['tool_calls']} ｜ 步数 {totals['steps']}"
    )
    print("-" * 78)
    print("断言强度统计（硬/弱比例就是从这里来的）:")
    for tier, slot in totals["tiers"].items():
        print(
            f"  {V.TIER_LABELS[tier]:<16} 共 {slot['total']:>4} 条"
            f"（过 {slot['passed']} / 未过 {slot['failed']} / 未测 {slot['skipped']}）"
        )
    print("-" * 78)
    print(f"{'题号':<7}{'维度':<18}{'结果':<8}{'得分':<8}{'耗时':<9}{'token':<9}失败断言")
    for r in payload["tasks"]:
        verdict = "PASS" if r["passed"] else ("未测" if r["unavailable"] else "FAIL")
        fails = [c["name"] for c in r["checks"] if c["ok"] is False]
        print(
            f"{r['id']:<7}{r['dimension']:<18}{verdict:<8}{str(r['score']):<8}"
            f"{r['elapsed_ms'] / 1000:>6.1f}s  {r['token_usage']:>7}  "
            f"{('；'.join(fails))[:60]}"
        )
    print("=" * 78)


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 6 评估（题集 + 强断言评分器）")
    parser.add_argument("--all", action="store_true", help="跑全部题")
    parser.add_argument("--task", action="append", default=[], help="只跑指定题号（可重复）")
    parser.add_argument(
        "--mode", default="single", choices=["single", "multi", "auto"], help="执行模式"
    )
    parser.add_argument("--run-id", default=None, help="本次运行标识（默认按模式与时间生成）")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 题（试跑用）")
    parser.add_argument("--list", action="store_true", help="只列题集结构，不执行")
    parser.add_argument(
        "--no-reuse-tools", action="store_true", help="每題重新加载 MCP 工具（慢，排查用）"
    )
    parser.add_argument("--archive", action="store_true", help="结果另复制一份到 docs/evidence/")
    args = parser.parse_args()

    if args.list or (not args.all and not args.task):
        _print_inventory()
        if not args.all and not args.task:
            print("\n（没有 --all / --task，只列了结构，未执行任何题目）")
        return 0

    from datetime import datetime

    run_id = args.run_id or f"{args.mode}-{datetime.now().strftime('%m%d-%H%M%S')}"
    payload = asyncio.run(
        run_all(
            TASKS,
            mode=args.mode,
            run_id=run_id,
            only=args.task or None,
            limit=args.limit,
            reuse_tools=not args.no_reuse_tools,
        )
    )
    path = save_run(payload)
    _print_summary(payload)
    print(f"\n结果已保存: {path}")

    if args.archive:
        dest = PROJECT_ROOT / "docs" / "evidence" / path.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        print(f"已归档到版本控制目录: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
