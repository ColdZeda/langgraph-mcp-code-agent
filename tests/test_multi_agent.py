"""多 Agent 图的行为测试（mock LLM 与子 agent，不真的调用模型）。

覆盖 T1.1 的修复：`retry_count` 必须真正自增，Verifier 反复判 FAIL 时图要在
MAX_RETRY 次打回后正常结束（而不是无限打回、直到撞 RecursionError / 超时）。

⚠️ T1.2 之后记忆是真的持久化的（checkpointer 按 thread_id 落 SQLite）→
测试必须**隔离**：checkpoint DB 指向临时文件 + 每个用例用独立 thread_id，
否则用例之间会互相读到对方的对话。
"""

import sys
from pathlib import Path
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.agent import multi_agent as ma


class _FakeRegistry:
    """假注册表：planner 节点走 registry.chain("planner")。

    ⚠️ 只 patch get_llm 是不够的 —— 那样测试会**真的调用模型**（实测每次 13~19 秒）。
    """

    def __init__(self, llm):
        self._llm = llm

    def chain(self, role="executor"):
        return [self._llm]

    def role_models(self):
        return {r: "fake-model" for r in ("planner", "executor", "verifier", "router")}


PLAN_JSON = '{"goal": "g", "steps": ["s1", "s2"], "verify_tools": ["read_file_range"]}'
FAIL_VERDICT = '{"verdict": "FAIL", "reason": "REASON-XYZ：产物不存在"}'
PASS_VERDICT = '{"verdict": "PASS", "reason": "ok"}'


class _FakeLLM:
    """假 Planner LLM：固定返回计划 JSON，并记录调用。"""

    def __init__(self, content: str = PLAN_JSON) -> None:
        self.content = content
        self.calls: list = []

    async def ainvoke(self, messages, **kwargs):
        self.calls.append(messages)
        return AIMessage(
            content=self.content,
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )


class _FakeAgent:
    """假 Executor / Verifier：把固定回复按 astream 的块协议吐出来。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.calls = 0
        self.inputs: list = []

    async def astream(self, inputs, config=None):
        idx = min(self.calls, len(self.replies) - 1)
        self.calls += 1
        self.inputs.append(inputs)
        yield {"agent": {"messages": [AIMessage(content=self.replies[idx])]}}


@pytest.fixture
def fake_planner(monkeypatch):
    llm = _FakeLLM()
    monkeypatch.setattr(ma, "get_llm", lambda *a, **k: llm)
    monkeypatch.setattr(ma, "registry", _FakeRegistry(llm))
    return llm


@pytest.fixture
def thread_id(monkeypatch, tmp_path):
    """隔离的 checkpoint DB + 独立 thread_id。"""
    monkeypatch.setattr(ma, "CHECKPOINT_DB", tmp_path / "checkpoints.db")
    return f"test-{uuid4().hex[:8]}"


async def test_verifier_fail_retries_then_stops(fake_planner, thread_id):
    """Verifier 恒判 FAIL 时：Executor 跑 MAX_RETRY+1 次后结束，不抛异常。"""
    executor = _FakeAgent([f"exec-{i}" for i in range(1, 6)])
    verifier = _FakeAgent([FAIL_VERDICT] * 5)

    result = await ma.run_multi_agent(
        "一个必然验收失败的任务",
        [],
        executor_agent=executor,
        verifier_agent=verifier,
        thread_id=thread_id,
    )

    assert executor.calls == ma.MAX_RETRY + 1, (
        f"Executor 应执行 MAX_RETRY+1={ma.MAX_RETRY + 1} 次（首次 + {ma.MAX_RETRY} 次重跑），"
        f"实际 {executor.calls} 次"
    )
    assert result["retry_count"] == ma.MAX_RETRY
    assert "FAIL" in result["verdict"].upper()


async def test_verifier_fail_reason_reaches_executor(fake_planner, thread_id):
    """第 2 轮起，Executor 收到的 prompt 里必须带上 Verifier 的 FAIL 原因。"""
    executor = _FakeAgent(["exec-1", "exec-2", "exec-3"])
    verifier = _FakeAgent([FAIL_VERDICT] * 3)

    await ma.run_multi_agent(
        "任务", [], executor_agent=executor, verifier_agent=verifier, thread_id=thread_id
    )

    assert executor.calls >= 2, "至少要有一次重跑，才能验证失败原因是否回灌"
    first_prompt = executor.inputs[0]["messages"][-1].content
    second_prompt = executor.inputs[1]["messages"][-1].content
    assert "REASON-XYZ" not in first_prompt, "首次执行不该带验收意见"
    assert "REASON-XYZ" in second_prompt, "重跑时必须把 FAIL 原因回灌给 Executor"


async def test_verifier_pass_stops_immediately(fake_planner, thread_id):
    """验收通过 → 只跑一次 Executor，retry_count 保持 0。"""
    executor = _FakeAgent(["exec-1", "exec-2"])
    verifier = _FakeAgent([PASS_VERDICT, PASS_VERDICT])

    result = await ma.run_multi_agent(
        "任务", [], executor_agent=executor, verifier_agent=verifier, thread_id=thread_id
    )

    assert executor.calls == 1
    assert result["retry_count"] == 0
    assert "PASS" in result["verdict"].upper()


# ── 阶段 6：「本轮实际使用的模型」（结果卡片显示用）──


def test_msg_model_reads_server_reported_name():
    """取的是服务端**真实回报**的模型名，不是配置里写的那个。

    本机实测过两者的差别：发 `deepseek-v4-flash`，服务端回报 `deepseek-flash`
    （官方 2026-09-10 把旧名路由到了新模型）。
    """
    msg = AIMessage(content="好", response_metadata={"model_name": "deepseek-flash"})
    assert ma._msg_model(msg) == "deepseek-flash"


def test_msg_model_tolerates_missing_metadata():
    assert ma._msg_model(AIMessage(content="x")) == ""


def test_models_of_dedupes_and_sorts():
    msgs = [
        AIMessage(content="a", response_metadata={"model_name": "b-model"}),
        AIMessage(content="b", response_metadata={"model_name": "a-model"}),
        AIMessage(content="c", response_metadata={"model_name": "b-model"}),
        AIMessage(content="d"),
    ]
    assert ma._models_of(msgs) == ["a-model", "b-model"]
    assert ma._models_of(None) == []
