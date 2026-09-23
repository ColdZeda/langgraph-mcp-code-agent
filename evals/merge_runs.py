"""逐题跑出来的结果 → 合并成**一轮**的结果文件（阶段 6 · 2026-09-24 新增）。

## 为什么需要它

原来一轮 30 题是**一次进程跑完**，只在整轮结束时写一次 JSON ——
于是"第 16 题把评估进程杀了"就等于**整轮白跑**（实测踩过：E016 无差别杀 python，
评估进程和 6 个 MCP 子进程一起没了，前 15 题的成绩全部丢失）。

改成**一题一次进程**之后，每题各自落盘一份 JSON ⇒ **天然的增量保存**：
即使某一题把宿主搞死了，前面已完成的题**数据还在**。

代价是结果散成 30 个文件，而 `evals/report.py` 只吃"一轮一个文件"。
本脚本就是把它们合回去 —— 而且**复用 `runner._aggregate` / `runner._by_dimension`**，
保证合并后的口径与整轮跑**完全一致**（不是另写一套算法）。

## 用法

```bash
# 一题一次：run-id 用 "<轮次前缀>-<题号>"，例如 v3-single-E001
uv run python evals/run_e2e.py --task E001 --mode single --run-id v3-single-E001

# 30 题跑完后合并（缺题会**拒绝写出**，避免把残轮当成整轮）
uv run python evals/merge_runs.py --prefix v3-single --mode single --archive
```

⚠️ **缺题默认拒绝**：必须显式给 `--allow-partial` 才允许写出不完整的一轮 ——
"残轮"和"整轮"在文件名上长得一样，靠人记是靠不住的。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.config import PROJECT_ROOT, RUNS_DIR  # noqa: E402
from evals import runner as R  # noqa: E402
from evals.tasks import TASKS  # noqa: E402


def _load(paths: list[Path]) -> list[dict]:
    """读出每个文件里的**那一题**记录（每份 JSON 的 `tasks` 只有一个元素）。"""
    records: list[dict] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for record in payload.get("tasks") or []:
            record = dict(record)
            record["_source"] = path.name
            records.append(record)
    return records


def _dedupe(records: list[dict]) -> tuple[list[dict], list[str]]:
    """同一题有多份分片时只保留**最新的那份**（按源文件修改时间），并报告重复的题号。"""
    counts: dict[str, int] = {}
    latest: dict[str, tuple[float, dict]] = {}
    for record in records:
        tid = record["id"]
        counts[tid] = counts.get(tid, 0) + 1
        path = RUNS_DIR / record["_source"]
        mtime = path.stat().st_mtime if path.exists() else 0.0
        if tid not in latest or mtime >= latest[tid][0]:
            latest[tid] = (mtime, record)
    kept = [rec for _mt, rec in sorted(latest.values(), key=lambda pair: pair[1]["id"])]
    return kept, sorted(t for t, n in counts.items() if n > 1)


def main() -> int:
    parser = argparse.ArgumentParser(description="把逐题跑的结果合并成一轮")
    parser.add_argument(
        "--prefix", required=True, help="run-id 前缀，如 v3-single（对应 v3-single-E001）"
    )
    parser.add_argument("--mode", required=True, choices=["single", "multi", "auto"])
    parser.add_argument("--out", default=None, help="输出路径（默认 runtime/runs/<prefix>.json）")
    parser.add_argument("--archive", action="store_true", help="另复制一份到 docs/evidence/")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="允许写出**不完整**的一轮（默认缺题就拒绝，避免把残轮当整轮）",
    )
    args = parser.parse_args()

    pattern = f"{args.prefix}-E*.json"
    paths = sorted(RUNS_DIR.glob(pattern))
    if not paths:
        print(f"❌ 没找到任何分片：{RUNS_DIR / pattern}")
        return 1

    records = _load(paths)
    records, dupes = _dedupe(records)

    # ── 校验：模式一致 / 是同一轮 / 缺了哪些题 ──
    modes = {r.get("mode") for r in records}
    if modes != {args.mode}:
        print(f"❌ 分片里的模式不一致：{sorted(modes)}（--mode 传的是 {args.mode}）")
        return 1
    got = {r["id"] for r in records}
    expected = [t.id for t in TASKS]
    missing = [t for t in expected if t not in got]
    extra = sorted(got - set(expected))
    if extra:
        print(f"⚠️ 有不在题集里的题号：{extra}")

    print(f"分片文件 {len(paths)} 个 → 题目 {len(records)} 道")
    if dupes:
        print(f"⚠️ 这些题有多份分片，已按文件修改时间取最新：{dupes}")
    if missing:
        print(f"⚠️ 缺 {len(missing)} 道题：{'、'.join(missing)}")
        if not args.allow_partial:
            print("❌ 拒绝写出不完整的一轮。确认就要残轮的话，加 --allow-partial。")
            return 1

    for record in records:
        record.pop("_source", None)

    # ── 口径与整轮跑**完全一致**：直接复用 runner 里那两个函数 ──
    first = json.loads(paths[0].read_text(encoding="utf-8"))
    payload = {
        "run_id": args.prefix,
        "mode": args.mode,
        "started_at": first.get("started_at", ""),
        "wall_sec": round(
            sum(
                float(json.loads(p.read_text(encoding="utf-8")).get("wall_sec") or 0) for p in paths
            ),
            1,
        ),
        "env": first.get("env", {}),
        "models": first.get("models", {}),
        "totals": R._aggregate(records),
        "by_dimension": R._by_dimension(records),
        "tasks": records,
        # ── 合并来源（别让读报告的人以为这是"一次进程跑完的"）──
        "merged_from": {
            "prefix": args.prefix,
            "shards": len(paths),
            "missing_tasks": missing,
            "partial": bool(missing),
            "note": "本轮为逐题分片跑（一题一进程）后合并；口径复用 runner._aggregate。",
            "merged_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
    }

    out = Path(args.out) if args.out else RUNS_DIR / f"{args.prefix}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    totals = payload["totals"]
    print(
        f"✅ 已合并：{out}\n"
        f"   通过 {totals['passed']}/{totals['available']}"
        f"（通过率 {totals['pass_rate']}）；未测 {totals['unavailable']}；"
        f"超时 {totals['timeout']}；异常 {totals['error']}"
    )

    if args.archive:
        dest = PROJECT_ROOT / "docs" / "evidence" / out.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out, dest)
        print(f"已归档到版本控制目录: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
