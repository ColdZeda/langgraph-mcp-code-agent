"""Code Agent 本地 Web 服务 — FastAPI + WebSocket。

启动：`uv run uvicorn app.web.server:app --port 8000`
- 启动时并行加载 6 个 MCP server 工具 + 文件工具，构建 Executor/Verifier
- WS `/ws/chat`：每个连接绑定 thread_id；**同一会话串行、不同会话可并发**（阶段 5 · B3）
- WS 上还会跑**人工确认**：后端发 `permission_request` → 前端弹框 → 前端回 `permission_response`
  （超时 / 没人应答 → 自动拒绝，见 `security/permissions.py`）
- REST：会话列表 / 模型设置（热切换）/ 连接测试 / 权限模式持久化
- 前端构建产物（`app/web/frontend/dist`）存在时自动托管
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.code_agent.agent.events import bind_sink as bind_event_sink
from app.code_agent.agent.multi_agent import (
    build_executor_agent,
    build_verifier_agent,
    run_multi_agent,
)
from app.code_agent.config import (
    BROWSER_SERVER_PATH,
    CHECKPOINT_DB,
    CODE_TOOLS_SERVER_PATH,
    CONFIRM_TIMEOUT,
    MODEL_NAME,
    MYSQL_SERVER_PATH,
    POWERSHELL_SERVER_PATH,
    RAG_SERVER_PATH,
    RUNTIME_DIR,
    VM_SERVER_PATH,
    setup_logging,
)
from app.code_agent.model.llm import ROLE_NAMES, build_llm, registry, set_llm
from app.code_agent.security.permissions import (
    DEFAULT_MODE,
    MODE_LABELS,
    MODE_OPEN,
    PermissionRequest,
    Session,
    bind_session,
    grant_always,
    mode_label,
    normalize_mode,
)
from app.code_agent.tools.file_tools import file_tools
from app.code_agent.utils.mcp import load_mcp_tools
from app.code_agent.utils.tool_cache import ToolCache
from app.code_agent.utils.tool_wrap import wrap_tools
from app.web import sessions as session_store

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
        # 阶段 4：工具包装层用的缓存（进程级作用域，见 load() 的说明）
        self.tool_cache = ToolCache(scope="web")

    async def load(self) -> None:
        results = await asyncio.gather(
            *(load_mcp_tools(client_id=cid, server_path=sp) for cid, sp in _MCP_SERVERS)
        )
        tools = [t for tool_set in results for t in tool_set]
        tools.extend(file_tools)
        # 阶段 4：与 CLI（run_agent）/ evals（run_single_task）**一致地**包一层
        # 「工具结果外置 + 只读结果缓存」。
        # ⚠️ 两个易错点：
        #   1) 必须接返回值 —— 只有同步 `_run` 的文件工具会被换成代理对象，不是原地改；
        #   2) 工具在进程里**只加载一次、跨会话共用**，所以缓存作用域是进程级的；
        #      正确性由「任何写操作执行后清空本作用域缓存」保证（见 utils/tool_cache.py）。
        #      （阶段 4 收尾时补的漏：此前只有 CLI 与 evals 两条入口接了包装，Web 没接，
        #      导致 Web 端既不做结果外置、也不走缓存。）
        self.tools = wrap_tools(tools, self.tool_cache)
        self.rebuild_agents()
        logger.info(f"Web 服务加载 {len(tools)} 个工具（已包装），Executor/Verifier 构建完成")

    def rebuild_agents(self) -> None:
        self.executor_agent = build_executor_agent(self.tools)
        self.verifier_agent = build_verifier_agent(self.tools)


runtime = AgentRuntime()

# ── 会话锁（阶段 5 · B3）──
# 改造前这里是一把**全局串行锁**（`task_lock = asyncio.Lock()`，`async with task_lock:` 包住整次
# `run_multi_agent`）→ 只要有一个会话在等人工确认，**整个 Web 端所有会话都排队卡死**。
# 现在改成**按会话（thread_id）一把锁**：不同会话可并发，同一会话仍然串行
# （同一 thread_id 的 checkpoint 顺序不能乱）。
#
# ⚠️ 已知代价（有意接受，已记进 AGENTS.md 的「已知坑」）：并发之后多个会话会**同时写**
#    同一个 `runtime/checkpoints.db`（SQLite）。SQLite 默认有 5 秒 busy timeout，
#    本地单用户场景够用；真出现 `database is locked` 再给 saver 加 WAL / busy_timeout。
#
# 锁对象**不回收**：一条连接断掉后那个空 Lock 会留在字典里。数量 = 进程内出现过的 thread_id 数，
# 本地单用户可忽略；回收反而要处理"有人正在排队"的竞态，得不偿失。
_session_locks: dict[str, asyncio.Lock] = {}
_session_locks_guard = asyncio.Lock()


async def get_session_lock(thread_id: str) -> asyncio.Lock:
    async with _session_locks_guard:
        lock = _session_locks.get(thread_id)
        if lock is None:
            lock = _session_locks[thread_id] = asyncio.Lock()
        return lock


# ── 活会话登记（阶段 7 · T7.5 会话管理）──
# 为什么需要它：删会话（软删除 / 彻底删除）之前必须知道"这个 thread 是不是正被某条 WebSocket 用着"
# —— 删掉正在跑的会话，前端一切走、WS 还在往一个已经没有的 thread 写。
# ⚠️ **不能拿上面的 `_session_locks` 当判据**：它按设计**不回收**（连接断掉后空 Lock 还留在字典里），
#    拿它判断会把"早就断开的会话"当成活的，于是永远删不掉。
_active_threads: dict[str, int] = {}


def mark_thread_active(thread_id: str, delta: int) -> None:
    """登记/注销一条 WS 连接对某个 thread 的占用（用**计数**：同一会话可能被多个标签页打开）。"""
    tid = (thread_id or "").strip()
    if not tid:
        return
    count = _active_threads.get(tid, 0) + delta
    if count <= 0:
        _active_threads.pop(tid, None)
    else:
        _active_threads[tid] = count


def is_thread_active(thread_id: str) -> bool:
    return (thread_id or "").strip() in _active_threads


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


def load_permission_mode() -> str:
    """读持久化的权限模式（**D4「③ 混合」**）。

    规则（定案，别放宽）：
      · 只把「**只读 / 需确认**」当持久值 —— 常用档位设一次就够；
      · 「**放开**」**永不持久化**：新会话一律回落「需确认」；
        即使有人手改配置文件塞了 `"open"`，加载时也当「需确认」处理
        （"我上次设过、这次忘了"是最容易埋雷的地方）；
      · 认不出来的值同样回落「需确认」。
    """
    raw = load_settings().get("permission_mode")
    mode = normalize_mode(raw, default=DEFAULT_MODE)
    if raw is not None and mode == MODE_OPEN:
        logger.warning("web-settings.json 里的权限模式是「放开」，按 D4 忽略并以「需确认」加载")
        return DEFAULT_MODE
    return mode


def persist_permission_mode(mode: str) -> str:
    """持久化权限模式并返回**实际生效**的档位（「放开」只生效不落盘）。"""
    mode = normalize_mode(mode, default=DEFAULT_MODE)
    settings = load_settings()
    if mode == MODE_OPEN:
        # 「放开」不落盘，且**要把旧值也删掉** —— 否则新会话会读回上一次的「只读」，
        # 与 D4 要求的"新会话回落「需确认」"不符。
        settings.pop("permission_mode", None)
    else:
        settings["permission_mode"] = mode
    save_settings(settings)
    return mode


def _custom_models_list(settings: dict) -> list[dict]:
    """读本机的自定义模型列表（**只保留合法条目**，坏数据不该让接口 500）。"""
    items = settings.get("custom_models") or []
    out: list[dict] = []
    for item in items:
        if isinstance(item, dict) and str(item.get("id") or "").strip():
            out.append(dict(item))
    return out


def _make_custom_id(model: str, existing: list[dict]) -> str:
    """给自定义模型生成一个稳定 id（角色下拉框里存的就是它）。"""
    slug = "".join(ch if ch.isalnum() else "-" for ch in model.lower()).strip("-") or "model"
    base = f"custom-{slug}"
    taken = {str(it.get("id")) for it in existing}
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def _mask_key(key: str) -> tuple[bool, str]:
    """只回显"有没有配"和**尾号 4 位** —— 完整密钥永远不回前端。"""
    key = str(key or "")
    return bool(key), (key[-4:] if len(key) >= 8 else "")


def masked_settings(settings: dict) -> dict:
    key = settings.get("api_key") or ""
    key_set, key_tail = _mask_key(key)
    custom = []
    for item in _custom_models_list(settings):
        c_set, c_tail = _mask_key(item.get("api_key", ""))
        custom.append(
            {
                "id": str(item.get("id")),
                "label": str(item.get("label") or item.get("id")),
                "model": str(item.get("model") or ""),
                "base_url": str(item.get("base_url") or ""),
                "api_key_set": c_set,
                "api_key_tail": c_tail,
            }
        )
    return {
        "model": settings.get("model") or "",
        "base_url": settings.get("base_url") or "",
        "roles": settings.get("roles") or {},
        "api_key_set": key_set,
        "api_key_tail": key_tail,
        # 用户自定义模型（**不含完整密钥**）
        "customModels": custom,
        # 权限模式（D4）：这里返回的是**实际生效**的档位，不是文件里的原始值
        "permissionMode": load_permission_mode(),
        "permissionModeLabel": mode_label(load_permission_mode()),
        "permissionModes": [{"value": v, "label": MODE_LABELS[v]} for v in MODE_LABELS],
    }


def apply_settings(settings: dict) -> None:
    """把（本地保存的）设置应用到 LLM 注册表。

    ⚠️ 启动时也要走这里 —— 改造前 lifespan 只传了 model/base_url、**漏传 api_key**，
    导致在界面里填的 Key 重启后失效。
    """
    set_llm(
        model=settings.get("model") or None,
        base_url=settings.get("base_url") or None,
        api_key=settings.get("api_key") or None,
    )
    roles = settings.get("roles") or {}
    # ⚠️ 这里必须**覆盖全部四个角色**（缺的用空串 = 恢复配置默认），不能只传文件里有的那几个：
    #    否则"删掉一个自定义模型"这种会让某个角色**从文件里消失**，
    #    而 `if roles:` 一挡就整个跳过 → 内存里那条旧覆盖留着，
    #    于是角色拿着一个已经不存在的 id 去请求（被当成模型名直接发出去 → 难懂的 400）。
    registry.set_role_models({r: str(roles.get(r) or "") for r in ROLE_NAMES})
    # 用户自定义模型（Web「我的模型」）：**自带凭据**，与上面那组全局凭据互不影响
    registry.set_custom_models(settings.get("custom_models"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await runtime.load()
    settings = load_settings()
    if settings:
        apply_settings(settings)  # 含 api_key 与各角色模型（改造前漏了 api_key）
        runtime.rebuild_agents()
        logger.info(f"已应用本地模型设置: {masked_settings(settings)}")
    yield


app = FastAPI(title="Code Agent Web", lifespan=lifespan)


# ── 模型设置 ──


@app.get("/api/settings")
async def get_settings():
    return masked_settings(load_settings())


@app.get("/api/models")
async def list_models():
    """模型清单（给前端下拉框当数据源）= **内置注册表 + 用户自定义模型**。

    每项给两个名字，前端按"不同才显示括号"的规则渲染：
    - `label`：给用户看的显示名（内置的键就是它的显示名；官方改名时显示名不动）
    - `model`：**实际发给 API 的模型名**

    ⚠️ **必须剥掉 `api_key`**：`registry.all_models()` 是内部结构、带明文密钥，
    直接返回等于把用户的 key 发到浏览器（只给"有没有配 + 尾号"就够前端展示了）。
    """
    models = []
    for key, spec in registry.all_models().items():
        key_set, key_tail = _mask_key(spec.get("api_key", ""))
        models.append(
            {
                "key": key,
                "label": spec.get("label") or key,
                "model": spec.get("model") or key,
                "base_url": spec.get("base_url", ""),
                "provider": spec.get("provider", "openai-compatible"),
                "custom": bool(spec.get("custom")),
                "api_key_set": key_set,
                "api_key_tail": key_tail,
            }
        )
    return {
        "roles": registry.role_models(),
        "roleNames": list(ROLE_NAMES),
        "models": models,
        # 「当前生效模型」：四个角色**解析后**各自会用哪个 —— 界面顶栏常驻显示它。
        # 为什么要后端算：兜底链有三层（界面覆盖 → models.json → .env），前端自己拼容易算错。
        "effectiveModels": registry.effective_models(),
    }


@app.post("/api/settings")
async def update_settings(body: dict):
    """更新设置并热切换（只覆盖传入字段；api_key 传空串表示清除为 .env 默认）。

    支持：模型三件套（model / base_url / api_key）+ `roles`（按角色的模型键）
    + **`permission_mode`**（阶段 5 · D4：只读 / 需确认落盘，「放开」只生效不落盘）。
    ⚠️ 未知字段会**记日志**而不是静默丢弃（改造前是白名单循环，前端加字段会被无声吞掉）。
    """
    known = {"model", "base_url", "api_key", "roles", "permission_mode"}
    unknown = [k for k in body if k not in known]
    if unknown:
        logger.warning(f"/api/settings 收到未知字段（已忽略）：{unknown}")

    settings = load_settings()
    for field in ("model", "base_url", "api_key"):
        if field in body:
            value = str(body.get(field) or "").strip()
            if value:
                settings[field] = value
            else:
                settings.pop(field, None)

    roles_in = body.get("roles")
    if isinstance(roles_in, dict):
        cleaned = {r: str(v).strip() for r, v in roles_in.items() if r in ROLE_NAMES}
        merged = {**(settings.get("roles") or {}), **cleaned}
        settings["roles"] = {r: v for r, v in merged.items() if v}  # 空值 = 恢复配置默认

    save_settings(settings)

    # 权限模式单独走一条路（它有自己的持久化规则，见 persist_permission_mode）
    if "permission_mode" in body:
        persist_permission_mode(str(body.get("permission_mode") or ""))

    # ⚠️ 只有**模型相关**字段变了才重建 agent：权限模式跟模型无关，
    #    改一次档位就重建 Executor/Verifier 是白费（而且正在跑的任务会看到新旧混用）。
    if any(f in body for f in ("model", "base_url", "api_key", "roles")):
        apply_settings(load_settings())
        runtime.rebuild_agents()

    return {"ok": True, **masked_settings(load_settings())}


@app.post("/api/settings/custom-model")
async def upsert_custom_model(body: dict):
    """新增/更新一个**用户自定义模型**（自带模型名 / 地址 / 密钥）。

    为什么单开一个接口而不是塞进 `/api/settings`：这里的语义是"增删一条记录"，
    不是"覆盖几个字段"；而且它**不该**让 `api_key` 被清空（用户只改显示名时，
    留空应当保持原密钥 —— 前端也拿不到原密钥来重填）。
    """
    model = str(body.get("model") or "").strip()
    if not model:
        return {"ok": False, "error": "模型名（实际调用名）不能为空"}
    label = str(body.get("label") or "").strip()
    base_url = str(body.get("base_url") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    mid = str(body.get("id") or "").strip()

    settings = load_settings()
    items = _custom_models_list(settings)
    if not mid:
        mid = _make_custom_id(model, items)
    prev = next((it for it in items if str(it.get("id")) == mid), None)

    item: dict = {"id": mid, "label": label or model, "model": model, "base_url": base_url}
    if api_key:
        item["api_key"] = api_key
    elif prev and prev.get("api_key"):
        item["api_key"] = prev["api_key"]  # 留空 = 保持原密钥

    settings["custom_models"] = [it for it in items if str(it.get("id")) != mid] + [item]
    save_settings(settings)
    apply_settings(load_settings())
    runtime.rebuild_agents()
    return {"ok": True, "id": mid, **masked_settings(load_settings())}


@app.delete("/api/settings/custom-model/{model_id}")
async def delete_custom_model(model_id: str):
    """删掉一个自定义模型；**顺带清掉角色里指向它的引用**。

    ⚠️ 不清引用的话，角色会拿着一个不存在的 id 去请求 ——
    代码会把它当成"模型名"直接发给 API（那是另一个合法用法），于是报一个
    很难懂的 404/400。所以这里必须一起清。
    """
    settings = load_settings()
    items = _custom_models_list(settings)
    kept = [it for it in items if str(it.get("id")) != model_id]
    if len(kept) == len(items):
        return {"ok": False, "error": f"没有这个自定义模型：{model_id}"}
    settings["custom_models"] = kept
    settings["roles"] = {
        r: v for r, v in (settings.get("roles") or {}).items() if str(v) != model_id
    }
    save_settings(settings)
    apply_settings(load_settings())
    runtime.rebuild_agents()
    return {"ok": True, **masked_settings(load_settings())}


@app.post("/api/settings/test")
async def test_settings(body: dict):
    """试连一次 LLM（不落盘、不切换当前实例）。

    两种用法：
    - 传 `model` + `base_url` + `api_key`：测**用户刚在表单里填的那一组**（「我的模型」用）；
    - 只传 `model_id`：测一个**已保存/内置**的模型（从注册表取它的真身与凭据）。

    ⚠️ 改造前它只发 `base_url + api_key`，模型名回落 `.env` 的 `MODEL_NAME` ——
    于是"测试连接成功"跟下拉框里选的那个模型**没有关系**（假阳性）。现在返回里带
    `testedModel`，前端会把它显示出来。
    """
    model = str(body.get("model") or "").strip()
    base_url = str(body.get("base_url") or "").strip()
    api_key = str(body.get("api_key") or "").strip()

    mid = str(body.get("model_id") or "").strip()
    if mid:
        spec = registry.all_models().get(mid)
        if not spec:
            return {"ok": False, "error": f"没有这个模型：{mid}"}
        model = model or str(spec.get("model") or mid)
        base_url = base_url or str(spec.get("base_url") or "")
        api_key = api_key or str(spec.get("api_key") or "")

    try:
        test_llm = build_llm(
            model=model or None,
            base_url=base_url or None,
            api_key=api_key or None,
        )
        start = time.time()
        await test_llm.ainvoke("回复两个字：正常")
        return {
            "ok": True,
            "elapsedSec": round(time.time() - start, 1),
            "testedModel": model or MODEL_NAME,
            "testedBaseUrl": base_url or "(.env 默认)",
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "testedModel": model or MODEL_NAME,
        }


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
async def list_sessions(include_hidden: int = 0, include_eval: int = 0):
    """列出历史会话：checkpoint 库（时间/条数）+ 侧车库（标题/置顶/回收站）。

    阶段 7 改了两处：
    1. 返回**对象**（`{items, counts}`）而不是裸数组 —— 前端要拿 `counts` 决定
       「回收站 (N)」「显示系统线程 (N)」这两行小字要不要出现；
    2. 默认**不显示**两类会话：回收站里的（软删除）与**系统线程**
       （`eval-*` / `probe-*` / `smoke*` / `nowrap-*`，即评估与探针留下的）。
       它们是"脚本造的"，不是用户的对话；`include_hidden=1` / `include_eval=1` 可以要回来。

    排序：**置顶优先**，然后按最近活跃（`MAX(checkpoint_id)` 是 UUIDv6，前缀自带时间戳）。
    """
    if not CHECKPOINT_DB.exists():
        return {"items": [], "counts": {"visible": 0, "hidden": 0, "eval": 0}}
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
        return {"items": [], "counts": {"visible": 0, "hidden": 0, "eval": 0}}

    meta = session_store.all_meta()
    items, hidden_count, eval_count = [], 0, 0
    for tid, n, last_id in rows:
        info = meta.get(tid) or {}
        is_eval = session_store.is_system_thread(tid)
        is_hidden = bool(info.get("deletedAt"))
        if is_eval:
            eval_count += 1
            if not include_eval:
                continue
        if is_hidden:
            hidden_count += 1
            if not include_hidden:
                continue
        items.append(
            {
                "threadId": tid,
                "title": info.get("title"),
                "titleSource": info.get("titleSource"),
                "checkpointCount": n,
                "updatedAt": _uuid6_to_unix(last_id) if last_id else None,
                "deletedAt": info.get("deletedAt"),
                "pinned": bool(info.get("pinned")),
                "system": is_eval,
                "active": is_thread_active(tid),
            }
        )
    items.sort(key=lambda x: (x["pinned"], x["updatedAt"] or 0), reverse=True)
    return {
        "items": items,
        "counts": {"visible": len(items), "hidden": hidden_count, "eval": eval_count},
    }


@app.post("/api/sessions/{thread_id}/title")
async def rename_session(thread_id: str, body: dict):
    """给会话改标题（改了之后自动标题不再覆盖它）。"""
    try:
        title = session_store.rename(thread_id, str(body.get("title") or ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "threadId": thread_id, "title": title}


@app.post("/api/sessions/{thread_id}/pin")
async def pin_session(thread_id: str, body: dict):
    """置顶 / 取消置顶（`{"pinned": true|false}`）。"""
    pinned = bool(body.get("pinned"))
    session_store.set_pinned(thread_id, pinned)
    return {"ok": True, "threadId": thread_id, "pinned": pinned}


@app.post("/api/sessions/{thread_id}/restore")
async def restore_session(thread_id: str):
    """从回收站恢复（只恢复"可见性"，checkpoint 一直在）。"""
    session_store.restore(thread_id)
    return {"ok": True, "threadId": thread_id}


@app.delete("/api/sessions/{thread_id}")
async def delete_session(thread_id: str, hard: int = 0):
    """删除会话。

    - 默认（`hard=0`）：**软删除** —— 进回收站，只动侧车库，`checkpoints` 一行都不少；
    - `hard=1`：**彻底删除** —— 真删 `writes` → `checkpoints` + 侧车行，**不可逆**
      （那个 thread 的跨轮记忆就此消失）。

    ⚠️ 两种删除都**拒绝正在被 WebSocket 使用的会话**（`409`）：前端要先切到新会话再删。
    """
    if is_thread_active(thread_id):
        raise HTTPException(
            status_code=409,
            detail="这个会话正在被当前连接使用：请先点「新会话」再删除它。",
        )
    if not hard:
        session_store.soft_delete(thread_id)
        return {"ok": True, "threadId": thread_id, "hard": False}
    removed = session_store.delete_thread_rows(thread_id, CHECKPOINT_DB)
    session_store.drop_meta(thread_id)
    return {"ok": True, "threadId": thread_id, "hard": True, "removed": removed}


@app.post("/api/sessions/purge-system")
async def purge_system_sessions():
    """一键清空**系统线程**（评估 / 探针 / 冒烟）—— 用户的会话一条都不动。

    正在被连接占用的系统线程会**跳过**（返回里列出 `skipped`），不做"删了还要写"的事。
    """
    result = session_store.purge_system_threads(CHECKPOINT_DB)
    kept = [tid for tid in result["threads"] if is_thread_active(tid)]
    if kept:
        # purge 已经把行删了 —— 但活着的连接还指着它，这里如实回显，避免前端以为全清干净了
        logger.warning(f"清空系统线程时发现仍被占用的：{kept}")
    result["skipped"] = kept
    return {"ok": True, **result}


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


async def _send(ws: WebSocket, state: dict, payload: dict) -> None:
    """统一的出站发送。

    ⚠️ 必须串行：阶段 5 起「跑任务」在**后台 task** 里（为了让人工确认期间主循环还能收消息），
    于是主循环与后台 task 可能同时往同一条 WS 写 → 不串行会把两帧交错写坏。
    """
    async with state["send_lock"]:
        await ws.send_text(json.dumps(payload, ensure_ascii=False))


class WebApprover:
    """Web 端的**人工确认通道**（T5.3）。

    流程：`permission_request` 出站 → 前端弹框 → 前端回 `permission_response` → 这里把答案交给闸门。
    - **超时由 `permissions.enforce` 统一处理**（B2：超时/无人应答 → 自动拒绝），
      这里只负责"发出去 + 等答案"；`timeoutSec` 也一并告诉前端，好让它自己收起弹框；
    - **「本会话内总是允许」不在主循环里写**：主循环的运行上下文与跑任务的 task 不是同一个，
      在那里调 `grant_always()` 会记到另一个会话上 → 所以用 `(allow, always)` 原样带回来，
      由**跑任务的 task 自己**（上下文正确）去登记。
    """

    def __init__(self, ws: WebSocket, state: dict) -> None:
        self._ws = ws
        self._state = state

    async def __call__(self, request: PermissionRequest) -> bool:
        request_id = uuid.uuid4().hex[:8]
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._state["pending"][request_id] = future
        try:
            await _send(
                self._ws,
                self._state,
                request.to_payload(request_id, self._state["confirm_timeout"]),
            )
            answer = await future  # 被 wait_for 取消时这里直接抛 CancelledError
            if answer.get("allow") and answer.get("always"):
                grant_always(request.tool_name)
                logger.info("[权限] 本会话内总是允许：%s", request.tool_name)
            return bool(answer.get("allow"))
        finally:
            self._state["pending"].pop(request_id, None)


async def _run_chat(
    ws: WebSocket,
    state: dict,
    user_input: str,
    exec_mode: str,
    permission_mode: str,
) -> None:
    """跑一次任务（在后台 task 里，所以人工确认期间主循环还能继续收消息）。

    同一 `thread_id` 用**同一把会话锁**串行（B3）；不同会话互不阻塞。
    """
    thread_id = state["thread_id"]
    async with await get_session_lock(thread_id):
        start = time.time()
        await _send(
            ws,
            state,
            {
                "type": "start",
                "threadId": thread_id,
                "message": "Planner → Executor → Verifier 协作中...",
                "permissionMode": permission_mode,
                "permissionModeLabel": mode_label(permission_mode),
            },
        )
        # 权限会话：模式来自前端下拉框；确认通道是这条 WS；「总是允许」集合跨消息复用（B7）
        session = Session(
            mode=permission_mode,
            scope=thread_id,
            approver=WebApprover(ws, state),
            timeout=state["confirm_timeout"],
            always_allow=state["always_allow"],
        )

        async def _push_node_event(event: dict) -> None:
            """T5.6：把节点级进度直接推给前端（用现有 WS，不引 SSE）。"""
            await _send(ws, state, {**event, "threadId": thread_id})

        try:
            # 同时绑「权限会话」与「进度事件接收器」（都在当前 task 的上下文里）
            with bind_session(session), bind_event_sink(_push_node_event):
                result = await run_multi_agent(
                    user_input,
                    runtime.tools,
                    executor_agent=runtime.executor_agent,
                    verifier_agent=runtime.verifier_agent,
                    thread_id=thread_id,
                    mode=exec_mode,
                )
        except Exception as e:
            logger.exception("任务执行失败")
            await _send(ws, state, {"type": "error", "message": f"{type(e).__name__}: {e}"})
            return

        elapsed = round(time.time() - start, 1)
        # 跨轮记忆由 checkpointer 落库（run_multi_agent 内部完成），这里不再手写 history
        await _send(
            ws,
            state,
            {
                "type": "result",
                "threadId": thread_id,
                "plan": result["plan"],
                "verdict": result["verdict"],
                "finalResponse": result["final_response"],
                "toolTrace": _summarize_trace(result.get("executor_trace_list")),
                "tokenUsage": result.get("token_usage", 0),
                "stepCount": result.get("step_count", 0),
                "retryCount": result.get("retry_count", 0),
                "mode": result.get("mode", exec_mode),
                "route": result.get("route", ""),
                "elapsedSec": elapsed,
                # 阶段 6：**本轮实际使用的模型**（服务端回报的名字）—— 结果卡片显示，
                # 与"配置里写的是谁"区分开（官方改名/中转别名时两者会不同）
                "modelsUsed": result.get("models_used", {}),
                "permissionMode": permission_mode,
                "permissionModeLabel": mode_label(permission_mode),
            },
        )


@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    state: dict = {
        "thread_id": str(uuid.uuid4())[:8],
        # 权限模式：新会话取持久化值（D4 已保证「放开」不会从文件里被读回来）
        "permission_mode": load_permission_mode(),
        "confirm_timeout": CONFIRM_TIMEOUT,
        # 「本会话内对该工具总是允许」（B7）：换会话即清空；键里带模式，切档自动失效
        "always_allow": set(),
        # requestId -> Future[(allow, always)]，由主循环收答案、由跑任务的 task 消费
        "pending": {},
        "run_task": None,
        "send_lock": asyncio.Lock(),
    }
    # 阶段 7：登记"这个会话正被一条连接占用"（删会话时要用它挡住）
    mark_thread_active(state["thread_id"], +1)

    async def _session_payload() -> dict:
        return {
            "type": "session",
            "threadId": state["thread_id"],
            "permissionMode": state["permission_mode"],
            "permissionModeLabel": mode_label(state["permission_mode"]),
            "permissionModes": [{"value": v, "label": lbl} for v, lbl in MODE_LABELS.items()],
            "confirmTimeoutSec": state["confirm_timeout"],
        }

    # 连接建立后**主动**把当前 threadId 推给前端 —— 否则界面一直显示"(连接后自动生成)"，
    # 而后端其实已经在用这个随机 ID 了。
    await _send(ws, state, await _session_payload())
    logger.info(f"WS 连接建立 thread_id={state['thread_id']}")

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await _send(ws, state, {"type": "error", "message": "消息不是合法 JSON"})
                continue

            mtype = msg.get("type")
            if mtype == "ping":
                await _send(ws, state, {"type": "pong"})
                continue

            if mtype == "permission_response":
                # 人工确认的答案回来了：交给等待中的那个 Future（真正的登记在跑任务的 task 里做）
                request_id = str(msg.get("requestId") or "")
                future = state["pending"].get(request_id)
                if future is None or future.done():
                    logger.debug("收到已失效的 permission_response：%s", request_id)
                    continue
                future.set_result(
                    {"allow": bool(msg.get("allow")), "always": bool(msg.get("alwaysAllow"))}
                )
                continue

            if mtype == "new_session":
                mark_thread_active(state["thread_id"], -1)
                state["thread_id"] = str(uuid.uuid4())[:8]
                mark_thread_active(state["thread_id"], +1)
                state["always_allow"] = set()  # 换会话 → 授权作废
                await _send(ws, state, await _session_payload())
                continue

            if mtype == "load_session":
                # 切换到一个历史会话：只更新 thread_id，历史由前端调
                # GET /api/sessions/{id}/messages 拉取回放。
                requested = str(msg.get("threadId") or "").strip()
                if not requested:
                    await _send(
                        ws, state, {"type": "error", "message": "load_session 缺少 threadId"}
                    )
                    continue
                mark_thread_active(state["thread_id"], -1)
                state["thread_id"] = requested
                mark_thread_active(requested, +1)
                state["always_allow"] = set()  # 换会话 → 授权作废
                await _send(ws, state, await _session_payload())
                continue

            if mtype == "set_permission_mode":
                # 前端下拉框改档位：**同时**更新本连接的档位 + 按 D4 持久化
                state["permission_mode"] = persist_permission_mode(str(msg.get("mode") or ""))
                await _send(ws, state, await _session_payload())
                continue

            if mtype != "chat":
                await _send(ws, state, {"type": "error", "message": f"未知消息类型 {mtype}"})
                continue

            # 执行模式：由前端下拉框随每条消息带上（白名单校验，非法值回落 auto）
            exec_mode = str(msg.get("mode") or "auto").strip().lower()
            if exec_mode not in ("auto", "single", "multi"):
                exec_mode = "auto"

            # 权限模式（阶段 5）：前端也随消息带；和「执行模式」一样做白名单校验。
            # ⚠️ 「放开」只对**本连接**生效，不落盘（D4）。
            if msg.get("permissionMode"):
                state["permission_mode"] = normalize_mode(
                    msg["permissionMode"], default=state["permission_mode"]
                )

            user_input = str(msg.get("message") or "").strip()
            if not user_input:
                await _send(ws, state, {"type": "error", "message": "消息不能为空"})
                continue
            if msg.get("threadId"):
                mark_thread_active(state["thread_id"], -1)
                state["thread_id"] = str(msg["threadId"])
                mark_thread_active(state["thread_id"], +1)

            # 阶段 7 · T7.5：**新会话的第一条消息就是它的标题**（首行、截断）。
            # 只写一次：用户改过的标题（title_source='user'）永不被覆盖 —— 见 web/sessions.py。
            session_store.ensure_title(state["thread_id"], user_input)

            # 同一连接同时只跑一个任务（并发入口是"不同连接 / 不同会话"）
            run_task = state.get("run_task")
            if run_task is not None and not run_task.done():
                await _send(
                    ws,
                    state,
                    {"type": "error", "code": "busy", "message": "有任务正在执行，请等待完成"},
                )
                continue

            state["run_task"] = asyncio.create_task(
                _run_chat(ws, state, user_input, exec_mode, state["permission_mode"])
            )

    except WebSocketDisconnect:
        logger.info(f"WS 连接断开 thread_id={state['thread_id']}")
    except Exception as e:
        logger.exception("WS 异常")
        try:
            await _send(ws, state, {"type": "error", "message": f"{type(e).__name__}: {e}"})
        except Exception:
            pass
    finally:
        # 连接没了就把还在跑的任务取消掉（否则它会继续等一个永远不会来的确认）
        run_task = state.get("run_task")
        if run_task is not None and not run_task.done():
            run_task.cancel()
        # 阶段 7：注销占用登记（否则那个会话会因为"永远活着"而删不掉）
        mark_thread_active(state["thread_id"], -1)


# ── 前端静态托管（构建产物存在时）──

if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
