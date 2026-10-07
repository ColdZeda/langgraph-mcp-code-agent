"""阶段 8 · P2：三项阈值按**模型窗口**自动算（候选池 §十三①）。

守四件事：

| 要守的事 | 为什么 |
|---|---|
| 窗口越大，三项阈值越大（并有封顶） | 32k 窗口不能被 30000 的 `NODE` 撞窗口；1M 窗口不该一直按 6000 压实 |
| **explicit env 永远优先** | 用户 `.env` 里写了数就该用他的（这是"我说了算"的出口），也是评估"只计量"口径（`=0`）的依据 |
| 没人声明窗口 ⇒ 回落默认 128k | 全新 clone 不配任何东西也必须能跑 |
| 注册表里声明的窗口**真的**被用上 | 端到端把自定义模型挂到 executor 角色上，看阈值是否跟着变 |

⚠️ 比例 2026-10-07 从 15%/35% 调成 **25%/50%**（`compact` 必须明显小于 `node`，见 config.py）。
⚠️ 任务级**刻意不是**窗口的小比例（候选池当时写的是 60~70%）：它是"成本保险丝"，
一个任务本来就可能跑好几轮满上下文 —— 128k × 65% ≈ 8.3 万，比今天的 20 万还紧，
与"预算放宽到 50 万~100 万"的结论相反。现在按 `max(50 万, 4 倍窗口)` 算。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent import config  # noqa: E402
from app.code_agent.config import budgets_for_window, token_budgets  # noqa: E402
from app.code_agent.model.llm import registry  # noqa: E402

# ═══════════════════════════════════════════════════════════════════
# 一、纯函数：窗口 → 三项阈值
# ═══════════════════════════════════════════════════════════════════


def test_128k_window_gives_the_expected_numbers():
    b = budgets_for_window(128_000)

    assert b.compact == 32_000  # 25%
    assert b.node == 64_000  # 50%
    assert b.task == 512_000  # max(50 万, 4×窗口)
    assert b.context_window == 128_000
    assert b.source == "window"


def test_thresholds_grow_with_the_window_and_are_capped():
    """窗口越大额度越大；但都有封顶（大窗口不等于无限烧）。"""
    small = budgets_for_window(32_000)
    mid = budgets_for_window(128_000)
    big = budgets_for_window(1_000_000)

    assert small.compact <= mid.compact <= big.compact
    assert small.node <= mid.node <= big.node
    assert small.task <= mid.task <= big.task

    assert small.compact == 8_000, "32k 窗口：压实按 25% 算（没撞下限）"
    assert small.node == 16_000, "32k 窗口：单次输入按 50% 算 —— **不能**被下限顶到 3 万"
    assert small.task == config.TASK_MIN, "任务级有下限：小窗口也不能把任务掐得太死"
    assert big.compact == config.COMPACT_MAX and big.node == config.NODE_MAX
    assert big.task == config.TASK_MAX


def test_node_never_eats_more_than_half_the_window():
    """🔴 安全性质：单次调用输入**不得超过窗口的一半**（留一半给输出 + 工具 + 估算误差）。

    这条是 2026-10-07 补的：原先 `NODE_MIN=30000` 会把 32k 窗口的额度顶到 3 万
    （= 窗口的 94%）⇒ 反而制造了「一次调用就撞窗口」的风险，方向是**反**的。
    """
    for window in (8_000, 16_000, 32_000, 64_000, 128_000, 200_000, 1_000_000):
        node = budgets_for_window(window).node
        assert node <= max(config.NODE_MIN, window // 2), (
            f"窗口 {window} 算出的 node={node} 超过了一半"
        )


def test_task_budget_is_a_cost_fuse_not_a_window_fraction():
    """任务级的下限就是"成本保险丝"的定位（**刻意偏离**候选池的 60~70% 窗口比例）。"""
    b = budgets_for_window(128_000)

    assert b.task >= config.TASK_MIN
    assert b.task > b.context_window * 0.65, "比'一整个窗口的 65%'要宽 —— 否则长任务会被误杀"


def test_env_values_win_over_the_window():
    b = budgets_for_window(
        128_000,
        compact_env=20_000,
        node_env=30_000,
        task_env=200_000,
    )

    assert (b.compact, b.node, b.task) == (20_000, 30_000, 200_000)
    assert b.source == "env"
    assert b.context_window == 128_000, "窗口仍然如实带出来（便于排查）"


def test_bad_window_falls_back_to_the_default():
    for bad in (0, -1, None):
        b = budgets_for_window(bad)  # type: ignore[arg-type]
        assert b.context_window == config.DEFAULT_CONTEXT_WINDOW


# ═══════════════════════════════════════════════════════════════════
# 二、运行期解析：真的去问注册表
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def clean_registry(monkeypatch):
    """把注册表恢复成"没有任何自定义模型、角色也没指过去"的干净态。"""
    monkeypatch.setattr(registry, "_custom_models", {}, raising=False)
    monkeypatch.setattr(registry, "_role_models", {}, raising=False)
    monkeypatch.delenv("CODE_AGENT_CONTEXT_WINDOW", raising=False)
    monkeypatch.delenv("CODE_AGENT_COMPACT_THRESHOLD", raising=False)
    monkeypatch.delenv("CODE_AGENT_NODE_TOKEN_BUDGET", raising=False)
    monkeypatch.delenv("CODE_AGENT_TASK_TOKEN_BUDGET", raising=False)
    return registry


def test_no_declared_window_falls_back_to_128k(clean_registry):
    b = token_budgets()

    assert b.context_window == config.DEFAULT_CONTEXT_WINDOW
    assert b.compact == 32_000 and b.node == 64_000 and b.task == 512_000


def test_declared_window_drives_the_thresholds(clean_registry, monkeypatch):
    """🔴 **验收那条**：给模型声明 200k 窗口 ⇒ 三项阈值立刻跟着变（不用重启）。"""
    clean_registry.set_custom_models(
        [{"id": "m200k", "label": "m200k", "model": "m200k", "context_window": 200_000}]
    )
    clean_registry.set_role_models({"executor": "m200k"})

    b = token_budgets()

    assert b.context_window == 200_000
    assert b.compact == 50_000  # 25%
    assert b.node == 100_000  # 50%
    assert b.task == 800_000  # max(50 万, 4×20 万)
    assert registry.context_window("executor") == 200_000


def test_env_window_overrides_the_registry(clean_registry, monkeypatch):
    """`CODE_AGENT_CONTEXT_WINDOW` 是全局覆盖（模型没声明、或想临时压小都能用）。"""
    monkeypatch.setenv("CODE_AGENT_CONTEXT_WINDOW", "32000")

    b = token_budgets()

    assert b.context_window == 32_000
    assert b.node == 16_000, "32k 窗口 ⇒ 单次输入 1.6 万（不再被下限顶到 3 万）"


def test_custom_model_without_window_still_works(clean_registry):
    """用户加模型时不填窗口是常态 —— 不许因此报错，按默认窗口算。"""
    clean_registry.set_custom_models([{"id": "x", "label": "x", "model": "x"}])
    clean_registry.set_role_models({"executor": "x"})

    assert registry.context_window("executor") is None
    assert token_budgets().context_window == config.DEFAULT_CONTEXT_WINDOW


def test_registry_survives_a_garbage_window(clean_registry):
    """配置文件被手改成脏值（字符串/负数）⇒ 当作"没声明"，绝不抛。"""
    clean_registry.set_custom_models(
        [{"id": "bad", "label": "bad", "model": "bad", "context_window": "128k"}]
    )
    clean_registry.set_role_models({"executor": "bad"})

    assert registry.context_window("executor") is None
    assert token_budgets().context_window == config.DEFAULT_CONTEXT_WINDOW
