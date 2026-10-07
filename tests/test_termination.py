"""阶段 8 · P1：**终止路径**（预算 / 停止）的文案与出边。

守两件事（都来自探索测试第 1–2 轮的账本）：

| 要守的事 | 账本 | 怎么测 |
|---|---|---|
| 因预算终止 ⇒ **不再调 Verifier**，且**不拼成"验收未通过"** | R5 | 真图跑一遍（multi 模式 + 极小的预算），数 Verifier 的调用次数 |
| 终止说明里**不许再出现"见上方工具调用轨迹"** | R6 | ① 产出的文本里没有；② **源码级**守卫（那行字是历史回放里最误导人的一处） |
| 停止与预算两条路的说明**共用同一套收尾** | P1 ② | 两条文案都必须含「副作用保留」与同一句收尾 |

⚠️ 为什么 R6 值得一条源码级守卫：历史回放（`GET /api/sessions/{id}/messages`）**只回最终回复**，
轨迹在 Web 的结果卡片里、**不在对话历史里** —— 只要有人再写一句"见上方轨迹"，
用户就会去一个不存在的地方找（第 2 题现场就是这样）。
"""

import ast
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.agent import cancel as cx  # noqa: E402
from app.code_agent.agent import multi_agent as ma  # noqa: E402
from tests.test_cancel import (  # noqa: E402
    _CountingAgent,
    _FakePlannerLLM,
    _FakeRegistry,
    _StepwiseExecutor,
    _writing_tool,
)

# ═══════════════════════════════════════════════════════════════════
# 夹具（与 tests/test_cancel.py 那套一致：隔离 checkpoint + 假 Planner）
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def fake_planner(monkeypatch):
    llm = _FakePlannerLLM()
    monkeypatch.setattr(ma, "get_llm", lambda *a, **k: llm)
    monkeypatch.setattr(ma, "registry", _FakeRegistry(llm))
    return llm


@pytest.fixture
def thread_id(monkeypatch, tmp_path):
    monkeypatch.setattr(ma, "CHECKPOINT_DB", tmp_path / "checkpoints.db")
    return f"term-{uuid4().hex[:8]}"


async def _run(tmp_path, token, agent, *, verifier, mode="multi", thread="t"):
    with cx.bind_cancel(token):
        return await ma.run_multi_agent(
            "批量删除前先统计一遍",  # 含 COMPLEX_KEYWORDS ⇒ 规则直接判 complex
            [],
            executor_agent=agent,
            verifier_agent=verifier,
            thread_id=thread,
            mode=mode,
            auto_inject=False,
            auto_deposit=False,
        )


# ═══════════════════════════════════════════════════════════════════
# 一、预算终止：不调 Verifier、不说成"验收未通过"
# ═══════════════════════════════════════════════════════════════════


async def test_budget_termination_skips_the_verifier(
    tmp_path, thread_id, fake_planner, monkeypatch
):
    """预算击穿 ⇒ 直接结束：**不跑验收**，说明也**不是**"验收未通过"。

    现场（账本 R5）：第 2 题因预算终止，界面却写着"验收未通过"+ 一句英文错误串。
    """
    monkeypatch.setattr(ma, "TASK_TOKEN_BUDGET", 50)  # 第一步（100 token）就会击穿
    token = cx.CancelToken()
    verifier = _CountingAgent()
    agent = _StepwiseExecutor(
        token,
        _writing_tool(tmp_path),
        steps=3,
        cancel_after=None,
        tokens_per_step=100,
    )

    result = await _run(tmp_path, token, agent, verifier=verifier, thread=thread_id)

    assert result["budget_exceeded"] is True
    assert verifier.calls == 0, "被预算掐断后还去跑一次验收 = 白花一次模型调用"
    assert result["verdict"] == "", "根本没有验收环节，不该有裁定"
    body = result["final_response"]
    assert body.startswith("【已终止·预算】"), body[:80]
    # ⚠️ 判据不能用"文本里没有「验收未通过」"—— 说明里**故意**有一句"也不是「验收未通过」"在澄清。
    #    要守的是"**没有被那个包装句覆盖**"（`run_multi_agent` 给 FAIL 加的前缀）。
    assert not body.startswith("任务执行完成"), "不许把预算终止包装成「任务执行完成，但验收未通过」"
    assert "但验收未通过" not in body
    assert result["final_response"] == result["executor_result"], "终止说明不该被再包一层"
    assert "CODE_AGENT_TASK_TOKEN_BUDGET" in body, "要说清是被哪个上限掐断的"
    assert result["step_count"] == 1, f"只跑完第 1 块就该停下，实际 {result['step_count']}"


