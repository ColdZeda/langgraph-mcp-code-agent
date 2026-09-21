"""阶段 5 · T5.3 / T5.4 的 **Web 侧**：人工确认、会话锁、权限模式持久化。

为什么打到 **WebSocket 协议层**、而不是直接调函数：
T5.3 真正容易错的地方全在**协议与并发**上 ——
后台 task 与主循环抢同一条 WS、`permission_response` 必须在"跑任务那个上下文"里生效、
超时要能自动拒绝、不同会话不能被一把全局锁串起来。这些只有按协议跑一遍才测得出来。

⚠️ 这里**不连真 MCP server、不调真模型**：`AgentRuntime.load()` 被换成空操作，
`run_multi_agent` 被换成"只调一次 `permissions.enforce()` 的替身" ——
要验的是**权限闸门与协议**，不是 agent 本身（那是 evals 的活）。
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.security import permissions as perm  # noqa: E402

# ═══════════════════════════════════════════════════════════════════
# 夹具
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def web(monkeypatch, tmp_path):
    """Web 应用 + 隔离掉的运行时（设置文件、checkpoint 库都指到 tmp）。"""
    from app.web import server

    monkeypatch.setattr(server, "SETTINGS_PATH", tmp_path / "web-settings.json")
    monkeypatch.setattr(server, "CHECKPOINT_DB", tmp_path / "checkpoints.db")

    async def _no_load():  # 不真的去起 6 个 MCP 子进程
        return None

    monkeypatch.setattr(server.runtime, "load", _no_load)
    return server


def install_fake_run(server, monkeypatch, *, tool="write_file", sleep=0.0, counter=None):
    """把 `run_multi_agent` 换成"只过一次权限闸门"的替身，并记录闸门的结果。"""
    outcome: dict = {}

    async def fake_run(user_input, all_tools, **kwargs):
        if counter is not None:
            counter["now"] += 1
            counter["max"] = max(counter["max"], counter["now"])
        try:
            if sleep:
                await asyncio.sleep(sleep)
            try:
                await perm.enforce(tool, {"path": "a.py"})
                outcome["decision"] = "allowed"
            except perm.PermissionDenied as exc:
                outcome["decision"] = exc.decision
        finally:
            if counter is not None:
                counter["now"] -= 1
        return {
            "plan": "计划",
            "verdict": "PASS",
            "final_response": "完成",
            "executor_trace_list": [],
            "token_usage": 0,
            "step_count": 0,
            "retry_count": 0,
            "mode": kwargs.get("mode", "auto"),
            "route": "",
        }

    monkeypatch.setattr(server, "run_multi_agent", fake_run)
    return outcome


def chat(ws, text="写个文件", **extra):
    ws.send_json({"type": "chat", "message": text, **extra})


def audit_decisions(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        json.loads(line)["decision"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


# ═══════════════════════════════════════════════════════════════════
# 一、确认流程（允许 / 拒绝 / 超时 / 总是允许）
# ═══════════════════════════════════════════════════════════════════


def test_confirm_allow_lets_the_tool_run(web, monkeypatch, tmp_path):
    outcome = install_fake_run(web, monkeypatch)
    log = tmp_path / "plog.txt"
    monkeypatch.setattr(perm, "PERMISSIONS_LOG", log)

    with TestClient(web.app) as client, client.websocket_connect("/ws/chat") as ws:
        assert ws.receive_json()["type"] == "session"
        chat(ws, permissionMode="confirm")
        assert ws.receive_json()["type"] == "start"

        request = ws.receive_json()
        assert request["type"] == "permission_request"
        assert request["tool"] == "write_file"
        assert request["highRisk"] is False
        assert "a.py" in request["args"], "确认框要能看到关键参数"
        assert request["timeoutSec"] > 0

        ws.send_json(
            {"type": "permission_response", "requestId": request["requestId"], "allow": True}
        )
        assert ws.receive_json()["type"] == "result"

    assert outcome["decision"] == "allowed"
    assert audit_decisions(log) == ["allowed_by_user"]


def test_confirm_deny_blocks_the_tool(web, monkeypatch, tmp_path):
    outcome = install_fake_run(web, monkeypatch)
    log = tmp_path / "plog.txt"
    monkeypatch.setattr(perm, "PERMISSIONS_LOG", log)

    with TestClient(web.app) as client, client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        chat(ws, permissionMode="confirm")
        ws.receive_json()
        request = ws.receive_json()
        ws.send_json(
            {"type": "permission_response", "requestId": request["requestId"], "allow": False}
        )
        assert ws.receive_json()["type"] == "result"

    assert outcome["decision"] == "denied_by_user"
    assert audit_decisions(log) == ["denied_by_user"]


def test_no_answer_times_out_into_rejection(web, monkeypatch, tmp_path):
    """**B2**：没人应答（前端关掉 / 用户走开）→ 自动拒绝，绝不能超时放行。"""
    monkeypatch.setattr(web, "CONFIRM_TIMEOUT", 0.3)
    outcome = install_fake_run(web, monkeypatch)
    log = tmp_path / "plog.txt"
    monkeypatch.setattr(perm, "PERMISSIONS_LOG", log)

    with TestClient(web.app) as client, client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        chat(ws, permissionMode="confirm")
        ws.receive_json()
        request = ws.receive_json()
        assert request["type"] == "permission_request" and request["timeoutSec"] == 0.3
        # 故意不回 —— 等后端自己超时
        assert ws.receive_json()["type"] == "result"

    assert outcome["decision"] == "deny_timeout"
    assert audit_decisions(log) == ["deny_timeout"]


def test_always_allow_is_remembered_within_the_session(web, monkeypatch):
    """**B7**：勾了「本会话内对该工具总是允许」之后，同一个工具不再弹第二次。"""
    install_fake_run(web, monkeypatch)

    with TestClient(web.app) as client, client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        chat(ws, permissionMode="confirm")
        ws.receive_json()
        request = ws.receive_json()
        ws.send_json(
            {
                "type": "permission_response",
                "requestId": request["requestId"],
                "allow": True,
                "alwaysAllow": True,
            }
        )
        assert ws.receive_json()["type"] == "result"

        # 第二次同工具：**不该再弹**（start 之后直接就是 result）
        chat(ws, permissionMode="confirm")
        assert ws.receive_json()["type"] == "start"
        assert ws.receive_json()["type"] == "result"


def test_new_session_drops_the_always_allow_grants(web, monkeypatch):
    """换会话 → 授权作废（B7：只在当前会话内有效）。"""
    install_fake_run(web, monkeypatch)

    with TestClient(web.app) as client, client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        chat(ws, permissionMode="confirm")
        ws.receive_json()
        request = ws.receive_json()
        ws.send_json(
            {
                "type": "permission_response",
                "requestId": request["requestId"],
                "allow": True,
                "alwaysAllow": True,
            }
        )
        ws.receive_json()

        ws.send_json({"type": "new_session"})
        assert ws.receive_json()["type"] == "session"

        chat(ws, permissionMode="confirm")
        ws.receive_json()
        assert ws.receive_json()["type"] == "permission_request", "换会话后必须重新问"


# ═══════════════════════════════════════════════════════════════════
# 二、另外两档（只读不弹、放开不弹）
# ═══════════════════════════════════════════════════════════════════


def test_readonly_mode_rejects_without_asking(web, monkeypatch, tmp_path):
    outcome = install_fake_run(web, monkeypatch)
    log = tmp_path / "plog.txt"
    monkeypatch.setattr(perm, "PERMISSIONS_LOG", log)

    with TestClient(web.app) as client, client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        chat(ws, permissionMode="readonly")
        assert ws.receive_json()["type"] == "start"
        # 只读档**直接拒绝**，所以第一条消息就是 result（不该出现 permission_request）
        assert ws.receive_json()["type"] == "result"

    assert outcome["decision"] == "deny_mode"
    assert audit_decisions(log) == ["deny_mode"]


def test_open_mode_does_not_ask(web, monkeypatch):
    outcome = install_fake_run(web, monkeypatch)

    with TestClient(web.app) as client, client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        chat(ws, permissionMode="open")
        assert ws.receive_json()["type"] == "start"
        assert ws.receive_json()["type"] == "result"

    assert outcome["decision"] == "allowed"


def test_open_mode_from_frontend_is_not_persisted(web):
    """D4：从 WS 切到「放开」只对**本连接**生效，落盘值为空 → 新会话回落「需确认」。"""
    with TestClient(web.app) as client, client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        ws.send_json({"type": "set_permission_mode", "mode": "open"})
        session_msg = ws.receive_json()
        assert session_msg["permissionMode"] == "open"
    assert "permission_mode" not in web.load_settings()

    with TestClient(web.app) as client, client.websocket_connect("/ws/chat") as ws:
        assert ws.receive_json()["permissionMode"] == "confirm", "新会话必须回落「需确认」"


def test_set_permission_mode_persists_readonly(web):
    with TestClient(web.app) as client, client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        ws.send_json({"type": "set_permission_mode", "mode": "readonly"})
        assert ws.receive_json()["permissionMode"] == "readonly"

    with TestClient(web.app) as client, client.websocket_connect("/ws/chat") as ws:
        assert ws.receive_json()["permissionMode"] == "readonly"


# ═══════════════════════════════════════════════════════════════════
# 三、B3：会话锁（不同会话必须能并发）
# ═══════════════════════════════════════════════════════════════════


def test_different_sessions_run_concurrently(web, monkeypatch):
    """**B3 的回归测试**：两个会话必须能**同时在跑**。

    改造前是 `async with task_lock:`（一把全局锁）→ 两个会话会被串起来；
    更糟的是"需确认"档下有一个会话在等人点确认时，**整个 Web 端都排队卡死**。
    这里用"同时在跑的计数"把差别测出来：全局锁下最大值只能是 1。
    """
    counter = {"now": 0, "max": 0}
    install_fake_run(web, monkeypatch, sleep=0.4, counter=counter)

    with (
        TestClient(web.app) as client,
        client.websocket_connect("/ws/chat") as a,
        client.websocket_connect("/ws/chat") as b,
    ):
        a.receive_json()
        b.receive_json()
        chat(a, permissionMode="open")
        chat(b, permissionMode="open")

        assert a.receive_json()["type"] == "start"
        assert b.receive_json()["type"] == "start"
        a.receive_json()
        b.receive_json()

    assert counter["max"] == 2, f"两个会话没有并发（最大同时在跑 {counter['max']}）"


def test_same_connection_rejects_a_second_task(web, monkeypatch):
    install_fake_run(web, monkeypatch, sleep=0.4)

    with TestClient(web.app) as client, client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        chat(ws, permissionMode="open")
        assert ws.receive_json()["type"] == "start"
        # 第一个还没跑完，再发一条 → 应当被"忙"挡掉（不是排队）
        chat(ws, permissionMode="open")
        busy = ws.receive_json()
        assert busy["type"] == "error" and busy.get("code") == "busy"
        assert ws.receive_json()["type"] == "result"


# ═══════════════════════════════════════════════════════════════════
# 四、D4 持久化语义（不需要 WS）
# ═══════════════════════════════════════════════════════════════════


def test_readonly_and_confirm_round_trip(web):
    web.persist_permission_mode("readonly")
    assert web.load_permission_mode() == "readonly"
    web.persist_permission_mode("confirm")
    assert web.load_permission_mode() == "confirm"


def test_open_mode_clears_the_old_value(web):
    """切到「放开」时要把旧值也删掉 —— 否则新会话会读回「只读」，与 D4 不符。"""
    web.persist_permission_mode("readonly")
    web.persist_permission_mode("open")

    assert "permission_mode" not in web.load_settings()
    assert web.load_permission_mode() == "confirm"


def test_hand_edited_open_in_settings_file_is_ignored(web):
    """配置文件被手改成 "open" → 加载时按「需确认」处理（D4 明写）。"""
    web.save_settings({"permission_mode": "open"})
    assert web.load_permission_mode() == "confirm"


def test_garbage_mode_falls_back_to_confirm(web):
    web.save_settings({"permission_mode": "随便写的"})
    assert web.load_permission_mode() == "confirm"


def test_masked_settings_exposes_permission_mode(web):
    web.persist_permission_mode("readonly")
    data = web.masked_settings(web.load_settings())

    assert data["permissionMode"] == "readonly"
    assert data["permissionModeLabel"] == "只读"
    assert {m["value"] for m in data["permissionModes"]} == {"readonly", "confirm", "open"}


def test_post_settings_changes_permission_mode_without_rebuilding_agents(web, monkeypatch):
    rebuilt = []
    monkeypatch.setattr(web.runtime, "rebuild_agents", lambda: rebuilt.append(1))

    with TestClient(web.app) as client:
        resp = client.post("/api/settings", json={"permission_mode": "readonly"})

    assert resp.status_code == 200
    assert resp.json()["permissionMode"] == "readonly"
    assert rebuilt == [], "只改权限档位不该重建 Executor/Verifier"


# ═══════════════════════════════════════════════════════════════════
# 五、T5.6：节点级进度推送（用现有 WS，不引 SSE）
# ═══════════════════════════════════════════════════════════════════


def test_node_events_are_pushed_over_the_websocket(web, monkeypatch):
    """跑任务期间前端应当**陆续**收到 `node` 事件，而不是干等到最后的 `result`。"""
    from app.code_agent.agent import events as ev

    async def fake_run(user_input, all_tools, **kwargs):
        for event in (
            {"type": "node", "node": "route", "status": "end", "route": "complex"},
            {"type": "node", "node": "planner", "status": "start"},
            {"type": "node", "node": "planner", "status": "end", "steps": 3},
            {
                "type": "node",
                "node": "executor",
                "status": "step",
                "step": 1,
                "tools": ["read_file"],
            },
            {"type": "node", "node": "verifier", "status": "end", "passed": True},
        ):
            await ev.emit(event)
        return {
            "plan": "p",
            "verdict": "",
            "final_response": "完成",
            "executor_trace_list": [],
            "token_usage": 0,
            "step_count": 1,
            "retry_count": 0,
            "mode": kwargs.get("mode", "auto"),
            "route": "complex",
        }

    monkeypatch.setattr(web, "run_multi_agent", fake_run)

    with TestClient(web.app) as client, client.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        chat(ws, permissionMode="open")

        received = []
        while True:
            msg = ws.receive_json()
            if msg["type"] == "result":
                break
            received.append(msg)

    nodes = [(m["node"], m["status"]) for m in received if m["type"] == "node"]
    assert nodes == [
        ("route", "end"),
        ("planner", "start"),
        ("planner", "end"),
        ("executor", "step"),
        ("verifier", "end"),
    ], f"节点事件没按预期推过来：{received}"
    assert all("threadId" in m for m in received if m["type"] == "node"), "每条都要带 threadId"
    # 结构化字段要留全 —— 文案由前端决定，后端只发数据
    step = next(m for m in received if m.get("status") == "step")
    assert step["step"] == 1 and step["tools"] == ["read_file"]


def test_emit_without_sink_is_a_noop():
    """没绑 sink 时（evals / 单测 / 任何不关心进度的入口）`emit()` 必须静默跳过。"""
    import asyncio

    from app.code_agent.agent import events as ev

    asyncio.run(ev.emit({"type": "node", "node": "planner", "status": "start"}))  # 不抛就算过


def test_broken_sink_does_not_break_the_task():
    """推送失败（比如连接断了）绝不能影响任务本身。"""
    import asyncio

    from app.code_agent.agent import events as ev

    def boom(_event):
        raise RuntimeError("连接断了")

    async def run():
        with ev.bind_sink(boom):
            await ev.emit({"type": "node", "node": "executor", "status": "start"})

    asyncio.run(run())  # 不抛就算过
