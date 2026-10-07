"""乙批 · 知识库接口（查看 / 单条删除）——给 Web 端「知识库」面板用。

用户提的需求原话：「你可以加个按钮然后点击可以进入一个看当前知识库的界面」+「带单条删除」。
触发背景：自动沉淀会自己往 `data/knowledge/` 写东西（实测一晚写了 3 条），而界面上什么都看不到，
错的结论沉淀进去也没法清。
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def kb(tmp_path, monkeypatch):
    """隔离出一个知识库目录 + 一个假的向量删除（不碰真 ChromaDB）。"""
    from app.code_agent.rag import store

    monkeypatch.setattr(store, "KNOWLEDGE_DIR", tmp_path / "knowledge")
    store.KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    (store.KNOWLEDGE_DIR / "MySQL MCP 服务地址.txt").write_text(
        "MySQL 的 MCP 服务在 127.0.0.1:3307，只读账号是 agent_readonly。", encoding="utf-8"
    )
    (store.KNOWLEDGE_DIR / "workspace 根目录路径.md").write_text(
        "# 笔记\n工作区在 runtime/workspace", encoding="utf-8"
    )

    deleted_sources: list[str] = []

    class _FakeCollection:
        def count(self) -> int:
            return 1 if deleted_sources else 2

        def delete(self, **kwargs):  # pragma: no cover - 兜底分支
            pass

    monkeypatch.setattr(store, "get_collection", lambda: _FakeCollection())
    monkeypatch.setattr(store, "_delete_source", lambda source: deleted_sources.append(source))
    return store, deleted_sources


@pytest.fixture
def client(monkeypatch, tmp_path):
    """⚠️ 必须把 `runtime.load` 打桩成空操作 —— 否则会真装 32 个工具、真加载 RAG 模型
    （实测这一份夹具就要 86 秒）。"""
    from app.web import server

    async def _no_load():
        return None

    monkeypatch.setattr(server, "SETTINGS_PATH", tmp_path / "web-settings.json")
    monkeypatch.setattr(server, "CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(server.runtime, "load", _no_load)
    monkeypatch.setattr(server.runtime, "rebuild_agents", lambda: None)
    with TestClient(server.app) as c:
        yield c


def test_list_knowledge_returns_entries(client, kb):
    res = client.get("/api/knowledge")

    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    names = [it["name"] for it in data["items"]]
    assert "MySQL MCP 服务地址.txt" in names
    assert "workspace 根目录路径.md" in names, "`.md` 也要列出来"
    first = data["items"][0]
    assert first["preview"], "每条要有预览（面板里直接看内容）"


def test_delete_knowledge_removes_file_and_vectors(client, kb):
    store, deleted_sources = kb

    res = client.request("DELETE", "/api/knowledge/MySQL MCP 服务地址.txt")

    assert res.status_code == 200 and res.json()["ok"] is True
    assert not (store.KNOWLEDGE_DIR / "MySQL MCP 服务地址.txt").exists(), "文件要真删掉"
    assert deleted_sources == ["MySQL MCP 服务地址.txt"], "🔴 向量也要删（否则检索会命中幽灵条目）"
    names = [it["name"] for it in res.json()["items"]]
    assert "MySQL MCP 服务地址.txt" not in names, "返回新的列表，前端不用再请求一次"


@pytest.mark.parametrize(
    "name",
    ["../web-settings.json", "..%2Fweb-settings.json", "sub/dir.txt", ".hidden.txt", "a.exe", ""],
)
def test_delete_knowledge_rejects_unsafe_names(client, kb, name):
    """🔴 删除接口只认**目录内的单层 .txt/.md**：路径穿越 / 其它扩展名一律拒绝。"""
    res = client.request("DELETE", f"/api/knowledge/{name}")

    # ⚠️ `../x` 会被 HTTP 层先归一化掉（`/api/knowledge/../x` → `/api/x`）⇒ 405：
    #    它**没能进到处理器**，同样是"被拒绝"（这条本来就是想证明"删不掉目录外的东西"）。
    assert res.status_code in (200, 404, 405), f"{name!r} 不该是别的结果"
    if res.status_code == 200:
        assert res.json().get("ok") is not True, f"{name!r} 不该被允许"
