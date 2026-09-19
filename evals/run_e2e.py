"""端到端 Evals Runner — STAR JSON 输出 + 增量保存 + 断点续跑。

用法:
  uv run python evals/run_e2e.py --all              # 全跑
  uv run python evals/run_e2e.py --all --force       # 强制重跑
  uv run python evals/run_e2e.py --task E001         # 单题
  uv run python evals/run_e2e.py --dimension safety  # 按维度跑
"""

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.agent.code_agent import run_single_task
from app.code_agent.config import MODEL_NAME, WORKSPACE_DIR
from evals.tasks import TASKS, get_tasks
from evals.verifiers import EvalContext, VerifierResult

# 项目根目录（不管在哪运行，结果都存到这里）
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = PROJECT_ROOT / "runtime" / "runs"

# 本次运行用的执行模式（由 --mode 设置；build_meta 会把它写进结果 JSON）
RUN_MODE = "auto"


def get_git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def parse_tool_name(tool_info: dict | str) -> str:
    if isinstance(tool_info, dict):
        return tool_info.get("name", "unknown")
    return str(tool_info)


def clean_workspace():
    """清理 workspace 目录（保留 .gitkeep）。"""
    if WORKSPACE_DIR.exists():
        for item in WORKSPACE_DIR.iterdir():
            if item.name != ".gitkeep":
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)


def write_setup_files(task):
    """写入任务的前提文件。"""
    for path, content in task.setup_files.items():
        full = WORKSPACE_DIR / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")


def collect_trace_from_messages(messages: list) -> list[dict]:
    """从 agent 返回的 messages 中提取工具调用 trace。"""
    trace = []
    for msg in messages:
        # ToolMessage — 工具执行结果
        if hasattr(msg, "content") and hasattr(msg, "name"):
            trace.append({"name": msg.name, "args": {}, "result": str(msg.content)[:500]})
    return trace


async def run_task(
    task, thread_id: str, timeout: int, mode: str = "auto"
) -> tuple[str, list[dict], list[dict], int, int, float, str]:
    """运行单个任务。

    返回 (response, tool_calls_trace, conversation, step_count, token_usage,
         elapsed_sec, route)；`route` 是 auto 模式下的路由结论（simple/complex）。
    """
    start = time.time()

    try:
        response, tool_trace, conversation, step_count, token_usage, route = await asyncio.wait_for(
            run_single_task(task.prompt, thread_id=thread_id, mode=mode),
            timeout=timeout,
        )
        elapsed = time.time() - start
        return (
            response or "",
            tool_trace,
            conversation,
            step_count,
            token_usage,
            round(elapsed, 1),
            route,
        )

    except TimeoutError:
        elapsed = time.time() - start
        return "", [], [], 0, 0, round(elapsed, 1), ""
    except Exception as e:
        elapsed = time.time() - start
        return str(e)[:500], [], [], 0, 0, round(elapsed, 1), ""


def run_verifiers(task, context: EvalContext) -> list[dict]:
    """跑所有 verifier，返回结果列表。"""
    results = []
    for vf in task.verifiers:
        try:
            result: VerifierResult = vf(context)
            results.append(
                {
                    "name": result.name,
                    "passed": result.passed,
                    "score": result.score if result.score is not None else None,
                    "reason": result.reason,
                }
            )
        except Exception as e:
            results.append(
                {
                    "name": getattr(vf, "__name__", "unknown"),
                    "passed": None,
                    "score": None,
                    "reason": f"verifier error: {e}",
                }
            )
    return results


def compute_scores(verifier_results: list[dict]) -> tuple[float, float]:
    """根据 verifier 结果计算得分和通过率。skip 的不计入。"""
    scores = [v["score"] for v in verifier_results if v["score"] is not None]
    if not scores:
        return 0.0, 0.0
    avg_score = sum(scores) / len(scores)
    # 通过率：score >= 0.5 视为通过
    passed = sum(1 for s in scores if s >= 0.5)
    pass_rate = passed / len(scores) if scores else 0.0
    return round(avg_score, 3), round(pass_rate, 3)