async def test_budget_message_is_the_fixed_chinese_one():
    """两种情形（开始前就超 / 跑到一半超）都用同一套固定中文说明。"""
    before = ma._budget_message(total_tokens=200_000, step_count=0, tool_calls=3)
    midway = ma._budget_message(
        total_tokens=208_509, step_count=11, tool_calls=44, last="最后一段结论"
    )

    assert "任务在「开始执行前」就停了" in before
    assert "任务在「第 11 步之后」停下" in midway
    for text in (before, midway):
        assert "成本保险丝" in text, "要把'预算终止'与'任务做错了'分开说"
        assert "208509" in text or "200000" in text
        assert ma._SIDE_EFFECTS_KEPT in text
        assert ma._TERMINATION_TAIL in text
    assert "最后一段结论" in midway


class _FailingVerifier:
    """恒判 FAIL 的假 Verifier（用来造"打回重跑"）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def astream(self, inputs, config=None):
        self.calls += 1
        yield {
            "agent": {"messages": [AIMessage(content='{"verdict": "FAIL", "reason": "再改改"}')]}
        }


class _FailThenBudgetExecutor(_StepwiseExecutor):
    """第一轮**不报 token**（正常跑完 → 被 Verifier 打回），第二轮才报 ⇒ 造出"重跑时撞预算"。"""

    async def astream(self, inputs, config=None):
        self.tokens_per_step = 0 if self.calls == 0 else 100
        async for chunk in super().astream(inputs, config):
            yield chunk


async def test_budget_on_a_retry_round_is_not_wrapped_as_a_failed_verification(
    tmp_path, thread_id, fake_planner, monkeypatch
):
    """🔴 **R5 的真实形状**：上一轮的裁定还留在 state 里，被预算掐断的那一轮**不该**被包装成
    "任务执行完成，但验收未通过" —— 那正是第 2 题现场读到的、完全看不懂的混合文案。

    触发条件：第 1 轮执行完 → Verifier 判 FAIL（`verdict` 非空）→ 打回重跑 → 第 2 轮**撞上预算**。
    此时最终回复若走"验收未通过"那条包装分支，就会变成
    「任务执行完成，但验收未通过（已重试 1 次）：验收意见：再改改…执行结果：【已终止·预算】…」。
    """
    monkeypatch.setattr(ma, "TASK_TOKEN_BUDGET", 50)
    token = cx.CancelToken()
    verifier = _FailingVerifier()
    agent = _FailThenBudgetExecutor(token, _writing_tool(tmp_path), steps=2, cancel_after=None)

    result = await _run(tmp_path, token, agent, verifier=verifier, thread=thread_id)

    assert result["budget_exceeded"] is True
    assert result["retry_count"] == 1, "应当先被打回一次，才有'重跑时撞预算'"
    assert verifier.calls == 1, "预算掐断后不该再跑验收（第一轮那次是正常的）"
    body = result["final_response"]
    assert not body.startswith("任务执行完成"), f"预算终止被包装成验收失败了：{body[:120]}"
    assert "但验收未通过" not in body
    assert body.startswith("【已终止·预算】"), body[:80]


# ═══════════════════════════════════════════════════════════════════
# 二、R6：终止说明里不许再指向一个不存在的东西
# ═══════════════════════════════════════════════════════════════════


def test_no_termination_text_points_at_a_missing_trace():
    """**源码级守卫**：`multi_agent.py` 的**字符串字面量**里不许再出现"上方工具调用轨迹"。

    历史回放只有最终回复、没有轨迹（`web/server.py::get_session_messages`），
    那句话会把用户引到一个不存在的地方 —— 第 2 题现场就是"文案让用户看轨迹，而回放里没有"。

    ⚠️ 只能用 AST 查**字符串字面量**、不能全文 grep：文件里**注释**提到这个短语是在解释
    "为什么禁止这么写"，全文 grep 会把它自己判红（本用例第一版就是这么挂的）。
    """
    tree = ast.parse(Path(ma.__file__).read_text(encoding="utf-8"))
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    offenders = [text for text in literals if "上方工具调用轨迹" in text or "见上方" in text]
    assert offenders == [], f"终止文案又在指向不存在的东西：{offenders}"

    for text in (
        ma._budget_message(total_tokens=1, step_count=2, tool_calls=3),
        ma._cancel_message(
            cx.CancelToken(), stage="executor", step_count=2, total_tokens=1, tool_calls=3
        ),
    ):
        assert "上方" not in text
        assert "轨迹" not in text, f"终止说明不该承诺轨迹：{text[:80]}"


def test_stop_and_budget_share_the_same_tail():
    """两条终止路径的说明必须**同一套收尾**（P1 ② 的"文案统一"）。"""
    token = cx.CancelToken()
    for text in (
        ma._budget_message(total_tokens=1, step_count=2, tool_calls=3),
        ma._cancel_message(token, stage="executor", step_count=2, total_tokens=1, tool_calls=3),
        ma._cancel_message(token, stage="verifier", step_count=2, total_tokens=1, tool_calls=3),
    ):
        assert ma._SIDE_EFFECTS_KEPT in text
        assert ma._TERMINATION_TAIL in text
