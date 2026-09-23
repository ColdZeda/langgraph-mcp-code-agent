"""阶段 6 追加 · **模型配置的自由度**：用户自定义模型（自带地址 + 自带密钥）。

为什么要有这一层：以前"一个 key 走天下"（`_api_key or MODEL_API_KEY`）——
用户想用 GLM 就得把 `.env` 的 key 换成 GLM 的，于是 DeepSeek 的模型全废。
现在自定义模型**自带凭据**，与内置模型那组全局凭据互不影响。

这个文件守四件事：

1. **凭据隔离**：自定义模型用**自己的** key/地址；内置模型照旧用全局那组；
2. **密钥不外泄**：`/api/settings` 与 `/api/models` **绝不能**把明文 key 回给前端
   （只给"有没有配 + 尾号 4 位"）—— 这是最容易在"方便前端展示"时犯的错；
3. **删得干净**：删掉自定义模型时，角色里指向它的引用要**一起清**，
   否则角色会拿一个 id 当模型名去请求（那是另一个合法用法 → 报错很难懂）；
4. **"测试连接"测的是你填的那个模型**：改造前它只发 base_url+key，
   模型名回落 `.env`，于是"测试通过"跟你选的模型无关（假阳性）。
"""

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.model.llm import ROLE_NAMES, LLMRegistry, registry  # noqa: E402

GLOBAL_KEY = "global-key-1234567890"
CUSTOM_KEY = "custom-key-abcdefgh"


# ═══════════════════════════════════════════════════════════════════
# 夹具
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def web(monkeypatch, tmp_path):
    """Web 应用 + 隔离的运行时；并**快照/还原全局注册表**，避免污染别的测试。"""
    from app.web import server

    saved = {
        "custom": registry.custom_models,
        "api_key": registry._api_key,
        "base_url": registry._base_url,
        "roles": dict(registry._role_override),
    }
    monkeypatch.setattr(server, "SETTINGS_PATH", tmp_path / "web-settings.json")
    monkeypatch.setattr(server, "CHECKPOINT_DB", tmp_path / "checkpoints.db")

    async def _no_load():
        return None

    monkeypatch.setattr(server.runtime, "load", _no_load)
    monkeypatch.setattr(server.runtime, "rebuild_agents", lambda: None)
    yield server
    registry.set_custom_models(list(saved["custom"].values()))
    registry._api_key = saved["api_key"]
    registry._base_url = saved["base_url"]
    registry.set_role_models(saved["roles"])
    registry._cache.clear()


@pytest.fixture
def client(web):
    with TestClient(web.app) as c:
        yield c


def _add(client, **overrides):
    payload = {
        "label": "GLM-5.3",
        "model": "glm-5.3",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": CUSTOM_KEY,
    }
    payload.update(overrides)
    return client.post("/api/settings/custom-model", json=payload)


# ═══════════════════════════════════════════════════════════════════
# ① 凭据隔离
# ═══════════════════════════════════════════════════════════════════


def test_custom_model_uses_its_own_credentials(client):
    """自定义模型用**自己的**模型名 / 地址 / 密钥。"""
    res = _add(client)
    assert res.status_code == 200 and res.json()["ok"] is True
    custom_id = res.json()["id"]

    llm = registry.get_key(custom_id)
    assert llm.model_name == "glm-5.3"
    assert llm.openai_api_base == "https://open.bigmodel.cn/api/paas/v4"
    assert llm.openai_api_key.get_secret_value() == CUSTOM_KEY


def test_builtin_model_keeps_global_credentials(tmp_path):
    """内置（注册表）模型照旧用全局那组凭据 —— 自定义模型的 key **不会**串过去。

    ⚠️ 这里用**临时注册表**、不用全局那个：2026-09-22 起仓库里的 `config/models.json`
    **故意是空的**（默认只有"系统默认"，直接走 .env），所以不能拿真实注册表当内置样本。
    """
    reg = LLMRegistry(_cfg(tmp_path))
    reg.set_credentials(base_url="https://global.example.com/v1", api_key=GLOBAL_KEY)
    reg.set_custom_models([{"id": "my-model", "model": "glm-5.3", "api_key": CUSTOM_KEY}])

    builtin = reg.get_key("builtin-a")
    custom = reg.get_key("my-model")
    assert builtin.openai_api_key.get_secret_value() == GLOBAL_KEY
    assert custom.openai_api_key.get_secret_value() == CUSTOM_KEY
    assert builtin.openai_api_key.get_secret_value() != custom.openai_api_key.get_secret_value()


