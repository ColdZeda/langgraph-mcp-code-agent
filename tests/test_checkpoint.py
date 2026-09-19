"""跨轮记忆（checkpointer）测试 —— T1.2 的核心验收。

要证明的三件事：
1. **进程重启后还记得**：同一个 `thread_id`，新的一次 `run_multi_agent` 能读到上一轮的对话；
2. **不同 thread 不串档**；
3. **重试不会污染记忆**：一轮任务最多只在记忆里留下 1 对 (任务, 最终回复)。
"""

import sys
from pathlib import Path
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.agent import multi_agent as ma

PLAN_JSON = '{"goal": "g", "steps": ["s1"], "verify_tools": ["read_file_range"]}'
FAIL_VERDICT = '{"verdict": "FAIL", "reason": "再改改"}'
PASS_VERDICT = '{"verdict": "PASS", "reason": "ok"}'


class _FakeLLM:
    def __init__(self, content: str = PLAN_JSON) -> None:
        self.content = content

    async def ainvoke(self, messages, **kwargs):
        return AIMessage(
            content=self.content,
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )


class _FakeAgent:
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
def isolated_db(monkeypatch, tmp_path):
    db = tmp_path / "checkpoints.db"
    monkeypatch.setattr(ma, "CHECKPOINT_DB", db)
    monkeypatch.setattr(ma, "get_llm", lambda *a, **k: _FakeLLM())
    return db


async def _run(task: str, reply: str, verdict: str, thread_id: str, repeats: int = 1):
    """跑一轮任务，返回 (executor, result)。"""
    executor = _FakeAgent([reply] * repeats)
    verifier = _FakeAgent([verdict] * repeats)
    result = await ma.run_multi_agent(
        task, [], executor_agent=executor, verifier_agent=verifier, thread_id=thread_id
    )
    return executor, result


async def test_memory_survives_restart(isolated_db):
    """同一个 thread_id：新的一次调用（等价于重启进程）能恢复上一轮对话。"""
    thread = f"restart-{uuid4().hex[:8]}"

    await _run("第一轮任务：建一个 hello.py", "第一轮已建好 hello.py", PASS_VERDICT, thread)

    # 全新的一组 agent 实例 = 模拟进程重启（记忆只能来自 SQLite）
    executor2, _ = await _run("第二轮任务：再建一个 world.py", "第二轮已建好", PASS_VERDICT, thread)

    seen = [m.content for m in executor2.inputs[0]["messages"]]
    assert any("第一轮任务" in t for t in seen), f"第二轮应看到第一轮的任务，实际：{seen}"
    assert any("第一轮已建好" in t for t in seen), f"第二轮应看到第一轮的回复，实际：{seen}"

    # checkpoint 文件确实被创建了（证明接线生效，而不是靠内存）
    assert isolated_db.exists(), "checkpoint DB 文件应当被创建"


async def test_different_threads_are_isolated(isolated_db):
    """不同 thread_id 之间不能串档。"""
    await _run("会话 A 的任务", "会话 A 的回复", PASS_VERDICT, f"a-{uuid4().hex[:8]}")

    executor_b, _ = await _run(
        "会话 B 的任务", "会话 B 的回复", PASS_VERDICT, f"b-{uuid4().hex[:8]}"
    )
    seen = [m.content for m in executor_b.inputs[0]["messages"]]
    assert not any("会话 A" in t for t in seen), f"会话 B 不应看到会话 A 的记忆：{seen}"


async def test_retry_does_not_pollute_memory(isolated_db):
    """一轮任务里重试多次，记忆里只应留下 1 对 (任务, 最终回复)。"""
    thread = f"retry-{uuid4().hex[:8]}"
    executor = _FakeAgent(["失败版", "失败版", "失败版"])
    verifier = _FakeAgent([FAIL_VERDICT] * 3)

    await ma.run_multi_agent(
        "会失败的任务", [], executor_agent=executor, verifier_agent=verifier, thread_id=thread
    )
    assert executor.calls == ma.MAX_RETRY + 1, "前置条件：确实重试了"

    # 再跑一轮，检查上一轮在记忆里留下了几条
    executor2, _ = await _run("新一轮", "ok", PASS_VERDICT, thread)
    seen = [m.content for m in executor2.inputs[0]["messages"]]
    hits = [t for t in seen if "会失败的任务" in t]
    assert len(hits) == 1, f"重试不应把中间轮次写进记忆，实际写了 {len(hits)} 条：{hits}"
