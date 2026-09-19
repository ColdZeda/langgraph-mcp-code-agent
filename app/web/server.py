"""Code Agent 本地 Web 服务 — FastAPI + WebSocket。

启动：uv run uvicorn app.web.server:app --port 8000
- 启动时并行加载 6 个 MCP server 工具 + 文件工具，构建 Executor/Verifier
- WS /ws/chat：每个连接绑定 thread_id 与跨轮 history；同一时刻只跑一个任务
- REST：会话列表 / 模型设置（热切换，改后自动重建 agent）/ 连接测试
- 前端构建产物（app/web/frontend/dist）存在时自动托管
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.code_agent.agent.multi_agent import (
    build_executor_agent,
    build_verifier_agent,
    run_multi_agent,
)
from app.code_agent.config import (
    BROWSER_SERVER_PATH,
    CHECKPOINT_DB,
    CODE_TOOLS_SERVER_PATH,
    MYSQL_SERVER_PATH,
    POWERSHELL_SERVER_PATH,
    RAG_SERVER_PATH,
    RUNTIME_DIR,
    VM_SERVER_PATH,
    setup_logging,
)
from app.code_agent.model.llm import build_llm, set_llm
from app.code_agent.tools.file_tools import file_tools
from app.code_agent.utils.mcp import load_mcp_tools

logger = setup_logging("code_agent.web")

SETTINGS_PATH = RUNTIME_DIR / "web-settings.json"
FRONTEND_DIST = Path(__file__).resolve().parent / "frontend" / "dist"

_MCP_SERVERS = [
    ("powershell", POWERSHELL_SERVER_PATH),
    ("rag", RAG_SERVER_PATH),
    ("browser", BROWSER_SERVER_PATH),
    ("vm", VM_SERVER_PATH),
    ("mysql", MYSQL_SERVER_PATH),
    ("code_tools", CODE_TOOLS_SERVER_PATH),
]


class AgentRuntime:
    """工具与 agent 的进程级持有者；模型切换后整体重建。"""

    def __init__(self) -> None:
        self.tools: list = []
        self.executor_agent = None
        self.verifier_agent = None

    async def load(self) -> None:
        results = await asyncio.gather(
            *(load_mcp_tools(client_id=cid, server_path=sp) for cid, sp in _MCP_SERVERS)
        )
        tools = [t for tool_set in results for t in tool_set]
        tools.extend(file_tools)
        self.tools = tools
        self.rebuild_agents()
        logger.info(f"Web 服务加载 {len(tools)} 个工具，Executor/Verifier 构建完成")

    def rebuild_agents(self) -> None:
        self.executor_agent = build_executor_agent(self.tools)
        self.verifier_agent = build_verifier_agent(self.tools)


runtime = AgentRuntime()
task_lock = asyncio.Lock()  # Agent 依赖本机环境（Edge/WSL/MySQL），同一时刻只跑一个任务


# ── 设置持久化（runtime/ 已被 gitignore，key 只落本地）──


def load_settings() -> dict:
    if SETTINGS_PATH.exists():
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_settings(settings: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def masked_settings(settings: dict) -> dict:
    key = settings.get("api_key") or ""
    return {
        "model": settings.get("model") or "",
        "base_url": settings.get("base_url") or "",
        "api_key_set": bool(key),
        "api_key_tail": key[-4:] if len(key) >= 8 else "",
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    await runtime.load()
    settings = load_settings()
    if settings.get("model") or settings.get("base_url"):
        set_llm(model=settings.get("model"), base_url=settings.get("base_url"))
        runtime.rebuild_agents()
        logger.info(f"已应用本地模型设置: model={settings.get('model')}")
    yield


app = FastAPI(title="Code Agent Web", lifespan=lifespan)


# ── 模型设置 ──


@app.get("/api/settings")
async def get_settings():
    return masked_settings(load_settings())


@app.post("/api/settings")
async def update_settings(body: dict):
    """更新模型设置并热切换（只覆盖传入字段；api_key 传空串表示清除为 .env 默认）。"""
    settings = load_settings()
    for field in ("model", "base_url", "api_key"):
        if field in body:
            value = str(body.get(field) or "").strip()
            if value:
                settings[field] = value
            else:
                settings.pop(field, None)
    save_settings(settings)
    set_llm(
        model=settings.get("model") or None,
        base_url=settings.get("base_url") or None,
        api_key=settings.get("api_key") or None,
    )
    runtime.rebuild_agents()
    return {"ok": True, **masked_settings(settings)}


@app.post("/api/settings/test")
async def test_settings(body: dict):
    """用给定参数试连一次 LLM（不落盘、不切换当前实例）。"""
    try:
        test_llm = build_llm(
            model=str(body.get("model") or "").strip() or None,
            base_url=str(body.get("base_url") or "").strip() or None,
            api_key=str(body.get("api_key") or "").strip() or None,
        )
        start = time.time()
        await test_llm.ainvoke("回复两个字：正常")
        return {"ok": True, "elapsedSec": round(time.time() - start, 1)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ── 会话（读 checkpointer 的 SQLite）──


def _uuid6_to_unix(checkpoint_id: str) -> float | None:
    """LangGraph 的 checkpoint_id 是 UUIDv6（时间有序）→ 转成 unix 秒，供界面排序/显示。

    解析失败返回 None（界面自己兜底），绝不因为时间戳解析问题让接口报错。
    """
    try:
        h = checkpoint_id.replace("-", "")
        ts_100ns = int(h[0:12] + h[13:16], 16)  # UUIDv6：time_high(48) + time_low(12)
        return ts_100ns / 1e7 - 12219292800  # 1582-10-15 → unix 纪元
    except Exception:
        return None


@app.get("/api/sessions")
async def list_sessions():
    """列出历史会话（来自 checkpoint 数据库，按最近活跃排序）。"""
    if not CHECKPOINT_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(CHECKPOINT_DB))
        try:
            rows = conn.execute(
                "SELECT thread_id, COUNT(*) AS n, MAX(checkpoint_id) AS last_id "
                "FROM checkpoints GROUP BY thread_id"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:  # 库还没建表/被占用 → 当作空列表
        logger.warning(f"读取会话列表失败：{e}")
        return []

    items = [
        {
            "threadId": tid,
            "checkpointCount": n,
            "updatedAt": _uuid6_to_unix(last_id) if last_id else None,
        }
        for tid, n, last_id in rows
    ]
    items.sort(key=lambda x: x["updatedAt"] or 0, reverse=True)
    return items


@app.get("/api/sessions/{thread_id}/messages")
async def get_session_messages(thread_id: str):
    """回放某个会话的历史消息（从 checkpointer 恢复），供前端"点历史会话继续聊"。"""
    if not CHECKPOINT_DB.exists():
        return {"threadId": thread_id, "messages": []}
    async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINT_DB)) as saver:
        tup = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
    if not tup:
        return {"threadId": thread_id, "messages": []}

    raw = (tup.checkpoint.get("channel_values") or {}).get("messages") or []
    messages = []
    for m in raw:
        role = "user" if m.__class__.__name__ == "HumanMessage" else "assistant"
        content = m.content if isinstance(m.content, str) else str(m.content)
        messages.append({"role": role, "content": content[:8000]})
    return {"threadId": thread_id, "messages": messages}


# ── WebSocket 聊天 ──


def _summarize_trace(trace_list: list[dict], limit: int = 30, arg_chars: int = 200) -> list[dict]:
    out = []
    for item in (trace_list or [])[:limit]:
        args = item.get("args", {})
        try:
            args_text = json.dumps(args, ensure_ascii=False)
        except Exception:
            args_text = str(args)
        out.append({"name": item.get("name", "?"), "args": args_text[:arg_chars]})
    return out


@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    state = {"thread_id": str(uuid.uuid4())[:8]}
    # 连接建立后**主动**把当前 threadId 推给前端 —— 否则界面一直显示"(连接后自动生成)"，
    # 而后端其实已经在用这个随机 ID 了（前端之前只在收到 new_session 的回复时才拿到 ID）。
    await ws.send_text(json.dumps({"type": "session", "threadId": state["thread_id"]}))
    logger.info(f"WS 连接建立 thread_id={state['thread_id']}")

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({"type": "error", "message": "消息不是合法 JSON"}))
                continue

            mtype = msg.get("type")
            if mtype == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
                continue
            if mtype == "new_session":
                state = {"thread_id": str(uuid.uuid4())[:8]}
                await ws.send_text(json.dumps({"type": "session", "threadId": state["thread_id"]}))
                continue
            if mtype == "load_session":
                # 切换到一个历史会话：只更新 thread_id，历史由前端调
                # GET /api/sessions/{id}/messages 拉取回放。
                requested = str(msg.get("threadId") or "").strip()
                if not requested:
                    await ws.send_text(
                        json.dumps({"type": "error", "message": "load_session 缺少 threadId"})
                    )
                    continue
                state["thread_id"] = requested
                await ws.send_text(json.dumps({"type": "session", "threadId": requested}))
                continue
            if mtype != "chat":
                await ws.send_text(
                    json.dumps({"type": "error", "message": f"未知消息类型 {mtype}"})
                )
                continue

            # 执行模式：由前端下拉框随每条消息带上（白名单校验，非法值回落 auto）
            mode = str(msg.get("mode") or "auto").strip().lower()
            if mode not in ("auto", "single", "multi"):
                mode = "auto"

            user_input = str(msg.get("message") or "").strip()
            if not user_input:
                await ws.send_text(json.dumps({"type": "error", "message": "消息不能为空"}))
                continue
            if msg.get("threadId"):
                state["thread_id"] = str(msg["threadId"])

            if task_lock.locked():
                await ws.send_text(
                    json.dumps(
                        {"type": "error", "code": "busy", "message": "有任务正在执行，请等待完成"}
                    )
                )
                continue

            async with task_lock:
                start = time.time()
                await ws.send_text(
                    json.dumps(
                        {
                            "type": "start",
                            "threadId": state["thread_id"],
                            "message": "Planner → Executor → Verifier 协作中...",
                        }
                    )
                )
                try:
                    result = await run_multi_agent(
                        user_input,
                        runtime.tools,
                        executor_agent=runtime.executor_agent,
                        verifier_agent=runtime.verifier_agent,
                        thread_id=state["thread_id"],
                        mode=mode,
                    )
                except Exception as e:
                    logger.exception("任务执行失败")
                    await ws.send_text(
                        json.dumps({"type": "error", "message": f"{type(e).__name__}: {e}"})
                    )
                    continue

                elapsed = round(time.time() - start, 1)
                # 跨轮记忆由 checkpointer 落库（run_multi_agent 内部完成），这里不再手写 history

                await ws.send_text(
                    json.dumps(
                        {
                            "type": "result",
                            "threadId": state["thread_id"],
                            "plan": result["plan"],
                            "verdict": result["verdict"],
                            "finalResponse": result["final_response"],
                            "toolTrace": _summarize_trace(result.get("executor_trace_list")),
                            "tokenUsage": result.get("token_usage", 0),
                            "stepCount": result.get("step_count", 0),
                            "retryCount": result.get("retry_count", 0),
                            "mode": result.get("mode", mode),
                            "route": result.get("route", ""),
                            "elapsedSec": elapsed,
                        },
                        ensure_ascii=False,
                    )
                )
    except WebSocketDisconnect:
        logger.info(f"WS 连接断开 thread_id={state['thread_id']}")
    except Exception as e:
        logger.exception("WS 异常")
        try:
            await ws.send_text(json.dumps({"type": "error", "message": f"{type(e).__name__}: {e}"}))
        except Exception:
            pass


# ── 前端静态托管（构建产物存在时）──

if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