def test_custom_model_without_key_falls_back_to_global(client):
    """自定义模型**没填** key 时回落全局凭据（照旧可用，只是共用 key）。"""
    client.post("/api/settings", json={"api_key": GLOBAL_KEY})
    res = _add(client, api_key="")
    custom_id = res.json()["id"]

    llm = registry.get_key(custom_id)
    assert llm.openai_api_key.get_secret_value() == GLOBAL_KEY
    assert llm.model_name == "glm-5.3"


def test_builtin_display_name_differs_from_api_model(tmp_path):
    """内置的显示名与**实际调用名**是两回事（官方改名时只动后者）。"""
    reg = LLMRegistry(_cfg(tmp_path))
    spec = reg.all_models()["builtin-a"]
    assert spec["label"] == "builtin-a"  # 键 = 显示名
    assert spec["model"] == "real-a"  # model 字段 = 实际调用名
    assert spec["custom"] is False


# ── 「系统默认」与「当前生效模型」（阶段 6 · 用户提的显示需求）──


def test_system_default_falls_back_to_env_model_name(tmp_path):
    """注册表为空时（**仓库现状**）：四个角色都用 .env 的 MODEL_NAME —— 这就是「系统默认」。"""
    from app.code_agent.config import MODEL_NAME

    path = tmp_path / "models.json"
    path.write_text(json.dumps({"models": {}, "roles": {}, "fallback": {}}), encoding="utf-8")
    reg = LLMRegistry(path)

    assert reg.all_models() == {}, "空注册表不该凭空造出模型条目"
    for role in ROLE_NAMES:
        assert reg.model_key(role) == MODEL_NAME
        resolved = reg.resolve_model(role)
        assert resolved["label"] == "系统默认"
        assert resolved["model"] == MODEL_NAME
        assert resolved["key"] == ""


def test_resolve_model_marks_custom_and_reports_real_name(tmp_path):
    """`resolve_model`：自定义模型报**它的显示名 + 实际调用名**（界面顶栏显示用）。"""
    reg = LLMRegistry(_cfg(tmp_path))
    reg.set_custom_models([{"id": "glm", "label": "GLM-5.3", "model": "glm-5.3"}])
    reg.set_role_models({"executor": "glm"})

    assert reg.resolve_model("executor") == {
        "key": "glm",
        "label": "GLM-5.3",
        "model": "glm-5.3",
        "custom": True,
    }
    assert reg.resolve_model("planner")["label"] == "builtin-a", "没覆盖的角色用注册表默认"


def test_effective_models_cover_every_role(tmp_path):
    reg = LLMRegistry(_cfg(tmp_path))
    reg.set_custom_models([{"id": "glm", "model": "glm-5.3"}])
    reg.set_role_models({"verifier": "glm"})

    eff = reg.effective_models()
    assert set(eff) == set(ROLE_NAMES)
    assert eff["verifier"]["custom"] is True
    assert eff["executor"]["custom"] is False


def test_models_endpoint_returns_effective_models(client):
    """界面顶栏靠它显示「当前生效模型」—— 后端算好，前端不自己拼三层兜底链。"""
    body = client.get("/api/models").json()
    eff = body["effectiveModels"]
    assert set(eff) == set(ROLE_NAMES)
    assert all("model" in v and "label" in v for v in eff.values())


# ═══════════════════════════════════════════════════════════════════
# ② 密钥不外泄（最容易犯的错）
# ═══════════════════════════════════════════════════════════════════


def test_settings_response_never_contains_raw_key(client):
    client.post("/api/settings", json={"api_key": GLOBAL_KEY})
    _add(client)

    body = client.get("/api/settings").text
    assert GLOBAL_KEY not in body, "/api/settings 泄露了内置 key"
    assert CUSTOM_KEY not in body, "/api/settings 泄露了自定义模型 key"
    data = json.loads(body)
    assert data["api_key_set"] is True
    assert data["api_key_tail"] == GLOBAL_KEY[-4:]
    assert data["customModels"][0]["api_key_tail"] == CUSTOM_KEY[-4:]


