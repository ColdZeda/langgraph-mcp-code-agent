"""阶段 7 · T7.6 的三条回归守卫（"还没有可用模型"不再致命 + 第一个模型自动接管四个角色）。

守的是**面向用户的那条路径**：全新用户不配 `.env` → 起服务（不能崩）→ 看到"添加你的 Key" →
填完保存 → 四个角色都指向它 → 开聊。任何一环回退，这里就红。

1. `import app.code_agent.model.llm` **不再要求 key**（模块级那句 `llm = get_llm()` 已惰性化）；
2. `AgentRuntime.rebuild_agents()` 缺 key 时**不抛**，只把 agent 置空 + 记 `model_error`；
3. 用户加的**第一个**自定义模型 ⇒ 四个角色一起指过去；**第二个**不许改动已有角色。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.model import llm as llm_mod  # noqa: E402
from app.web import server  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── 1. import 期不许建 LLM ──────────────────────────────────────────


def test_llm_module_has_no_module_level_instantiation():
    """源码级守卫：模块里不许再出现 `llm = get_llm()` 这种**import 期**建对象的写法。

    为什么要用源码级断言：真去"没有 key 的环境里 import 一次"要在子进程里做，
    而这条不变量的破坏方式恰恰是**一行**代码 —— 源码检查最直接、最快、也最难被绕过。
    （同款做法见 `tests/test_mcp_tool_lifecycle.py` 对 RAG 那句 import 的守卫。）
    """
    source = (REPO_ROOT / "app" / "code_agent" / "model" / "llm.py").read_text(encoding="utf-8")
    # ⚠️ 只盯"建**客户端**"的那种（`get_llm()` / `build_llm()`）——
    #    `registry = LLMRegistry()` 是**故意**留着的：它只读 `models.json`，不要 key。
    offenders = [
        line
        for line in source.splitlines()
        if line.startswith(("llm = ", "registry = "))
        and ("get_llm(" in line or "build_llm(" in line)
    ]
    assert offenders == [], (
        f"llm.py 里又有模块级建对象的语句了：{offenders} —— "
        "它会让 import 阶段就要求 key ⇒ 没配 key 时服务起不来、pytest 收集阶段也会炸"
    )
    assert "def __getattr__" in source, "惰性别名（PEP 562）不见了"


def test_lazy_alias_and_unknown_attribute(monkeypatch):
    """`llm` 这个名字仍然可用（惰性取），别的名字照旧 AttributeError。"""
    sentinel = object()
    monkeypatch.setattr(llm_mod, "get_llm", lambda *a, **k: sentinel)
    assert llm_mod.llm is sentinel
    with pytest.raises(AttributeError):
        _ = llm_mod.this_name_does_not_exist


# ── 2. 缺 key 时重建 agent 不致命 ────────────────────────────────────


def test_rebuild_agents_tolerates_missing_key(monkeypatch):
    rt = server.AgentRuntime()

    def boom(_tools):
        raise ValueError("模型 API key 未配置，请在 .env 中设置 MODEL_API_KEY")

    monkeypatch.setattr(server, "build_executor_agent", boom)
    rt.rebuild_agents()
    assert rt.executor_agent is None and rt.verifier_agent is None
    assert "key" in rt.model_error

    monkeypatch.setattr(server, "build_executor_agent", lambda _tools: "EXEC")
    monkeypatch.setattr(server, "build_verifier_agent", lambda _tools: "VERIFY")
    rt.rebuild_agents()
    assert (rt.executor_agent, rt.verifier_agent) == ("EXEC", "VERIFY")
    assert rt.model_error == ""


# ── 3. 第一个自定义模型自动接管四个角色 ──────────────────────────────


@pytest.fixture
def web(tmp_path, monkeypatch):
    async def _no_load() -> None:
        return None

    monkeypatch.setattr(server, "SETTINGS_PATH", tmp_path / "web-settings.json")
    monkeypatch.setattr(server, "CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(server.runtime, "load", _no_load)
    monkeypatch.setattr(server.runtime, "rebuild_agents", lambda: None)
    return tmp_path


def test_first_custom_model_takes_over_all_roles(web):
    with TestClient(server.app) as c:
        assert c.get("/api/settings").json()["roles"] == {}
        saved = c.post(
            "/api/settings/custom-model",
            json={
                "label": "我的模型",
                "model": "my-model",
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-test",
            },
        ).json()
        assert saved["ok"] is True
        mid = saved["id"]
        assert saved["roles"] == dict.fromkeys(
            ("planner", "executor", "verifier", "router"), mid
        ), "第一个模型必须把四个角色一起指过去（否则其余角色会去走 .env ⇒ 报 key 未配置）"


def test_second_custom_model_keeps_existing_roles(web):
    with TestClient(server.app) as c:
        first = c.post(
            "/api/settings/custom-model",
            json={"label": "A", "model": "model-a", "api_key": "sk-a"},
        ).json()
        second = c.post(
            "/api/settings/custom-model",
            json={"label": "B", "model": "model-b", "api_key": "sk-b"},
        ).json()
        assert first["id"] != second["id"]
        assert second["roles"] == dict.fromkeys(
            ("planner", "executor", "verifier", "router"), first["id"]
        ), "用户已经配过角色 ⇒ 加第二个模型不许动它"
