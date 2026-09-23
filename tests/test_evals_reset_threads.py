"""`evals/reset_eval_threads.py` 的测试。

它守的是一个**会让整轮作废**的坑：`thread_id` = `eval-{run-id}-{题号}-{mode}`，
而 `run_multi_agent` 会从 checkpoint 取回上一轮对话当历史
⇒ **复用同一个 run-id 重跑 = 模型看到自己上次的答案**（大概率照抄）。
所以清理脚本必须：① 只删 `eval-*`；② 绝不碰用户会话。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import reset_eval_threads as R  # noqa: E402


def _make_db(tmp_path: Path) -> Path:
    """造一个只有两张表的最小库：混着 eval 线程与用户会话。"""
    db = tmp_path / "checkpoints.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE checkpoints (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT)")
    con.execute("CREATE TABLE writes (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT)")
    rows = [
        ("eval-v3-single-E001-single", 3),
        ("eval-smoke1-E007-single", 2),
        ("default", 5),
        ("29a968cb", 2),
        ("probe-memory-restart", 4),
        ("smoke6-1", 1),
    ]
    for tid, n in rows:
        for i in range(n):
            con.execute("INSERT INTO checkpoints VALUES (?, '', ?)", (tid, f"c{i}"))
            con.execute("INSERT INTO writes VALUES (?, '', ?)", (tid, f"c{i}"))
    con.commit()
    con.close()
    return db


def test_dry_run_reports_without_deleting(tmp_path):
    db = _make_db(tmp_path)

    info = R.reset(db, apply=False)

    assert info["eval_threads"] == 2
    assert info["eval_rows"] == 10  # (3+2) 行 × 两张表
    assert info["applied"] is False
    assert sorted(info["kept_threads"]) == [
        "29a968cb",
        "default",
        "probe-memory-restart",
        "smoke6-1",
    ]

    # 只报告：库里必须一个字都没变
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 17
    con.close()


def test_apply_removes_only_eval_threads(tmp_path):
    db = _make_db(tmp_path)

    info = R.reset(db, apply=True)

    assert info["applied"] is True
    assert sorted(info["kept_threads"]) == [
        "29a968cb",
        "default",
        "probe-memory-restart",
        "smoke6-1",
    ]

    con = sqlite3.connect(db)
    left = sorted(r[0] for r in con.execute("SELECT DISTINCT thread_id FROM checkpoints"))
    assert left == ["29a968cb", "default", "probe-memory-restart", "smoke6-1"], "用户会话被误删了！"
    assert con.execute("SELECT COUNT(*) FROM writes").fetchone()[0] == 12, "writes 里的用户行也没了"
    con.close()


def test_missing_db_is_not_an_error(tmp_path):
    info = R.reset(tmp_path / "nope.db", apply=True)
    assert info == {"eval_threads": 0, "eval_rows": 0, "kept_threads": [], "applied": False}


def test_schema_change_is_refused(tmp_path):
    """库结构变了就该报错，而不是对着不存在的列瞎删。"""
    db = tmp_path / "weird.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE checkpoints (id TEXT)")
    con.execute("CREATE TABLE writes (id TEXT)")
    con.commit()
    con.close()

    try:
        R.reset(db, apply=True)
    except RuntimeError as exc:
        assert "thread_id" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("结构不对时应该抛 RuntimeError")
