"""`/api/sessions` 这一组接口的行为测试（阶段 7 · T7.5 会话管理）。

守的是一整条产品路径：**列表 → 改标题 → 置顶 → 软删除（进回收站）→ 恢复 → 彻底删除**，
另外两条"必须挡住"的：**正在被连接使用的会话不许删**（409）、
**系统线程默认不出现**（要 `include_eval=1` 才给）。

⚠️ 全程用 tmp 的 checkpoints 库与侧车库（`monkeypatch.setattr`），**不碰真实 runtime/**。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.web import server  # noqa: E402
from app.web import sessions as session_store  # noqa: E402

MINE = "9a11a0a1"
EVAL = "eval-v3-single-E001-single"
OTHER = "a700398c"


@pytest.fixture
def web(tmp_path, monkeypatch):
    """一个"只差 lifespan"的 Web 端：库全指到 tmp，工具/agent 不真的加载。"""

    async def _no_load() -> None:  # ⚠️ 必须是协程：lifespan 里是 `await runtime.load()`
        return None

    ckpt = tmp_path / "checkpoints.db"
    con = sqlite3.connect(str(ckpt))
    con.executescript(
        "CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id TEXT);"
        "CREATE TABLE writes (thread_id TEXT, checkpoint_id TEXT);"
    )
    rows = [
        (MINE, "1f1ba64f-0aaf-66e4-bfff-7328939eb6a1"),  # 较早
        (MINE, "1f1ba64f-0aaf-66e4-bfff-7328939eb6a2"),
        (EVAL, "1f1ba64f-0aaf-66e4-bfff-7328939eb6b1"),
        (OTHER, "1f1ba64f-0aaf-66e4-bfff-7328939eb6c1"),
    ]
    for tid, cid in rows:
        con.execute("INSERT INTO checkpoints VALUES (?, ?)", (tid, cid))
        con.execute("INSERT INTO writes VALUES (?, ?)", (tid, cid))
    con.commit()
    con.close()

    monkeypatch.setattr(server, "CHECKPOINT_DB", ckpt)
    monkeypatch.setattr(session_store, "SESSIONS_DB", tmp_path / "sessions.db")
    monkeypatch.setattr(server.runtime, "load", _no_load)
    monkeypatch.setattr(server.runtime, "rebuild_agents", lambda: None)
    # 活会话登记是**进程级**的：测完必须还原，免得污染其它测试
    monkeypatch.setattr(server, "_active_threads", {})
    return {"ckpt": ckpt, "tmp": tmp_path}


def _ids(payload: dict) -> list[str]:
    return [item["threadId"] for item in payload["items"]]


def test_list_hides_eval_threads_and_reports_counts(web):
    with TestClient(server.app) as c:
        data = c.get("/api/sessions").json()
    assert _ids(data) == sorted([MINE, OTHER], key=lambda t: t) or set(_ids(data)) == {MINE, OTHER}
    assert data["counts"]["eval"] == 1, "系统线程数量要如实回显（前端靠它决定要不要显示那行小字）"
    assert data["counts"]["hidden"] == 0

    with TestClient(server.app) as c:
        with_eval = c.get("/api/sessions", params={"include_eval": 1}).json()
    assert set(_ids(with_eval)) == {MINE, EVAL, OTHER}


def test_items_carry_title_and_flags(web):
    session_store.ensure_title(MINE, "深圳今天天气怎么样", web["tmp"] / "sessions.db")
    with TestClient(server.app) as c:
        data = c.get("/api/sessions").json()
    item = next(i for i in data["items"] if i["threadId"] == MINE)
    assert item["title"] == "深圳今天天气怎么样"
    assert item["titleSource"] == "auto"
    assert item["pinned"] is False
    assert item["system"] is False
    assert item["deletedAt"] is None
    assert item["checkpointCount"] == 2
    assert item["updatedAt"] is not None


def test_rename_then_auto_title_will_not_override(web):
    with TestClient(server.app) as c:
        assert c.post(f"/api/sessions/{MINE}/title", json={"title": "我的天气会话"}).json()["ok"]
        item = next(i for i in c.get("/api/sessions").json()["items"] if i["threadId"] == MINE)
    assert item["title"] == "我的天气会话"
    assert item["titleSource"] == "user"

    # 之后又来一条消息：自动标题不许盖掉用户起的名字
    assert session_store.ensure_title(MINE, "随便问点什么", web["tmp"] / "sessions.db") is None
    with TestClient(server.app) as c:
        item = next(i for i in c.get("/api/sessions").json()["items"] if i["threadId"] == MINE)
    assert item["title"] == "我的天气会话"

    with TestClient(server.app) as c:
        assert c.post(f"/api/sessions/{MINE}/title", json={"title": "   "}).status_code == 400


def test_pin_and_unpin_sort_order(web):
    with TestClient(server.app) as c:
        assert c.post(f"/api/sessions/{OTHER}/pin", json={"pinned": True}).json()["pinned"] is True
        data = c.get("/api/sessions").json()
        assert data["items"][0]["threadId"] == OTHER, "置顶的要排最前"
        assert data["items"][0]["pinned"] is True

        assert (
            c.post(f"/api/sessions/{OTHER}/pin", json={"pinned": False}).json()["pinned"] is False
        )
        assert c.get("/api/sessions").json()["items"][0]["threadId"] == MINE, "取消置顶后按时间排"


def test_soft_delete_hides_then_restore_brings_it_back(web):
    with TestClient(server.app) as c:
        assert c.delete(f"/api/sessions/{OTHER}").json()["hard"] is False
        data = c.get("/api/sessions").json()
        assert OTHER not in _ids(data), "软删除后不该出现在列表里"
        assert data["counts"]["hidden"] == 1

        # 回收站里能看到它
        hidden = c.get("/api/sessions", params={"include_hidden": 1}).json()
        assert OTHER in _ids(hidden)

        assert c.post(f"/api/sessions/{OTHER}/restore").json()["ok"] is True
        assert OTHER in _ids(c.get("/api/sessions").json())

    # 软删除**没有**动 checkpoint 行
    con = sqlite3.connect(str(web["ckpt"]))
    try:
        assert (
            con.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (OTHER,)
            ).fetchone()[0]
            == 1
        )
    finally:
        con.close()


def test_hard_delete_removes_checkpoint_rows(web):
    with TestClient(server.app) as c:
        result = c.delete(f"/api/sessions/{OTHER}", params={"hard": 1}).json()
    assert result["hard"] is True
    assert result["removed"] == {"writes": 1, "checkpoints": 1}
    con = sqlite3.connect(str(web["ckpt"]))
    try:
        assert (
            con.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (OTHER,)
            ).fetchone()[0]
            == 0
        )
        assert (
            con.execute("SELECT COUNT(*) FROM writes WHERE thread_id = ?", (OTHER,)).fetchone()[0]
            == 0
        )
        # 别人的会话一行都不许少
        assert (
            con.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (MINE,)).fetchone()[
                0
            ]
            == 2
        )
    finally:
        con.close()


def test_active_session_cannot_be_deleted(web):
    """正在被一条 WebSocket 使用的会话：软删与彻底删都要挡住（409），前端先切新会话。"""
    server.mark_thread_active(MINE, +1)
    try:
        with TestClient(server.app) as c:
            soft = c.delete(f"/api/sessions/{MINE}")
            hard = c.delete(f"/api/sessions/{MINE}", params={"hard": 1})
        assert soft.status_code == 409
        assert hard.status_code == 409
        assert "新会话" in soft.json()["detail"]
    finally:
        server.mark_thread_active(MINE, -1)

    with TestClient(server.app) as c:
        assert c.delete(f"/api/sessions/{MINE}").status_code == 200


def test_purge_system_threads_keeps_user_sessions(web):
    session_store.ensure_title(EVAL, "评估", web["tmp"] / "sessions.db")
    with TestClient(server.app) as c:
        result = c.post("/api/sessions/purge-system").json()
        assert result["threads"] == [EVAL]
        assert _ids(c.get("/api/sessions").json()) == [MINE, OTHER] or set(
            _ids(c.get("/api/sessions").json())
        ) == {MINE, OTHER}
    con = sqlite3.connect(str(web["ckpt"]))
    try:
        assert (
            con.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (EVAL,)).fetchone()[
                0
            ]
            == 0
        )
        assert (
            con.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (MINE,)).fetchone()[
                0
            ]
            == 2
        )
    finally:
        con.close()


def test_active_thread_registry_counts_and_releases():
    server.mark_thread_active("t-x", +1)
    server.mark_thread_active("t-x", +1)
    assert server.is_thread_active("t-x")
    server.mark_thread_active("t-x", -1)
    assert server.is_thread_active("t-x"), "还有一条连接占着 → 仍然算活"
    server.mark_thread_active("t-x", -1)
    assert not server.is_thread_active("t-x")
    server.mark_thread_active("", +1)  # 空 id 不该进登记表
    assert not server.is_thread_active("")
