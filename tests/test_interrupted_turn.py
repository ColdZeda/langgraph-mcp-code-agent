"""B5 的测试：被硬中断的一轮也必须留档（现场：执行中关页面 ⇒ 整轮从记忆里消失）。

两条：
1. helper 本身真的写进去一对 `(任务, 【本轮被中断】…)`；
2. 图被**取消**（`asyncio.CancelledError`）时，`run_multi_agent` 会**照旧把异常抛出去**
   （不吞 —— Web 端靠它收尾），同时**已经排好**补记。
"""

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.agent import multi_agent as ma  # noqa: E402


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(ma, "CHECKPOINT_DB", tmp_path / "checkpoints.db")
    return tmp_path


async def test_interrupted_turn_is_written_into_memory(isolated_db):
    thread = f"interrupt-{uuid4().hex[:8]}"

    await ma._record_interrupted_turn(thread, "把文件传到 WSL", "连接断开或任务被取消")

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async with AsyncSqliteSaver.from_conn_string(str(ma.CHECKPOINT_DB)) as saver:
        tup = await saver.aget_tuple({"configurable": {"thread_id": thread}})
    messages = (tup.checkpoint.get("channel_values") or {}).get("messages") or []
    texts = [m.content for m in messages if isinstance(m.content, str)]

    assert any("把文件传到 WSL" in t for t in texts), "用户那句话必须留档"
    assert any("【本轮被中断】" in t for t in texts), "中断说明必须留档"
    assert any("连接断开" in t for t in texts), "原因要写清楚（便于下一轮知道发生了什么）"


async def test_helpers_are_noop_on_empty_input(isolated_db):
    """空 thread / 空任务：不许写、不许抛（否则会在收尾路径里制造二次异常）。"""
    await ma._record_interrupted_turn("", "x", "r")
    await ma._record_interrupted_turn("t", "", "r")


def test_reason_text_is_chinese_and_mentions_the_cause():
    assert "连接断开" in ma._interrupt_reason(asyncio.CancelledError())
    assert "ZeroDivisionError" in ma._interrupt_reason(ZeroDivisionError("boom"))


async def test_cancelled_run_records_and_still_raises(isolated_db, monkeypatch):
    """🔴 关键性质：**异常必须照样抛出去**（吞掉它 Web 端会永远挂着），同时补记已排好。"""
    scheduled: list[tuple] = []
    monkeypatch.setattr(ma, "_schedule_interruption_record", lambda *a: scheduled.append(a))

    class _BoomExecutor:
        async def astream(self, *a, **k):
            raise asyncio.CancelledError()
            yield  # pragma: no cover

    thread = f"cancel-{uuid4().hex[:8]}"
    with pytest.raises(asyncio.CancelledError):
        await ma.run_multi_agent(
            "随便一个任务",
            [],
            executor_agent=_BoomExecutor(),
            verifier_agent=_BoomExecutor(),
            thread_id=thread,
            mode="single",
            auto_inject=False,
            auto_deposit=False,
        )

    assert scheduled, "被取消时必须排一条中断补记"
    assert scheduled[0][0] == thread
    assert scheduled[0][1] == "随便一个任务"
