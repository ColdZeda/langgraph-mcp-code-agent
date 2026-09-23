"""提示词里的**模型名**必须跟着"当前生效模型"走（订正 #37）。

用户实测踩到的原话："我问他是什么模型，他说'我是执行者 novi，运行在 deepseek-flash 模型上'，
不过下方显示的实际使用确实是 mimo-v2.6.flash" ——
提示词里那个名字以前来自 `.env` 的 `MODEL_NAME`，**import 时定死**，
所以切了模型之后 `rebuild_agents()` 重建了 agent，**提示词里的名字还是旧的**。

守四件事：

| 风险 | 对应测试 |
|---|---|
| 提示词写的是**显示名**而不是实际调用名 | `test_prompt_uses_call_name_not_label` |
| 切模型后提示词不跟着变 | `test_prompt_follows_registry_switch` |
| 有人又拿静态 `PROMPT_CONTEXT` 去 format（静默报错模型名） | `test_raw_context_fails_loudly` |
| 格式化处绕开 `prompt_context()` | `test_multi_agent_formats_via_prompt_context` |
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.agent.prompts import (  # noqa: E402
    EXECUTOR_PLAN_PROMPT,
    PROMPT_CONTEXT,
    SYSTEM_PROMPT_TEMPLATE,
    effective_model_name,
    prompt_context,
)
from app.code_agent.config import MODEL_NAME  # noqa: E402
from app.code_agent.model.llm import ROLE_NAMES  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def clean_registry(monkeypatch, tmp_path):
    """换掉**模块级单例**注册表（`models` 是只读 property，改不动它，只能整体替换）。

    `effective_model_name()` 是在函数里 `from ... import registry` 的 → 改模块属性即生效；
    monkeypatch 负责测试后还原。
    """
    from app.code_agent import model as model_pkg
    from app.code_agent.model.llm import LLMRegistry

    fresh = LLMRegistry(tmp_path / "not-exist.json")  # 空注册表 = 四个角色走 .env 的默认
    monkeypatch.setattr(model_pkg.llm, "registry", fresh)
    return fresh


def test_prompt_uses_call_name_not_label(clean_registry):
    """提示词里要写**实际发给 API 的名字**（`model`），不是下拉框里的显示名（`label`）。

    显示名 ≠ 调用名是这个项目的老约定（`deepseek-v4.1-flash` → 实际 `deepseek-flash`），
    提示词这条也不能例外 —— 否则模型会报一个"界面上不存在"的名字。
    """
    clean_registry.set_custom_models(
        [{"id": "mimo", "label": "我的 Mimo（显示名）", "model": "mimo-v2.6-flash"}]
    )
    clean_registry.set_role_models({"executor": "mimo"})

    text = SYSTEM_PROMPT_TEMPLATE.format(**prompt_context("executor"))

    assert "mimo-v2.6-flash" in text, "提示词必须写实际调用名"
    assert "我的 Mimo（显示名）" not in text, "显示名不该进提示词"


def test_prompt_follows_registry_switch(clean_registry):
    """换模型之后再格式化，提示词必须变成新名字（这就是用户踩的那个 bug）。"""
    before = SYSTEM_PROMPT_TEMPLATE.format(**prompt_context("executor"))
    assert MODEL_NAME in before, "没配置时应该退回 .env 的系统默认"

    clean_registry.set_custom_models([{"id": "step", "model": "stepfun-3.7-flash"}])
    clean_registry.set_role_models({"executor": "step"})
    after = SYSTEM_PROMPT_TEMPLATE.format(**prompt_context("executor"))

    assert "stepfun-3.7-flash" in after
    assert MODEL_NAME not in after, "换了模型之后，提示词里不该还留着旧名字"


def test_each_role_gets_its_own_name(clean_registry):
    """四个角色可以各不相同 —— 提示词里的名字要**按角色**取，不能一个填所有。

    （模板里 `{model_name}` 出现在两处：通用段与 Executor 段。）
    """
    clean_registry.set_custom_models(
        [
            {"id": "a", "model": "model-a"},
            {"id": "b", "model": "model-b"},
        ]
    )
    clean_registry.set_role_models({"planner": "a", "executor": "b"})

    assert effective_model_name("planner") == "model-a"
    assert effective_model_name("executor") == "model-b"
    assert "model-b" in SYSTEM_PROMPT_TEMPLATE.format(**prompt_context("executor"))
    assert "model-a" in EXECUTOR_PLAN_PROMPT.format(**prompt_context("planner"))


def test_every_role_is_covered(clean_registry):
    """四个角色都要取得到名字（取不到会退回 .env —— 但那是兜底，不该发生在正常角色上）。"""
    for role in ROLE_NAMES:
        assert effective_model_name(role), f"{role} 取不到模型名"


def test_raw_context_fails_loudly():
    """**故意**让直接用静态 `PROMPT_CONTEXT` 的写法炸掉。

    宁可 `KeyError: 'model_name'`（构建时就发现），也不要静默地写出一个过期的模型名
    —— 后者正是这个 bug 藏了这么久的原因。
    """
    assert "model_name" not in PROMPT_CONTEXT
    with pytest.raises(KeyError, match="model_name"):
        SYSTEM_PROMPT_TEMPLATE.format(**PROMPT_CONTEXT)


def test_multi_agent_formats_via_prompt_context():
    """源码级守卫：格式化提示词的地方必须走 `prompt_context(...)`。"""
    source = (REPO_ROOT / "app/code_agent/agent/multi_agent.py").read_text(encoding="utf-8")
    assert "prompt_context(" in source
    # 不许再出现"拿静态上下文去 format"的写法
    assert ".format(**PROMPT_CONTEXT)" not in source
    assert "PROMPT_CONTEXT," not in source.replace("prompt_context,", ""), (
        "multi_agent 不该再 import 静态的 PROMPT_CONTEXT（它没有 model_name）"
    )
