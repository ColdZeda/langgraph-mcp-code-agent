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


def test_cli_survives_a_gbk_console(tmp_path):
    """**打印不能崩**（2026-09-24 实测踩到的真坑，回归守卫）。

    真实症状：删完 + VACUUM 之后，最后那行 `✅ 已删除…` 在 GBK 控制台上
    `UnicodeEncodeError` → **退出码非 0、吐 traceback**，看起来像"清理失败了"，
    实际早就成功 —— 这类"静默误导"比崩掉本身更坏（会让人重复劳动或误判环境）。

    做法：把 stdout 的编码钉死成 GBK（`PYTHONIOENCODING=gbk`，就是那台机器的控制台默认），
    再跑真进程，断言**退出码 0**。
    """
    import os
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    db = _make_db(tmp_path)

    env = {**os.environ, "PYTHONIOENCODING": "gbk"}
    proc = subprocess.run(
        [sys.executable, "evals/reset_eval_threads.py", "--yes", "--db", str(db)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )

    assert proc.returncode == 0, f"GBK 控制台下崩了：\n{proc.stdout}\n{proc.stderr}"
    assert "已删除" in proc.stdout or "eval" in proc.stdout
    # 功能也必须真的生效（不能为了"不崩"把删除吞掉）
    con = sqlite3.connect(db)
    assert (
        con.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id LIKE 'eval-%'").fetchone()[0]
        == 0
    )
    con.close()