def aggregate_dimensions(tasks_results: list[dict]) -> dict:
    """按维度聚合统计。"""
    dims: dict[str, list[float]] = {}
    for tr in tasks_results:
        dim = tr["dimension"]
        if dim not in dims:
            dims[dim] = []
        dims[dim].append(tr["score"])

    return {
        dim: {
            "score": round(sum(scores) / len(scores), 3) if scores else 0.0,
            "pass_rate": round(sum(1 for s in scores if s >= 0.5) / len(scores), 3)
            if scores
            else 0.0,
            "tasks": len(scores),
        }
        for dim, scores in dims.items()
    }


def load_existing_results(run_id: str) -> list[dict] | None:
    """加载已有结果（断点续跑）。"""
    path = RUNS_DIR / f"{run_id}.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("tasks", [])
    return None


def save_results_incremental(run_id: str, results: list[dict], meta: dict):
    """增量保存结果。"""
    path = RUNS_DIR / f"{run_id}.json"
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    output = {**meta, "tasks": results}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)


async def main_async(tasks: list, run_id: str, force: bool = False, mode: str = "auto"):
    # 检查断点续跑
    existing_results = None if force else load_existing_results(run_id)
    completed_ids = {r["id"] for r in existing_results} if existing_results else set()
    if completed_ids:
        print(
            f"断点续跑: 已跳过 {len(completed_ids)} 题, 剩余 {len(tasks) - len(completed_ids)} 题\n"
        )

    # 追踪失败的前置任务
    failed_ids: set[str] = set()
    results: list[dict] = existing_results or []

    for i, task in enumerate(tasks, 1):
        # 断点续跑：跳过已完成
        if task.id in completed_ids:
            continue

        # 前置任务检查
        if any(dep_id in failed_ids for dep_id in task.depends_on):
            print(f"[{i}/{len(tasks)}] {task.id} (SKIPPED — 前置失败)")
            results.append(
                {
                    "id": task.id,
                    "dimension": task.dimension,
                    "difficulty": task.difficulty,
                    "score": 0.0,
                    "verifiers": [],
                    "step_count": 0,
                    "tool_calls": 0,
                    "latency_sec": 0,
                    "token_usage": 0,
                    "status": "skipped",
                    "response": "",
                    "conversation": [],
                    "mode": mode,
                    "route": "",
                }
            )
            save_results_incremental(run_id, results, build_meta(run_id, results))
            continue

        # 清理 + 写前提文件
        # 有依赖的任务保留 workspace（依赖产物可能在前置任务生成），无依赖的清空
        if not task.depends_on:
            clean_workspace()
        write_setup_files(task)

        # thread_id 带 run-id 与 mode 前缀：
        # ⚠️ 阶段 1 接上 checkpointer 后，只有 task.id 会让 single / multi 两轮
        #    **共用同一批线程** → 第二轮读到第一轮的记忆，两套数字不可比。
        thread_id = f"{run_id}-{mode}-{task.id}"
        print(f"[{i}/{len(tasks)}] {task.id} [{task.dimension}] {task.prompt[:60]}...")

        (
            response,
            tool_trace,
            conversation,
            step_count,
            token_usage,
            elapsed,
            route,
        ) = await run_task(task, thread_id, task.timeout_sec, mode)

        # 判断状态
        if elapsed >= task.timeout_sec and not response:
            status = "timeout"
        elif not response or response.startswith("[ERROR]"):
            status = "error"
        else:
            status = "completed"

        # 跑 verifier
        ctx = EvalContext(
            task=task,
            response=response if status == "completed" else "",
            tool_calls=tool_trace,
            step_count=step_count,
            elapsed_sec=elapsed,
            workspace=WORKSPACE_DIR,
        )
        verifier_results = run_verifiers(task, ctx) if status == "completed" else []
        score, _ = compute_scores(verifier_results)

        result = {
            "id": task.id,
            "dimension": task.dimension,
            "difficulty": task.difficulty,
            "score": score,
            "verifiers": verifier_results,
            "step_count": step_count,
            "tool_calls": len(tool_trace),
            "latency_sec": elapsed,
            "token_usage": token_usage,
            "status": status,
            "response": response,
            "conversation": conversation,
            "mode": mode,
            "route": route,
        }
        results.append(result)

        icon = (
            "✅"
            if status == "completed"
            else "⏱️"
            if status == "timeout"
            else "❌"
            if status == "error"
            else "⏭️"
        )
        print(f"  {icon} {status} score={score} ({elapsed}s)")

        if status in ("error", "timeout"):
            failed_ids.add(task.id)

        # 增量保存
        save_results_incremental(run_id, results, build_meta(run_id, results))

    # 最终输出摘要
    print_summary(results, run_id)


