"""回归守卫：Verifier 裁定**不是合法 JSON** 时的重试路径（2026-09-24 修 D1 / D2）。

两个都是 multi 轮实测踩到的坑（single 模式没有 Verifier、没有重跑，所以只在 multi 复发）：

- **D1 · 打回上限失效**：旧代码用 `"FAIL" in prev_verdict.upper()` 判断"这次算不算重跑"，
  而**循环上限只看那个计数**（`retry_count >= MAX_RETRY`）。
  ⇒ 上游返回错误串（实测原文 `Sorry, need more steps to process this request.`）时，
  字符串里既没有 `PASS` 也没有 `FAIL` ⇒ 计数**永不增长** ⇒ **"最多打回 2 次"永不生效**
  ⇒ 只能等 token 预算被烧穿。而且 `is_retry=False` ⇒ 重跑时**连"上一轮为什么没通过"都没告诉 Executor**（盲重试）。
  实测 E007：Executor 跑 3 次、Verifier 跑 3 次、`retry_count=0`、232,700 token、击穿 200k 预算。

- **D2 · 轨迹被覆盖**：`executor_trace_list` 每轮**整体替换** ⇒ 最后一次（被预算掐断的）重跑
  把前面真正干活的轨迹冲掉 ⇒ **状态断言全过、轨迹断言却挂**（计分假阴性）。
  实测 E007：`eval_shop.products` 建好了、3 行数据也在（3 条状态断言全过），
  但"调用了工具 [mysql_insert_data, mysql_create_database, mysql_create_table]"判否
  —— 记录里只剩最后那次重跑的第 1 步 `mysql_create_database`。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.agent import multi_agent as ma  # noqa: E402

UPSTREAM_ERROR = "Sorry, need more steps to process this request."


# ═══════════════════════════════════════════════════════════════════
# D1：裁定判据 + 打回上限
# ═══════════════════════════════════════════════════════════════════


def test_verdict_passed_only_accepts_explicit_pass():
    """**只有**明确解析出 `{"verdict": "PASS"}` 才算通过。"""
    assert ma._verdict_passed('{"verdict": "PASS", "reason": "产物齐全"}') is True
    assert ma._verdict_passed('{"verdict": "FAIL", "reason": "缺产物"}') is False
    assert ma._verdict_passed("") is False


def test_unparseable_verdict_is_treated_as_a_retry_round():
    """**D1 的核心回归**：上游错误串必须算"没通过 ⇒ 这是重跑"。

    旧写法 `"FAIL" in UPSTREAM_ERROR.upper()` 返回 False（串里没有 FAIL 这个词），
    于是计数不增长、上限失效 —— 这条断言就是钉住那个行为。
    """
    assert ma._is_retry_round(UPSTREAM_ERROR) is True, "上游错误串没被当成失败 ⇒ 上限会失效"
    assert ma._is_retry_round('{"verdict": "FAIL", "reason": "x"}') is True
    assert ma._is_retry_round('{"verdict": "PASS"}') is False
    assert ma._is_retry_round("") is False  # 首次执行：没有上一轮裁定
    assert ma._is_retry_round("   \n") is False


def test_retry_cap_still_applies_when_verdict_is_garbage():
    """裁定无法解析时，循环也必须在 MAX_RETRY 处收住（旧代码这里会一直回 executor）。"""
    state = {"verdict": UPSTREAM_ERROR, "retry_count": ma.MAX_RETRY, "budget_exceeded": False}
    assert ma.decide_after_verify(state) == "end"

    state["retry_count"] = 0
    assert ma.decide_after_verify(state) == "executor"

    state.update({"verdict": '{"verdict": "PASS"}', "retry_count": 0})
    assert ma.decide_after_verify(state) == "end"

    state.update({"verdict": UPSTREAM_ERROR, "retry_count": 0, "budget_exceeded": True})
    assert ma.decide_after_verify(state) == "end", "预算击穿必须先结束"


# ═══════════════════════════════════════════════════════════════════
# D2：执行轨迹跨轮累积
# ═══════════════════════════════════════════════════════════════════


class _FakeExecutor:
    """假 Executor：按 astream 的块协议吐 N 次工具调用 + 一句收尾。"""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def astream(self, inputs, config=None):  # noqa: ANN001, ARG002
        for name in self.calls:
            msg = AIMessage(content="")
            msg.tool_calls = [{"name": name, "args": {}, "id": f"call-{name}"}]
            yield {"agent": {"messages": [msg]}}
        yield {"agent": {"messages": [AIMessage(content="做完了")]}}


def _executor_state(**over: object) -> dict:
    state = {
        "user_input": "把任务做完",
        "plan": [],
        "messages": [],
        "token_usage": 0,
        "step_count": 0,
        "pruned_messages": 0,
        "executor_trace_list": [],
    }
    state.update(over)
    return state


@pytest.mark.asyncio
async def test_executor_trace_accumulates_across_retries():
    """重跑**不能**把上一轮的轨迹冲掉（D2）。"""
    state = _executor_state(
        step_count=4,
        executor_trace_list=[{"name": "mysql_create_table", "args": {}}],
        verdict='{"verdict": "FAIL", "reason": "缺数据"}',  # 上一轮没过 ⇒ 本次是重跑
    )

    out = await ma.executor_node(state, _FakeExecutor(["mysql_insert_data"]))

    names = [t["name"] for t in out["executor_trace_list"]]
    assert names == ["mysql_create_table", "mysql_insert_data"], "上一轮的轨迹被覆盖了"
    assert "mysql_create_table" in out["executor_trace"], "文本轨迹同样要保留上一轮"
    assert out["retry_count"] == 1, "重跑必须计数（D1）"


@pytest.mark.asyncio
async def test_step_count_accumulates_across_retries():
    """步数也应当跨轮累加 —— 否则记录里只显示最后那次的步数。"""
    state = _executor_state(step_count=10, verdict='{"verdict": "FAIL"}')
    out = await ma.executor_node(state, _FakeExecutor(["read_file"]))
    assert out["step_count"] > 10, "步数没有累加"


@pytest.mark.asyncio
async def test_first_run_has_no_retry_and_no_accumulation():
    """首次执行（没有上一轮裁定）：不计次、轨迹就是本轮的。"""
    out = await ma.executor_node(_executor_state(), _FakeExecutor(["write_file"]))
    assert [t["name"] for t in out["executor_trace_list"]] == ["write_file"]
    assert out["retry_count"] == 0