def test_models_response_never_contains_raw_key(client):
    """`/api/models` 曾经直接把 `registry.all_models()` 返回出去 —— 那会带明文密钥。"""
    _add(client)
    body = client.get("/api/models").text
    assert CUSTOM_KEY not in body, "/api/models 泄露了自定义模型 key"
    entry = next(m for m in json.loads(body)["models"] if m["custom"])
    assert entry["api_key_set"] is True
    assert entry["api_key_tail"] == CUSTOM_KEY[-4:]
    assert "api_key" not in entry


def test_saved_key_is_kept_when_field_left_blank(client):
    """只改显示名时留空 api_key → **保持原密钥**（前端拿不到原值，不该被清掉）。"""
    custom_id = _add(client).json()["id"]
    res = _add(client, id=custom_id, label="GLM 改名了", api_key="")
    assert res.json()["ok"] is True

    llm = registry.get_key(custom_id)
    assert llm.openai_api_key.get_secret_value() == CUSTOM_KEY, "留空不该清掉密钥"


# ═══════════════════════════════════════════════════════════════════
# ③ 增删与引用清理
# ═══════════════════════════════════════════════════════════════════


def test_custom_model_appears_in_model_list(client):
    custom_id = _add(client).json()["id"]
    models = {m["key"]: m for m in client.get("/api/models").json()["models"]}
    assert custom_id in models
    assert models[custom_id]["custom"] is True
    assert models[custom_id]["label"] == "GLM-5.3"
    assert models[custom_id]["model"] == "glm-5.3"


def test_delete_removes_model_and_role_reference(client):
    """删掉自定义模型 → 列表里没了，**角色里指向它的引用也一起清掉**。"""
    custom_id = _add(client).json()["id"]
    client.post("/api/settings", json={"roles": {"executor": custom_id}})
    assert registry.model_key("executor") == custom_id

    res = client.request("DELETE", f"/api/settings/custom-model/{custom_id}")
    assert res.status_code == 200 and res.json()["ok"] is True
    assert custom_id not in {m["key"] for m in client.get("/api/models").json()["models"]}
    assert registry.model_key("executor") != custom_id, "角色引用没被清掉"
    assert registry.model_key("executor")  # 回落配置默认，且非空


def test_delete_unknown_model_is_rejected(client):
    res = client.request("DELETE", "/api/settings/custom-model/not-a-model")
    assert res.json()["ok"] is False
    assert "没有这个自定义模型" in res.json()["error"]


def test_custom_model_requires_model_name(client):
    res = _add(client, model="")
    assert res.json()["ok"] is False
    assert "不能为空" in res.json()["error"]


def test_duplicate_model_names_get_distinct_ids(client):
    first = _add(client).json()["id"]
    second = _add(client, label="另一个 GLM").json()["id"]
    assert first != second
    assert second.startswith(first)


def test_custom_models_survive_settings_roundtrip(client, tmp_path):
    """落盘 → 重新加载（模拟重启）后自定义模型还在。"""
    custom_id = _add(client).json()["id"]
    saved = json.loads((tmp_path / "web-settings.json").read_text(encoding="utf-8"))
    assert saved["custom_models"][0]["id"] == custom_id

    registry.set_custom_models([])  # 模拟进程重启：内存清空
    from app.web import server

    server.apply_settings(server.load_settings())
    assert custom_id in registry.all_models()
    assert registry.get_key(custom_id).openai_api_key.get_secret_value() == CUSTOM_KEY


# ═══════════════════════════════════════════════════════════════════
# ④ 「测试连接」必须测你填的那个模型
# ═══════════════════════════════════════════════════════════════════