def build_meta(run_id: str, results: list[dict]) -> dict:
    dims = aggregate_dimensions(results)
    scores = [r["score"] for r in results if r["status"] != "skipped"]
    overall_score = round(sum(scores) / len(scores), 3) if scores else 0.0
    pass_rate = round(sum(1 for s in scores if s >= 0.5) / len(scores), 3) if scores else 0.0
    latencies = [r["latency_sec"] for r in results if r["status"] == "completed"]
    steps = [r["step_count"] for r in results if r["status"] == "completed"]
    tool_calls_list = [r["tool_calls"] for r in results if r["status"] == "completed"]
    token_list = [r["token_usage"] for r in results if r["status"] == "completed"]

    return {
        "run_id": run_id,
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "git_commit": get_git_commit(),
        "model": MODEL_NAME,  # 不再硬编码：换模型后存档里的 model 字段会跟着变
        "mode": RUN_MODE,  # 本次运行用的执行模式（single / multi / auto）
        "evals_version": "v2.0",
        "total_tasks": len(results),
        "overall": {
            "score": overall_score,
            "pass_rate": pass_rate,
            "avg_steps": round(sum(steps) / len(steps), 1) if steps else 0,
            "avg_latency_sec": round(sum(latencies) / len(latencies), 1) if latencies else 0,
            "avg_tool_calls": round(sum(tool_calls_list) / len(tool_calls_list), 1)
            if tool_calls_list
            else 0,
            "total_token_usage": sum(token_list) if token_list else 0,
        },
        "by_dimension": dims,
    }


def print_summary(results: list[dict], run_id: str):
    meta = build_meta(run_id, results)
    print(f"\n{'=' * 55}")
    print(f"Run: {run_id}  |  {meta['total_tasks']} tasks  |  Overall: {meta['overall']['score']}")
    print(f"{'=' * 55}")
    for dim, data in meta["by_dimension"].items():
        print(f"  {dim:20s}  {data['score']:.2f}  ({data['tasks']} tasks)")
    print("\n  状态分布:")
    statuses = {}
    for r in results:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    for s, c in statuses.items():
        print(f"    {s}: {c}")

    # 保存到文件
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    output = {**meta, "tasks": results}
    out_path = RUNS_DIR / f"{run_id}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n  结果已保存: {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help=f"运行全部 {len(TASKS)} 题")
    parser.add_argument("--task", help="只运行指定任务 ID")
    parser.add_argument("--dimension", help="按维度运行")
    parser.add_argument("--force", action="store_true", help="忽略断点续跑，全部重跑")
    parser.add_argument("--run-id", default=None, help="自定义 run_id")
    parser.add_argument(
        "--mode",
        choices=["auto", "single", "multi"],
        default="auto",
        help="执行模式：auto=按复杂度自动路由（默认）；single=单 Agent；multi=完整三阶段",
    )
    parser.add_argument(
        "--role-models",
        default=None,
        help='临时覆盖各角色模型，如 "planner=ds-v4-flash,executor=ds-v41-flash"（不改配置文件）',
    )
    args = parser.parse_args()

    if args.task:
        selected = [t for t in TASKS if t.id == args.task]
    elif args.dimension:
        selected = get_tasks(args.dimension)
    elif args.all:
        # 按任务 ID 排序执行（E001→E030），显示与 ID 一一对应；
        # E014 depends_on E001，排序后 E001 先执行，依赖仍满足
        selected = sorted(TASKS, key=lambda t: t.id)
    else:
        print("用法: --all 运行全部 / --task E001 运行单个 / --dimension safety")
        sys.exit(1)

    run_id = args.run_id or f"eval_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    RUN_MODE = args.mode

    print(f"Run ID: {run_id}  |  {len(selected)} tasks  |  force={args.force}  |  mode={args.mode}")
    asyncio.run(main_async(selected, run_id, args.force, args.mode))
