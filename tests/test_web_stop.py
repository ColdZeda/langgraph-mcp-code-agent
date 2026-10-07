"""阶段 8 · P0 的 **Web 侧**：界面上的「停止」+ 任务状态条（`status` 事件）。

为什么打到 **WebSocket 协议层**：停止真正容易错的地方全在**协议与并发**上 ——
主循环与跑任务的 task 不是同一个上下文（停止开关必须能跨过去）、
点停止时可能正卡在权限弹框上（不收掉弹框就得等满确认超时，停止就形同虚设）、
停止之后**必须有回包**（否则界面永远停在"执行中"）。这些只有按协议跑一遍才测得出。

⚠️ 与 `tests/test_web_permission.py` 同一套做法：不连真 MCP server、不调真模型 ——
`AgentRuntime.load()` 换成空操作，`run_multi_agent` 换成**自己按检查点读停止开关**的替身。
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.security import permissions as perm  # noqa: E402

# `_run_chat` 直接索引的键（少一个就会 KeyError，不是"静默降级"）
BASE_RESULT: dict = {
    "plan": "计划",
    "verdict": "",
    "final_response": "完成",
    "executor_trace_list": [],
    "token_usage": 0,
    "step_count": 0,
    "retry_count": 0,
    "mode": "auto",
    "route": "",
}


@pytest.fixture
def web(monkeypatch, tmp_path):
    """Web 应用 + 隔离掉的运行时（设置文件、checkpoint 库都指到 tmp）。

    与 `test_web_permission.py` 的同名夹具逐行一致（两份都只有十来行；
    抽到 conftest 会让那批权限测试的隔离语义变得含糊，所以刻意各留一份）。
    """
    from app.web import server

    monkeypatch.setattr(server, "SETTINGS_PATH", tmp_path / "web-settings.json")
    monkeypatch.setattr(server, "CHECKPOINT_DB", tmp_path / "checkpoints.db")

    async def _no_load():  # 不真的去起 6 个 MCP 子进程
        return None

    monkeypatch.setattr(server.runtime, "load", _no_load)
    return server


def install_cancellable_run(server, monkeypatch, *, steps=40, delay=0.02, emit_status=True):
    """替身：**自己实现协作式停止**（每个检查点读一次停止开关），并记录跑到了第几步。

    为什么要自己实现而不是复用真图：这一层要验的是**协议**（stop 消息 → 停止开关 → 回包），
    真图那部分的检查点由 `tests/test_cancel.py` 按状态断言守着。
    """
    from app.code_agent.agent import events as ev
    from app.code_agent.agent.cancel import TaskCancelled, current_token, raise_if_cancelled

    state = {"steps": 0}

    async def fake_run(user_input, all_tools, **kwargs):
        for _ in range(steps):
            try:
                raise_if_cancelled()
            except TaskCancelled as exc:
                token = current_token()
                return {
                    **BASE_RESULT,
                    "final_response": "【已停止】测试替身",
                    "cancelled": True,
                    "cancel_reason": exc.reason,
                    "cancel_stage": "executor",
                    "step_count": state["steps"],
                    "status": token.snapshot() if token is not None else None,
                }
            state["steps"] += 1
            if emit_status:
                await ev.emit({"type": "status", "steps": state["steps"], "tokens": 100})
            await asyncio.sleep(delay)
        return {**BASE_RESULT, "cancelled": False, "step_count": state["steps"]}

    monkeypatch.setattr(server, "run_multi_agent", fake_run)
    return state


def chat(ws, text="跑个长任务", **extra):
    ws.send_json({"type": "chat", "message": text, **extra})


def read_until_result(ws, *, limit=200) -> list[dict]:
    """读到 `result` 为止（停止期间还会有 stopping / status / node 事件）。"""
    seen: list[dict] = []
    for _ in range(limit):
        msg = ws.receive_json()
        seen.append(msg)
        if msg["type"] in ("result", "error"):
            return seen
    raise AssertionError(f"没有等到 result/error，收到的是：{seen[-5:]}")


def audit_decisions(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        json.loads(line)["decision"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


# ═══════════════════════════════════════════════════════════════════
# 一、停止：协议 → 停止开关 → 回包
# ═══════════════════════════════════════════════════════════════════


def test_stop_message_stops_a_running_task(web, monkeypatch):
    """点「停止」：任务在**下一个检查点**停下，并且**一定有回包**（界面才不会卡在"执行中"）。"""
    state = install_cancellable_run(web, monkeypatch)

    with TestClient(web.app) as client, client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()  # session
        chat(ws, permissionMode="open")
        assert ws.receive_json()["type"] == "start"

        ws.send_json({"type": "stop"})
        seen = read_until_result(ws)

    kinds = [m["type"] for m in seen]
    assert "stopping" in kinds, f"后端要先回一条 stopping（界面据此显示「停止中…」）：{kinds}"

    result = seen[-1]
    assert result["type"] == "result"
    assert result["cancelled"] is True
    assert result["cancelReason"] == "user"
    assert result["cancelStage"] == "executor"

    assert 0 < state["steps"] < 40, f"应该「跑到一半就被停」，实际 {state['steps']}/40 步"
    assert result["stepCount"] == state["steps"], "回包里的步数要与实际一致"


def test_status_events_reach_the_frontend(web, monkeypatch):
    """任务状态条：后端推的 `status` 事件要带 threadId 一路到前端。"""
    install_cancellable_run(web, monkeypatch, steps=3, delay=0.01, emit_status=True)

    with TestClient(web.app) as client, client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        chat(ws, permissionMode="open")
        seen = read_until_result(ws)

    status = [m for m in seen if m["type"] == "status"]
    assert status, f"没收到状态条事件：{[m['type'] for m in seen]}"
    assert all("threadId" in m for m in status)
    assert status[-1]["steps"] == 3, f"状态条步数不对：{status[-1]}"
    assert seen[-1]["cancelled"] is False, "跑完的任务不该被标成停止"


def test_stop_without_a_running_task_is_a_noop(web, monkeypatch):
    """没有任务在跑时点停止：忽略掉，不报错、不影响下一条消息。"""
    install_cancellable_run(web, monkeypatch, steps=1, delay=0.0, emit_status=False)

    with TestClient(web.app) as client, client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        ws.send_json({"type": "stop"})
        # 没有任务 ⇒ 不该冒出 stopping / error
        chat(ws, permissionMode="open")
        assert ws.receive_json()["type"] == "start"
        seen = read_until_result(ws)

    assert seen[-1]["type"] == "result"
    assert seen[-1]["cancelled"] is False


# ═══════════════════════════════════════════════════════════════════
# 二、停止 vs 人工确认（弹框还开着的时候点停止）
# ═══════════════════════════════════════════════════════════════════


def _install_confirmation_run(server, monkeypatch):
    """替身：整轮任务只过一次权限闸门（用来验"弹框还开着时点停止"）。"""
    from app.code_agent.agent.cancel import TaskCancelled, current_token

    async def fake_run(user_input, all_tools, **kwargs):
        try:
            await perm.enforce("write_file", {"path": "a.py"})
        except TaskCancelled as exc:
            token = current_token()
            return {
                **BASE_RESULT,
                "final_response": "【已停止】弹框被停止收掉了",
                "cancelled": True,
                "cancel_reason": exc.reason,
                "cancel_stage": "executor",
                "status": token.snapshot() if token is not None else None,
            }
        except perm.PermissionDenied as exc:
            return {**BASE_RESULT, "cancelled": False, "final_response": f"被拒：{exc.decision}"}
        return {**BASE_RESULT, "cancelled": False, "final_response": "允许了"}

    monkeypatch.setattr(server, "run_multi_agent", fake_run)


def test_stop_dismisses_the_pending_dialog_and_does_not_look_like_a_denial(
    web, monkeypatch, tmp_path
):
    """⚠️ 弹框还开着时点停止：**立刻**按停止解栈。

    若不收掉弹框，任务要等满确认超时（默认 120 秒）才轮到下一个检查点 ——
    "点了停止还要等两分钟"就是最反直觉的那种 bug。
    同时**不能留一条"用户拒绝了该工具"**的审计：那是另一回事，还会让模型换别的办法接着干。
    """
    log = tmp_path / "permissions.log"
    monkeypatch.setattr(perm, "PERMISSIONS_LOG", log)
    _install_confirmation_run(web, monkeypatch)

    with TestClient(web.app) as client, client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        chat(ws, permissionMode="confirm")
        assert ws.receive_json()["type"] == "start"
        request = ws.receive_json()
        assert request["type"] == "permission_request", "需确认档下写操作要先弹框"

        ws.send_json({"type": "stop"})  # 弹框还开着
        seen = read_until_result(ws)

    assert seen[-1]["type"] == "result", f"停止后必须有回包：{seen}"
    assert seen[-1]["cancelled"] is True
    assert seen[-1]["cancelReason"] == "user"
    assert "denied_by_user" not in audit_decisions(log), "停止不该被记成「用户拒绝了该工具」"


def test_stop_is_still_answered_when_the_signal_escapes_the_graph(web, monkeypatch):
    """纵深防御：`TaskCancelled` 继承 `BaseException`，若它穿过整张图，WS 层也必须接住。

    漏了这条分支的症状是"点了停止之后**一直没有回包**"（比停止失败更糟）。
    """
    from app.code_agent.agent.cancel import TaskCancelled, current_token, raise_if_cancelled

    async def escaping_run(user_input, all_tools, **kwargs):
        await asyncio.sleep(0)  # 让 stop 消息有机会先被主循环处理
        try:
            for _ in range(100):
                raise_if_cancelled()
                await asyncio.sleep(0.01)
        except TaskCancelled:
            raise  # ← 故意让它逃出去（模拟"某个节点没接住"）
        return BASE_RESULT

    monkeypatch.setattr(web, "run_multi_agent", escaping_run)

    with TestClient(web.app) as client, client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        chat(ws, permissionMode="open")
        assert ws.receive_json()["type"] == "start"
        ws.send_json({"type": "stop"})
        seen = read_until_result(ws)

    assert seen[-1]["type"] == "error"
    assert seen[-1]["code"] == "cancelled"
    assert current_token() is None, "跑完（或出错）之后不该还留着停止开关"


# ═══════════════════════════════════════════════════════════════════
# 三、墙钟与配置
# ═══════════════════════════════════════════════════════════════════


def test_start_message_carries_the_wall_clock(web, monkeypatch):
    """`start` 消息要带上墙钟上限（界面据此显示"⏱ 0:12 / 15:00"）。"""
    install_cancellable_run(web, monkeypatch, steps=1, delay=0.0, emit_status=False)

    with TestClient(web.app) as client, client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        chat(ws, permissionMode="open")
        start = ws.receive_json()

    assert start["type"] == "start"
    assert start["wallClockSec"] == pytest.approx(float(web.TASK_WALL_CLOCK))
    assert start["wallClockSec"] > 0, "默认必须是有限值（0 = 不限制，那这条安全网就没了）"


def test_task_wall_clock_default_is_15_minutes():
    """默认 15 分钟（用户 2026-10-07 定的）：正常任务 1~5 分钟，失控任务实测 30 分钟。"""
    from app.code_agent.config import TASK_WALL_CLOCK

    assert TASK_WALL_CLOCK == 900