def test_test_endpoint_uses_the_given_model_name(web, monkeypatch):
    """改造前的假阳性：只发 base_url+key，模型名回落 `.env` → 测试结果与你选的模型无关。"""
    captured: dict = {}

    class _FakeLLM:
        async def ainvoke(self, *_a, **_kw):
            return None

    def fake_build_llm(model=None, base_url=None, api_key=None, timeout=None):
        captured.update(model=model, base_url=base_url, api_key=api_key)
        return _FakeLLM()

    monkeypatch.setattr(web, "build_llm", fake_build_llm)
    with TestClient(web.app) as c:
        res = c.post(
            "/api/settings/test",
            json={"model": "glm-5.3", "base_url": "https://x/v1", "api_key": "k-12345678"},
        )
    body = res.json()
    assert body["ok"] is True
    assert captured["model"] == "glm-5.3", "测试连接没把模型名带下去"
    assert body["testedModel"] == "glm-5.3", "响应里要能看出到底测了哪个模型"


def test_test_endpoint_accepts_saved_model_id(web, monkeypatch):
    """只给 `model_id` 时，从注册表取它的真身与凭据来测。"""
    captured: dict = {}

    class _FakeLLM:
        async def ainvoke(self, *_a, **_kw):
            return None

    def fake_build_llm(model=None, base_url=None, api_key=None, timeout=None):
        captured.update(model=model, base_url=base_url, api_key=api_key)
        return _FakeLLM()

    monkeypatch.setattr(web, "build_llm", fake_build_llm)
    with TestClient(web.app) as c:
        c.post(
            "/api/settings/custom-model",
            json={"model": "glm-5.3", "base_url": "https://x/v1", "api_key": CUSTOM_KEY},
        )
        res = c.post("/api/settings/test", json={"model_id": "custom-glm-5-3"})
    assert res.json()["ok"] is True
    assert captured["model"] == "glm-5.3"
    assert captured["api_key"] == CUSTOM_KEY
    assert captured["base_url"] == "https://x/v1"


def test_test_endpoint_rejects_unknown_model_id(client):
    res = client.post("/api/settings/test", json={"model_id": "nope"})
    assert res.json()["ok"] is False
    assert "没有这个模型" in res.json()["error"]


# ═══════════════════════════════════════════════════════════════════
# ⑤ 注册表层（不经过 HTTP）
# ═══════════════════════════════════════════════════════════════════


def _cfg(tmp_path, *, models=None, roles=None):
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps(
            {
                "models": models
                or {
                    "builtin-a": {"model": "real-a", "base_url": "https://a.example.com"},
                },
                "roles": roles or dict.fromkeys(ROLE_NAMES, "builtin-a"),
                "fallback": {},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_bad_custom_entries_are_skipped_not_fatal(tmp_path):
    """配置文件被手改坏时不该让程序起不来：缺 id / 缺 model 的条目直接跳过。"""
    reg = LLMRegistry(_cfg(tmp_path))
    reg.set_custom_models(
        [
            {"id": "ok", "model": "m1"},
            {"id": "", "model": "m2"},
            {"model": "m3"},
            "不是字典",
            None,
        ]
    )
    assert set(reg.custom_models) == {"ok"}


def test_custom_overrides_builtin_with_same_id(tmp_path):
    reg = LLMRegistry(_cfg(tmp_path))
    reg.set_custom_models([{"id": "builtin-a", "model": "my-own", "base_url": "https://mine"}])
    assert reg.all_models()["builtin-a"]["model"] == "my-own"
    assert reg.all_models()["builtin-a"]["custom"] is True
    assert reg.get_key("builtin-a").model_name == "my-own"


def test_default_label_falls_back_to_id(tmp_path):
    reg = LLMRegistry(_cfg(tmp_path))
    reg.set_custom_models([{"id": "no-label", "model": "m"}])
    assert reg.all_models()["no-label"]["label"] == "no-label"


def test_set_custom_models_clears_cache(tmp_path):
    """改模型配置后必须清缓存，否则会继续用旧凭据那次建的实例。"""
    reg = LLMRegistry(_cfg(tmp_path))
    reg.set_custom_models([{"id": "c", "model": "m1", "api_key": "k1-123456"}])
    first = reg.get_key("c")
    reg.set_custom_models([{"id": "c", "model": "m2", "api_key": "k2-123456"}])
    second = reg.get_key("c")
    assert first is not second
    assert second.model_name == "m2"
