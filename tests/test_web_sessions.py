"""会话管理侧车库的单元测试（阶段 7 · T7.5）。

守四件事：
1. **标题只写一次**，且**永不被自动标题覆盖用户改过的**（`title_source='user'`）；
2. **删除永远是先软删除**：只动侧车库，`checkpoints` 一行都不许少；
3. **彻底删除**才真删 checkpoint 行，且顺序是 `writes` → `checkpoints`（`writes` 引用 checkpoint）；
4. **系统线程**（`eval-` / `probe-` / `smoke` / `nowrap-`）能识别、能一键清空，**用户会话一条都不动**。

⚠️ 全部用 tmp 库：`db_path=` 指到 tmp；checkpoint 那边用 `monkeypatch.setattr(config, "CHECKPOINT_DB", …)`。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent import config  # noqa: E402
from app.web import sessions as S  # noqa: E402

# ── 夹具 ────────────────────────────────────────────────────────────


@pytest.fixture
def side_db(tmp_path) -> Path:
    return tmp_path / "sessions.db"


@pytest.fixture
def fake_checkpoints(tmp_path, monkeypatch) -> Path:
    """造一个"只有两张表"的假 checkpoints 库，塞两个会话的数据。"""
    db = tmp_path / "checkpoints.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        "CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id TEXT);"
        "CREATE TABLE writes (thread_id TEXT, checkpoint_id TEXT);"
    )
    for tid in ("mine-1", "eval-v3-single-E001-single"):
        con.execute("INSERT INTO checkpoints VALUES (?, 'c1')", (tid,))
        con.execute("INSERT INTO checkpoints VALUES (?, 'c2')", (tid,))
        con.execute("INSERT INTO writes VALUES (?, 'c1')", (tid,))
    con.commit()
    con.close()
    monkeypatch.setattr(config, "CHECKPOINT_DB", db)
    return db


def _count(db: Path, table: str, thread_id: str | None = None) -> int:
    con = sqlite3.connect(str(db))
    try:
        if thread_id is None:
            return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
        return con.execute(
            f"SELECT COUNT(*) FROM {table} WHERE thread_id = ?",
            (thread_id,),  # noqa: S608
        ).fetchone()[0]
    finally:
        con.close()


# ── 标题 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("深圳今天天气怎么样", "深圳今天天气怎么样"),
        ("  读一下当前目录  ", "读一下当前目录"),
        ("第一行\n第二行", "第一行"),
        ("多   个    空格", "多 个 空格"),
        ("", ""),
        ("   \n  \n", ""),
        ("あ" * 40, "あ" * S.TITLE_MAX),
    ],
)
def test_derive_title(text, expected):
    assert S.derive_title(text) == expected


def test_ensure_title_writes_once_and_never_overwrites(side_db):
    assert S.ensure_title("t1", "深圳今天天气怎么样", side_db) == "深圳今天天气怎么样"
    # 第二次来消息：不覆盖
    assert S.ensure_title("t1", "再问一句", side_db) is None
    assert S.all_meta(side_db)["t1"]["title"] == "深圳今天天气怎么样"


def test_ensure_title_does_not_touch_user_title(side_db):
    S.rename("t1", "我自己起的名字", side_db)
    assert S.ensure_title("t1", "随便说点什么", side_db) is None
    meta = S.all_meta(side_db)["t1"]
    assert meta["title"] == "我自己起的名字"
    assert meta["titleSource"] == "user"


def test_ensure_title_ignores_empty_text(side_db):
    assert S.ensure_title("t1", "   \n ", side_db) is None
    assert "t1" not in S.all_meta(side_db)


def test_rename_truncates_and_rejects_empty(side_db):
    assert S.rename("t1", "x" * 40, side_db) == "x" * S.TITLE_MAX
    with pytest.raises(ValueError):
        S.rename("t1", "   ", side_db)


# ── 软删除 / 恢复 / 置顶 ─────────────────────────────────────────────


def test_soft_delete_then_restore(side_db):
    S.soft_delete("t1", side_db)
    assert S.all_meta(side_db)["t1"]["deletedAt"] is not None
    S.restore("t1", side_db)
    assert S.all_meta(side_db)["t1"]["deletedAt"] is None


def test_soft_delete_does_not_touch_checkpoints(side_db, fake_checkpoints):
    before = _count(fake_checkpoints, "checkpoints", "mine-1")
    S.soft_delete("mine-1", side_db)
    assert _count(fake_checkpoints, "checkpoints", "mine-1") == before, "软删除不许动 checkpoint"


def test_set_pinned_toggles(side_db):
    S.set_pinned("t1", True, side_db)
    assert S.all_meta(side_db)["t1"]["pinned"] is True
    S.set_pinned("t1", False, side_db)
    assert S.all_meta(side_db)["t1"]["pinned"] is False


# ── 系统线程 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tid", "expected"),
    [
        ("eval-v3-single-E001-single", True),
        ("probe-896793da", True),
        ("smoke-1", True),
        ("nowrap-probe", True),
        ("9a11a0a1", False),
        ("default", False),
        ("我的会话", False),
    ],
)
def test_is_system_thread(tid, expected):
    assert S.is_system_thread(tid) is expected


def test_delete_thread_rows_removes_only_that_thread(side_db, fake_checkpoints):
    removed = S.delete_thread_rows("mine-1", fake_checkpoints)
    assert removed == {"writes": 1, "checkpoints": 2}
    assert _count(fake_checkpoints, "checkpoints", "mine-1") == 0
    assert _count(fake_checkpoints, "writes", "mine-1") == 0
    # 另一个会话（eval-*）不受影响
    assert _count(fake_checkpoints, "checkpoints", "eval-v3-single-E001-single") == 2


def test_delete_thread_rows_on_missing_db_is_noop(tmp_path):
    assert S.delete_thread_rows("t1", tmp_path / "nope.db") == {}


def test_purge_system_threads_keeps_user_sessions(side_db, fake_checkpoints):
    S.ensure_title("eval-v3-single-E001-single", "评估题", side_db)
    S.ensure_title("mine-1", "我的会话", side_db)

    result = S.purge_system_threads(fake_checkpoints, side_db)

    assert result["threads"] == ["eval-v3-single-E001-single"]
    assert result["count"] == 1
    # eval-* 的 checkpoint 行与侧车行都没了
    assert _count(fake_checkpoints, "checkpoints", "eval-v3-single-E001-single") == 0
    assert "eval-v3-single-E001-single" not in S.all_meta(side_db)
    # 用户会话原封不动
    assert _count(fake_checkpoints, "checkpoints", "mine-1") == 2
    assert S.all_meta(side_db)["mine-1"]["title"] == "我的会话"


def test_all_meta_shape(side_db):
    S.ensure_title("t1", "标题", side_db)
    S.set_pinned("t1", True, side_db)
    meta = S.all_meta(side_db)["t1"]
    assert set(meta) == {
        "title",
        "titleSource",
        "createdAt",
        "updatedAt",
        "deletedAt",
        "pinned",
    }
