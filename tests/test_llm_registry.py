"""模型注册表与降级链测试（T3.2）。

覆盖：
1. 按角色取模型（默认全同一个；可覆盖）；
2. 降级链顺序（主力 + fallback 配置）；
3. `invoke_with_fallback`：主力报错 → 切备用；**超时**也算失败并切下一个；全挂才抛异常；
4. 配置文件缺失时优雅退化（不让程序起不来）。
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.model.llm import ROLE_NAMES, LLMRegistry, invoke_with_fallback

CONFIG = {
    "models": {
        "m-main": {
            "provider": "openai-compatible",
            "model": "main-model",
            "base_url": "https://a.example",
        },
        "m-backup": {
            "provider": "openai-compatible",
            "model": "backup-model",
            "base_url": "https://b.example",
        },
        "m-cheap": {
            "provider": "openai-compatible",
            "model": "cheap-model",
            "base_url": "https://c.example",
        },
    },
    "roles": {"planner": "m-main", "executor": "m-main", "verifier": "m-main", "router": "m-cheap"},
    "fallback": {"executor": ["m-backup"], "planner": []},
    "timeout_sec": 5,
}


def _write_cfg(tmp_path: Path, data: dict | None = CONFIG) -> Path:
    p = tmp_path / "models.json"
    p.write_text(json.dumps(data if data is not None else {}, ensure_ascii=False), encoding="utf-8")
    return p


class _OkLLM:
    def __init__(self, name: str, content: str = "ok") -> None:
        self.model_name = name
        self._content = content
        self.calls = 0

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        return AIMessage(content=self._content)


class _FailLLM:
    def __init__(self, name: str) -> None:
        self.model_name = name
        self.calls = 0

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        raise RuntimeError(f"{self.model_name}: 401 unauthorized")


class _SlowLLM:
    def __init__(self, name: str) -> None:
        self.model_name = name
        self.calls = 0

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        await asyncio.sleep(5)
        return AIMessage(content="too late")


# ── ① 按角色取模型 ──


def test_roles_default_from_config(tmp_path):
    reg = LLMRegistry(_write_cfg(tmp_path))
    assert reg.role_models() == {
        "planner": "m-main",
        "executor": "m-main",
        "verifier": "m-main",
        "router": "m-cheap",
    }
    assert reg.get("router").model_name == "cheap-model"


def test_role_models_are_independent(tmp_path):
    """三角色能配不同模型，且各自生效。"""
    reg = LLMRegistry(_write_cfg(tmp_path))
    reg.set_role_models({"planner": "m-backup", "executor": "m-cheap", "verifier": "m-main"})
    assert reg.get("planner").model_name == "backup-model"
    assert reg.get("executor").model_name == "cheap-model"
    assert reg.get("verifier").model_name == "main-model"
    assert reg.get("router").model_name == "cheap-model", "未覆盖的角色保持配置值"


def test_override_from_spec_parses_role_models(tmp_path):
    """`planner=x,executor=y` 形式的 spec 临时覆盖。"""
    reg = LLMRegistry(_write_cfg(tmp_path))
    applied = reg.override_from_spec("planner=m-cheap, executor=m-backup ,坏格式,router=")
    assert applied == {"planner": "m-cheap", "executor": "m-backup"}
    assert reg.model_key("planner") == "m-cheap"
    assert reg.model_key("executor") == "m-backup"


def test_empty_key_restores_config_default(tmp_path):
    reg = LLMRegistry(_write_cfg(tmp_path))
    reg.set_role_models({"executor": "m-cheap"})
    assert reg.model_key("executor") == "m-cheap"
    reg.set_role_models({"executor": ""})
    assert reg.model_key("executor") == "m-main"


def test_missing_config_degrades_to_single_model(tmp_path):
    """配置文件缺失/损坏时不应让程序起不来（退化成单模型）。"""
    reg = LLMRegistry(tmp_path / "not-exist.json")
    assert set(reg.role_models()) == set(ROLE_NAMES)
    assert all(v for v in reg.role_models().values())


# ── ② 降级链 ──


def test_chain_includes_configured_fallback(tmp_path):
    reg = LLMRegistry(_write_cfg(tmp_path))
    assert [llm.model_name for llm in reg.chain("executor")] == ["main-model", "backup-model"]
    assert [llm.model_name for llm in reg.chain("planner")] == ["main-model"], (
        "未配 fallback 的角色只有主力"
    )


# ── ③ invoke_with_fallback ──


async def test_fallback_switches_to_next_model():
    """主力报错 → 自动降级到备用。"""
    main, backup = _FailLLM("main"), _OkLLM("backup", "来自备用")
    resp = await invoke_with_fallback([main, backup], [], timeout=2)
    assert resp.content == "来自备用"
    assert main.calls == 1 and backup.calls == 1


async def test_timeout_also_triggers_fallback():
    """模型太慢（挂起）→ 超时也算失败，同样切下一个（fallback 与 timeout 管的是两件事）。"""
    slow, backup = _SlowLLM("slow"), _OkLLM("backup", "备用顶上")
    resp = await invoke_with_fallback([slow, backup], [], timeout=0.2)
    assert resp.content == "备用顶上"


async def test_all_fail_raises_last_error():
    a, b = _FailLLM("a"), _FailLLM("b")
    with pytest.raises(RuntimeError, match="b: 401"):
        await invoke_with_fallback([a, b], [], timeout=2)
    assert a.calls == 1 and b.calls == 1


async def test_single_model_no_fallback_needed():
    ok = _OkLLM("only", "唯一模型")
    resp = await invoke_with_fallback([ok], [], timeout=2)
    assert resp.content == "唯一模型"
