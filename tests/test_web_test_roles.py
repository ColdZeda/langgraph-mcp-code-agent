"""`POST /api/settings/test-roles` 的回归测试（阶段 7 · 界面第二轮反馈）。

这个接口解决的是"只测 Executor 说明不了整条链路通不通"+"四个角色盲测四次是浪费"：

1. **按模型键去重**：2 个模型分给 4 个角色 ⇒ 只发 **2** 次探测（不是 4 次）；
2. **失败按模型分组报告**：某个模型不通时，只有它那一组 `ok=False`，并带上它服务的角色；
3. **没指定的角色并成一组**：`roles` 全空时只探一次全局凭据（键为空串那一组）。

⚠️ 真去调模型要花钱/要 key，所以这里**打桩 `server.build_llm`**（`/api/settings/test`
的实现会对它调 `ainvoke`）—— 但被测的业务逻辑（去重 / 分组 / 并发 / 结果形状）是真的跑。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.model.llm import ROLE_NAMES  # noqa: E402
from app.web import server  # noqa: E402

FAILING_MODEL = "bad-model"


class _FakeLLM:
    def __init__(self, model: str) -> None:
        self.model = model

    async def ainvoke(self, _msg: str) -> str:
        if self.model == FAILING_MODEL:
            raise RuntimeError("连接失败（测试桩）")
        return "正常"


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def _no_load() -> None:
        return None

    monkeypatch.setattr(server, "SETTINGS_PATH", tmp_path / "web-settings.json")
    monkeypatch.setattr(server, "CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(server.runtime, "load", _no_load)
    monkeypatch.setattr(server.runtime, "rebuild_agents", lambda: None)

    calls: list[str] = []
    monkeypatch.setattr(
        server,
        "build_llm",
        lambda **kw: (calls.append(kw.get("model")), _FakeLLM(str(kw.get("model"))))[1],
    )

    # 注册表是进程级单例 ⇒ 用完必须还原，别污染别的测试
    yield {"calls": calls}
    server.registry.set_custom_models([])
    server.registry.set_role_models(dict.fromkeys(ROLE_NAMES, ""))


def _two_models() -> None:
    server.registry.set_custom_models(
        [
            {
                "id": "custom-a",
                "label": "模型A",
                "model": "model-a",
                "base_url": "",
                "api_key": "sk-a",
            },
            {
                "id": "custom-b",
                "label": "模型B",
                "model": FAILING_MODEL,
                "base_url": "",
                "api_key": "sk-b",
            },
        ]
    )


def test_dedupes_models_and_groups_roles(client):
    """2 个模型 / 4 个角色 ⇒ 2 次探测，且分组里带着各自的角色。"""
    _two_models()
    server.registry.set_role_models(
        {
            "planner": "custom-a",
            "executor": "custom-b",
            "verifier": "custom-b",
            "router": "custom-a",
        }
    )
    with TestClient(server.app) as c:
        data = c.post("/api/settings/test-roles").json()

    assert len(data["groups"]) == 2, "两个模型必须只探两次（去重）"
    assert len(client["calls"]) == 2, f"实际探测次数：{client['calls']}"

    by_key = {g["key"]: g for g in data["groups"]}
    assert by_key["custom-a"]["roles"] == ["planner", "router"]
    assert by_key["custom-b"]["roles"] == ["executor", "verifier"]
    assert by_key["custom-a"]["label"] == "模型A"
    assert by_key["custom-a"]["ok"] is True
    assert by_key["custom-a"]["elapsedSec"] >= 0


def test_failure_is_reported_per_group(client):
    """一个模型不通 ⇒ 只有那组红，整体 ok 为 False，且错误信息带在那一组上。"""
    _two_models()
    server.registry.set_role_models(
        {
            "planner": "custom-a",
            "executor": "custom-b",
            "verifier": "custom-b",
            "router": "custom-a",
        }
    )
    with TestClient(server.app) as c:
        data = c.post("/api/settings/test-roles").json()

    by_key = {g["key"]: g for g in data["groups"]}
    assert data["ok"] is False
    assert by_key["custom-a"]["ok"] is True
    assert by_key["custom-b"]["ok"] is False
    assert "连接失败（测试桩）" in by_key["custom-b"]["error"]
    assert by_key["custom-b"]["roles"] == ["executor", "verifier"]


def test_empty_roles_probe_global_credentials_once(client):
    """四个角色都没单独指定模型 ⇒ 归成一组，且测的是**全局凭据**（不是拿默认模型名当 id 去查）。

    ⚠️ 这里的坑：`registry.model_key(role)` 在没指定时返回 `.env` 的 `MODEL_NAME`
    （一个不在注册表里的"裸模型名"）。要是直接把它当 `model_id` 发过去，
    接口会回"没有这个模型：xxx" —— 那是**假失败**。
    """
    server.registry.set_custom_models([])
    with TestClient(server.app) as c:
        data = c.post("/api/settings/test-roles").json()

    assert len(data["groups"]) == 1
    group = data["groups"][0]
    assert group["key"] == server.MODEL_NAME
    assert group["fallback"] is True, "没指定模型的角色要走全局凭据"
    assert group["ok"] is True
    assert sorted(group["roles"]) == sorted(ROLE_NAMES)
    assert client["calls"] == [None], f"应当只探一次全局凭据（model=None）：{client['calls']}"
