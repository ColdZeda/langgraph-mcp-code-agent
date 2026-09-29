"""会话元数据侧车库（阶段 7 · T7.5 会话管理）。

**为什么单开一个库，而不是塞进 `runtime/checkpoints.db`**：
那份库是 langgraph 的 `AsyncSqliteSaver` 在管（它只认自己的 `checkpoints` / `writes` 两张表），
我们的"标题 / 置顶 / 回收站"是**应用层的东西**。混进去的代价是：两边的写抢同一把 sqlite 锁，
出问题时分不清是谁写的。分开放之后，备份、清空、删文件都互不影响。

**三条语义（用户 2026-09-27 决定）**：
1. **删除永远是"先软删除"**：只把 `deleted_at` 置上 → 会话从列表消失、进"回收站"；
   回收站里可以「恢复」，也可以「彻底删除」（那时才真删 `checkpoints`/`writes` 的行）。
2. **标题来自新会话的第一条用户消息**（首行、压平空白、截断 `TITLE_MAX`）。
   ⚠️ **用户改过的标题永不被自动覆盖**（`title_source='user'`）。
3. **置顶**：`pinned` 排最前，可取消。

⚠️ 这条库是**运行期数据**：`runtime/` 已被 gitignore，它不会进仓库。
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from app.code_agent.config import RUNTIME_DIR

#: 侧车库路径（可用环境变量覆盖 —— 单测就靠它指到 tmp）
SESSIONS_DB = Path(os.getenv("CODE_AGENT_SESSIONS_DB", RUNTIME_DIR / "sessions.db"))

#: 标题最大长度（按字符数；超长由前端用 CSS 省略号收尾）
TITLE_MAX = 24

#: **系统线程**的前缀：评估 / 探针 / 冒烟脚本造出来的会话，不是用户自己的对话。
#: 列表默认不显示它们（`include_eval=1` 才带上），也可以「一键清空」。
SYSTEM_THREAD_PREFIXES = ("eval-", "probe-", "probe_", "smoke", "nowrap-")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_meta (
    thread_id    TEXT PRIMARY KEY,
    title        TEXT,
    title_source TEXT NOT NULL DEFAULT 'auto',   -- auto | user
    created_at   REAL,
    updated_at   REAL,
    deleted_at   REAL,                            -- NULL = 正常；非 NULL = 在回收站
    pinned       INTEGER NOT NULL DEFAULT 0
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """开一个连接（会建表）。WAL + busy_timeout：与 checkpointer 各写各的文件，仍留足余量。"""
    path = Path(db_path or SESSIONS_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.executescript(_SCHEMA)
    return con


def is_system_thread(thread_id: str) -> bool:
    """是不是脚本造出来的会话（评估 / 探针 / 冒烟）。"""
    tid = (thread_id or "").strip().lower()
    return any(tid.startswith(prefix) for prefix in SYSTEM_THREAD_PREFIXES)


def derive_title(text: str) -> str:
    """从用户的第一条消息里取标题：**首行**、压平空白、按 `TITLE_MAX` 截断。"""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    flat = " ".join(lines[0].split())
    return flat[:TITLE_MAX].strip()


def ensure_title(thread_id: str, text: str, db_path: Path | None = None) -> str | None:
    """新会话收到第一条消息时写标题。

    - 已经有标题（无论是自动还是用户改的）→ **不动**；
    - 用户改过的（`title_source='user'`）→ **永不覆盖**；
    - 没有行 → 插一条；
    - 有行但标题为空（例如先被手工建过 meta）→ 补上，但仍保持原 source。
    """
    title = derive_title(text)
    if not title:
        return None
    now = time.time()
    with connect(db_path) as con:
        row = con.execute(
            "SELECT title, title_source FROM session_meta WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO session_meta (thread_id, title, title_source, created_at, updated_at)"
                " VALUES (?, ?, 'auto', ?, ?)",
                (thread_id, title, now, now),
            )
            return title
        if not (row["title"] or "").strip() and row["title_source"] != "user":
            con.execute(
                "UPDATE session_meta SET title = ?, updated_at = ? WHERE thread_id = ?",
                (title, now, thread_id),
            )
            return title
    return None


def rename(thread_id: str, title: str, db_path: Path | None = None) -> str:
    """用户改标题：`title_source` 置为 `user`（从此自动标题不再覆盖它）。"""
    clean = " ".join((title or "").split())[:TITLE_MAX]
    if not clean:
        raise ValueError("标题不能为空")
    now = time.time()
    with connect(db_path) as con:
        con.execute(
            "INSERT INTO session_meta (thread_id, title, title_source, created_at, updated_at)"
            " VALUES (?, ?, 'user', ?, ?)"
            " ON CONFLICT(thread_id) DO UPDATE SET title = excluded.title,"
            " title_source = 'user', updated_at = excluded.updated_at",
            (thread_id, clean, now, now),
        )
    return clean


def _ensure_row(con: sqlite3.Connection, thread_id: str, now: float) -> None:
    con.execute(
        "INSERT OR IGNORE INTO session_meta (thread_id, title, title_source, created_at, updated_at)"
        " VALUES (?, NULL, 'auto', ?, ?)",
        (thread_id, now, now),
    )


def soft_delete(thread_id: str, db_path: Path | None = None) -> None:
    """软删除：进回收站（只动侧车库，**不碰** checkpoints）。"""
    now = time.time()
    with connect(db_path) as con:
        _ensure_row(con, thread_id, now)
        con.execute(
            "UPDATE session_meta SET deleted_at = ?, updated_at = ? WHERE thread_id = ?",
            (now, now, thread_id),
        )


def restore(thread_id: str, db_path: Path | None = None) -> None:
    """从回收站恢复。"""
    now = time.time()
    with connect(db_path) as con:
        _ensure_row(con, thread_id, now)
        con.execute(
            "UPDATE session_meta SET deleted_at = NULL, updated_at = ? WHERE thread_id = ?",
            (now, thread_id),
        )


def set_pinned(thread_id: str, pinned: bool, db_path: Path | None = None) -> None:
    now = time.time()
    with connect(db_path) as con:
        _ensure_row(con, thread_id, now)
        con.execute(
            "UPDATE session_meta SET pinned = ?, updated_at = ? WHERE thread_id = ?",
            (1 if pinned else 0, now, thread_id),
        )


def drop_meta(thread_id: str, db_path: Path | None = None) -> None:
    """删掉侧车行的**唯一**出口（彻底删除会话时用）。"""
    with connect(db_path) as con:
        con.execute("DELETE FROM session_meta WHERE thread_id = ?", (thread_id,))


def all_meta(db_path: Path | None = None) -> dict[str, dict]:
    """一次性取出全部元数据：`{thread_id: {...}}`。

    为什么整表取：会话数量是"本地单用户"级别（几十条），一次读比逐条查省事，
    而且 `/api/sessions` 本来就要按它排序/过滤。
    """
    with connect(db_path) as con:
        rows = con.execute("SELECT * FROM session_meta").fetchall()
    return {
        row["thread_id"]: {
            "title": row["title"],
            "titleSource": row["title_source"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "deletedAt": row["deleted_at"],
            "pinned": bool(row["pinned"]),
        }
        for row in rows
    }


# ── 彻底删除：真删 checkpoint 行 ─────────────────────────────────────
# ⚠️ 顺序固定 `writes` → `checkpoints`：`writes` 引用 `checkpoint`。
#    这个顺序与 `evals/reset_eval_threads.py` 保持一致（那边删的是 `eval-%` 前缀）。
CHECKPOINT_TABLES = ("writes", "checkpoints")


def delete_thread_rows(thread_id: str, checkpoint_db: Path | None = None) -> dict[str, int]:
    """把某个会话的 checkpoint 行真正删掉（**不可逆**：那个 thread 的跨轮记忆就此消失）。

    ⚠️ `checkpoint_db` 建议**显式传**（`server.py` 就把自己的 `CHECKPOINT_DB` 传进来）：
    这个函数是删除动作，绝不能因为"没传参"就落到别的库上去 —— 单测也靠这个参数指到 tmp。
    返回 `{表名: 删了几行}`，供接口回显与测试断言。
    """
    from app.code_agent.config import CHECKPOINT_DB

    db = Path(checkpoint_db or CHECKPOINT_DB)
    if not db.exists():
        return {}
    removed: dict[str, int] = {}
    con = sqlite3.connect(str(db), timeout=5)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        existing = {
            row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in CHECKPOINT_TABLES:
            if table not in existing:
                continue
            cur = con.execute(f"DELETE FROM {table} WHERE thread_id = ?", (thread_id,))  # noqa: S608
            removed[table] = cur.rowcount
        con.commit()
    finally:
        con.close()
    return removed


def purge_system_threads(checkpoint_db: Path | None = None, db_path: Path | None = None) -> dict:
    """一键清空**系统线程**（评估 / 探针 / 冒烟）：checkpoint 行 + 侧车行一起删。

    只删 `SYSTEM_THREAD_PREFIXES` 命中的 thread_id —— 用户自己的会话一条都不动。
    """
    from app.code_agent.config import CHECKPOINT_DB

    db = Path(checkpoint_db or CHECKPOINT_DB)
    removed_threads: list[str] = []
    if db.exists():
        con = sqlite3.connect(str(db), timeout=5)
        try:
            con.execute("PRAGMA busy_timeout=5000")
            rows = con.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
            for (tid,) in rows:
                if is_system_thread(tid):
                    removed_threads.append(tid)
        finally:
            con.close()
    for tid in removed_threads:
        delete_thread_rows(tid, db)
        drop_meta(tid, db_path)
    return {"threads": sorted(removed_threads), "count": len(removed_threads)}
