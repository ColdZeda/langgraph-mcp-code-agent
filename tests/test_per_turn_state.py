"""阶段 8 · 实测修复：**每轮通道必须复位**（用户第 3 轮实测撞出来的缺陷）。

现场（2026-10-07 晚，`a8b3133b` 会话）：用户只发了「你好」，Executor 却拿到**上一轮**的上传计划，
对着旧计划跑了 13 次工具调用 / 6 分钟、整段英文；回复里还混进上一轮计划的章节。
根因：LangGraph「没传进 `ainvoke` 的通道保留上一轮 checkpoint 的值」，而 `plan` 从不在复位字典里。

⚠️ 这跟 P0 修过的 **F2（`step_count` 跨轮累加）是同一个根因** —— 所以这里除了行为测试，
还要一条**机械守卫**：`AgentState` 的每个通道都必须被"归类"（每轮复位 / 每轮赋值 / 跨轮累积），
以后谁新增通道忘了分类，测试当场变红。
"""

import sys
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.agent import multi_agent as ma  # noqa: E402

# ═══════════════════════════════════════════════════════════════════
# 一、机械守卫：每个通道都必须被归类
# ═══════════════════════════════════════════════════════════════════


def test_every_state_channel_is_classified():
    """🔴 **新增通道忘了分类 ⇒ 这条会红**（比"事后发现串味"便宜得多）。"""
    channels = set(ma.AgentState.__annotations__)
    classified = set(ma.PER_TURN_RESET) | set(ma.PER_TURN_ASSIGNED) | set(ma.CROSS_TURN_CHANNELS)

    assert channels == classified, (
        f"没被归类的通道：{sorted(channels - classified)}；"
        f"归了类但已不存在的通道：{sorted(classified - channels)}"
    )


def test_cross_turn_channels_are_only_messages():
    """跨轮累积是**例外**，只允许 `messages`（别的通道累积就是串味）。"""
    assert ma.CROSS_TURN_CHANNELS == ("messages",)


def test_per_turn_reset_covers_the_channels_that_actually_bit_us():
    """把现场撞到的那几个名字钉住（防止有人"顺手删掉"某一项）。"""
    for name in ("plan", "verdict", "executor_result", "executor_trace_list", "step_count"):
        assert name in ma.PER_TURN_RESET, f"{name} 必须每轮复位"


def test_per_turn_reset_returns_a_fresh_object():
    """每次都要一份新对象：共享可变默认值会让一轮里改到别的轮。"""
    a, b = ma.per_turn_reset(), ma.per_turn_reset()
    a["executor_trace_list"].append({"name": "x", "args": {}})

    assert b["executor_trace_list"] == [], "复位字典里的列表被共享了"


# ═══════════════════════════════════════════════════════════════════
# 二、行为测试：第二轮不能再拿到第一轮的计划
# ═══════════════════════════════════════════════════════════════════

PLAN_TEXT = "步骤1：在宿主机确认源文件存在并获取绝对路径（第一轮的计划）"


class _FakePlanner:
    """假的 Planner 模型：只回一段固定的计划 JSON。"""

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, messages, **kwargs):
        from langchain_core.messages import AIMessage

        self.calls += 1
        return AIMessage(
            content=f'{{"plan": [{{"step": 1, "action": "{PLAN_TEXT}"}}], "reasoning": "test"}}'
        )


class _PlanSpyExecutor:
    """假的 Executor：把**每次收到的输入**原样记下来，再回一句话。

    ⚠️ 必须与 Verifier 的替身**分开**：合成一个的话，`inputs[1]` 记的会是第一轮
    Verifier 的调用（它的提示词里本来就带计划），断言就假绿/假红了。
    """

    def __init__(self, reply: str = "好的") -> None:
        self.inputs: list[str] = []
        self.reply = reply

    async def astream(self, input_, config=None, **kwargs):
        from langchain_core.messages import AIMessage

        msgs = input_.get("messages") if isinstance(input_, dict) else input_
        text = "\n".join(str(getattr(m, "content", "")) for m in (msgs or []))
        self.inputs.append(text)
        yield {"agent": {"messages": [AIMessage(content=self.reply)]}}


class _FakeRegistry:
    def __init__(self, llm) -> None:
        self._llm = llm

    def chain(self, role="executor"):
        return [self._llm]

    def get(self, role="executor"):
        return self._llm


@pytest.fixture
def patched(tmp_path, monkeypatch):
    monkeypatch.setattr(ma, "CHECKPOINT_DB", tmp_path / "checkpoints.db")
    planner = _FakePlanner()
    monkeypatch.setattr(ma, "get_llm", lambda *a, **k: planner)
    monkeypatch.setattr(ma, "registry", _FakeRegistry(planner))
    return planner


async def test_second_turn_does_not_inherit_the_previous_plan(patched):
    """🔴 现场复现的那条：第一轮留下计划，第二轮判 simple（跳过 Planner）时**不能**再用它。"""
    executor = _PlanSpyExecutor()
    verifier = _PlanSpyExecutor(reply='{"verdict": "PASS", "reason": "测试用，直接通过"}')
    thread = f"perturn-{uuid4().hex[:8]}"

    first = await ma.run_multi_agent(
        "把 readme.txt 上传到 WSL 的 nginx 目录",
        [],
        executor_agent=executor,
        verifier_agent=verifier,
        thread_id=thread,
        mode="multi",  # 走 Planner ⇒ 会产出一段计划
        auto_inject=False,
        auto_deposit=False,
    )
    second = await ma.run_multi_agent(
        "你好",
        [],
        executor_agent=executor,
        verifier_agent=verifier,
        thread_id=thread,
        mode="single",  # 跳过 Planner —— 正是现场出事的那条路
        auto_inject=False,
        auto_deposit=False,
    )

    assert len(executor.inputs) == 2, f"两轮各一次 Executor 调用，实际 {len(executor.inputs)} 次"
    assert PLAN_TEXT in executor.inputs[0], "第一轮本该带着计划（前提没成立，测试无效）"
    assert PLAN_TEXT not in executor.inputs[-1], (
        "🔴 第二轮又拿到了上一轮的计划 —— 这就是现场那个缺陷"
    )
    assert "（无计划）" in executor.inputs[-1], (
        "第二轮的 plan 通道应当是空的 —— 模板在这种情况下会写「（无计划）」"
    )
    assert "被打回" not in executor.inputs[-1], "`verdict` 没复位会让第二轮被当成重跑轮"
    assert not first.get("needs_clarification")
    assert first["retry_count"] == 0, "verifier 回 PASS ⇒ 不该有打回"
    assert second["retry_count"] == 0, "`retry_count` 必须每轮从 0 开始"
