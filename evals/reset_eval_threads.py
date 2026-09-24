"""清掉 `runtime/checkpoints.db` 里**评估自己**留下的会话线程（阶段 6 · 2026-09-24 新增）。

## 为什么需要它

评估的 `thread_id` 是 `eval-{run-id}-{题号}-{mode}`，而 `run_multi_agent` 会**从 checkpoint
里取回上一轮的对话当历史**：

```python
prior_turns = list(state.get("messages") or [])      # ← 就是这里
raw_input = [*prior_turns, HumanMessage(content=user_msg)]
```

⇒ **复用同一个 run-id 重跑**（比如再跑一轮 `v3-single`）= 模型看到
「上一轮这道题的题目 + 它自己给出的答案」→ 它大概率照抄
⇒ 测的就不是"能不能做"，而是"记不记得自己做过"，**整轮作废**。

⚠️ 顺带一个容易搞错的点：**把结果 JSON 挪去别处并不能解决这个问题** ——
文件同名只是"看起来撞"，真正的污染在 `checkpoints.db` 里。

## 安全边界（已实测）

只删 `thread_id LIKE 'eval-%'`：
- **评估线程**一律是这个前缀（probe / smoke / v3-single / 事故那轮…）；
- **Web / CLI 会话**是 `default` / 哈希 id / `probe-*` / `smoke6-1` 之类，**一个都不会被碰到**
  （实测一次：42 个 eval 线程 vs 15 个用户线程，零重叠）。

⚠️ 它**只删对话历史**：成绩早就写进 `runtime/runs/*.json` 了（正式的那份还会归档到 `docs/evidence/`）。

## 用法

```bash
uv run python evals/reset_eval_threads.py            # 只报告，不删（默认）
uv run python evals/reset_eval_threads.py --yes      # 真删 + VACUUM
```
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ⚠️ Windows 控制台默认是 GBK，而本脚本要打 ✅ / 中文 —— **不统一成 UTF-8 会直接崩**。
# 2026-09-24 实测踩到：删完并 VACUUM 之后**崩在最后一行**（`UnicodeEncodeError: 'gbk' codec
# can't encode character '\u2705'`）⇒ 看起来像"清理失败了"，实际早就成功了 —— 典型"静默误导"。
# 与 `scripts/probe_mcp_server.py` / `agent/code_agent.py` 用同一种做法；
# 多加 `errors="replace"`：控制台认不了的字降级成 `?`，绝不因为打印而改变退出码。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app.code_agent.config import RUNTIME_DIR  # noqa: E402

DB_PATH = RUNTIME_DIR / "checkpoints.db"
EVAL_PREFIX = "eval-%"
TABLES = ("writes", "checkpoints")  # ⚠️ 先删 writes：它引用 checkpoint


def _count(con: sqlite3.Connection, table: str) -> int:
    return int(
        con.execute(
            f"SELECT COUNT(*) FROM {table} WHERE thread_id LIKE ?", (EVAL_PREFIX,)
        ).fetchone()[0]
    )


def _threads(con: sqlite3.Connection) -> list[str]:
    return sorted(
        r[0] for r in con.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
    )


def reset(db_path: Path = DB_PATH, *, apply: bool = False) -> dict:
    """报告（并可选执行）评估线程清理。返回 `{eval_threads, eval_rows, kept_threads, applied}`。"""
    if not db_path.exists():
        return {"eval_threads": 0, "eval_rows": 0, "kept_threads": [], "applied": False}

    con = sqlite3.connect(f"file:{db_path}", uri=True, timeout=15)
    try:
        for table in TABLES:
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
            if "thread_id" not in cols:
                raise RuntimeError(f"{table} 表里没有 thread_id —— 库结构变了，别乱删")

        eval_rows = sum(_count(con, t) for t in TABLES)
        eval_threads = sorted(
            {
                r[0]
                for r in con.execute(
                    "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE ?",
                    (EVAL_PREFIX,),
                )
            }
        )
        kept = [t for t in _threads(con) if not t.startswith("eval-")]

        if apply and eval_threads:
            for table in TABLES:
                con.execute(f"DELETE FROM {table} WHERE thread_id LIKE ?", (EVAL_PREFIX,))
            con.commit()
            con.execute("VACUUM")

        return {
            "eval_threads": len(eval_threads),
            "eval_rows": eval_rows,
            "kept_threads": kept,
            "applied": bool(apply and eval_threads),
        }
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="清掉 checkpoints.db 里的评估线程（别删用户会话）")
    parser.add_argument("--yes", action="store_true", help="真的执行删除（默认只报告）")
    parser.add_argument("--db", default=str(DB_PATH), help="db 路径（测试用）")
    args = parser.parse_args()

    info = reset(Path(args.db), apply=args.yes)
    print(f"库：{args.db}")
    print(f"  eval-* 线程：{info['eval_threads']} 个（{info['eval_rows']} 行，两张表合计）")
    print(f"  保留的用户会话：{len(info['kept_threads'])} 个 → {info['kept_threads']}")
    if info["applied"]:
        print("  ✅ 已删除并 VACUUM —— 现在可以复用同名 run-id 重跑了")
    elif info["eval_threads"]:
        print("  （只报告，没删。要真删请加 --yes）")
    else:
        print("  ✅ 库里没有评估线程，什么都不用做")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
