"""run_single_task 的资源清理测试（T1.8）。

## 真实风险（实测校正）

`evals/run_e2e.py` 用 `asyncio.wait_for(run_single_task(...), timeout=240)` 控制单题超时。
**单次取消不会直接导致泄漏** —— 实测：取消在进入清理块之前就已投递完毕，
`await client.__aexit__(...)` 仍能正常跑完，所以 6 个 MCP client 都会被关闭。

真正的风险是「**清理过程中又收到一次取消**」：此时 `__aexit__` 里会抛 `CancelledError`
（属 BaseException），而原来的清理块只 `except Exception` → 那个异常直接冲出循环，
**剩余的 client 一个都不会被关闭**。会触发它的现实场景：清理较慢时用户再按一次 Ctrl-C、
外层还有别的 waiter 再次取消、或进程收到第二个信号。

## 本测试断言

即使清理期间被再次取消，6 个 client 也都会被尝试关闭（逐个 housekeeping）。
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.agent import code_agent as ca

EXPECTED_CLIENTS = 6      # powershell / rag / browser / vm / mysql / code_tools
CLEANUP_SECONDS = 0.15    # 模拟每个 client 关闭 stdio 子进程的耗时


class _FakeClient:
    def __init__(self, client_id: str, closed: list) -> None:
        self.client_id = client_id
        self._closed = closed

    async def __aexit__(self, *exc):
        # 真实 MCP client 的关闭是异步 I/O，这里用 sleep 模拟其耗时
        await asyncio.sleep(CLEANUP_SECONDS)
        self._closed.append(self.client_id)
        return False


async def test_recancel_during_cleanup_still_closes_all_clients(monkeypatch):
    """清理期间再次被取消时，6 个 MCP client 仍然要全部关闭。"""
    closed: list[str] = []

    async def fake_loader(*, client_id, server_path):
        return _FakeClient(client_id, closed), []

    async def fake_run_multi_agent(*args, **kwargs):
        await asyncio.sleep(30)          # 一直挂着，等待取消
        return {}                        # pragma: no cover

    monkeypatch.setattr(ca, "load_mcp_tools_managed", fake_loader)
    monkeypatch.setattr(ca, "run_multi_agent", fake_run_multi_agent)
    monkeypatch.setattr(ca, "build_executor_agent", lambda tools: None)
    monkeypatch.setattr(ca, "build_verifier_agent", lambda tools: None)

    task = asyncio.create_task(ca.run_single_task("任务", thread_id="probe"))
    await asyncio.sleep(0.05)            # 让它跑到 run_multi_agent 处挂起
    task.cancel()                        # 第 1 次取消 = 单题超时
    await asyncio.sleep(0.05)            # 让清理开始（进入第一个 client 的 __aexit__）
    task.cancel()                        # 第 2 次取消 = 清理期间再次被取消

    with pytest.raises(asyncio.CancelledError):
        await task

    # 清理被放进独立任务（gather + shield），外层被取消后它仍在后台继续跑 —— 等它跑完再断言。
    # 这正是修复的意义：**清理不再被取消打断**，只是"等待"被打断。
    await asyncio.sleep(CLEANUP_SECONDS * 3)

    assert len(closed) == EXPECTED_CLIENTS, (
        f"清理期间再次取消后，{EXPECTED_CLIENTS} 个 client 都应被关闭，"
        f"实际只关闭了 {len(closed)} 个：{closed}"
    )
