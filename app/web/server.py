"""Code Agent 本地 Web 服务 — FastAPI + WebSocket。

启动：`uv run uvicorn app.web.server:app --port 8000`
- 启动时并行加载 6 个 MCP server 工具 + 文件工具，构建 Executor/Verifier
- WS `/ws/chat`：每个连接绑定 thread_id；**同一会话串行、不同会话可并发**（阶段 5 · B3）
- WS 上还会跑**人工确认**：后端发 `permission_request` → 前端弹框 → 前端回 `permission_response`
  （超时 / 没人应答 → 自动拒绝，见 `security/permissions.py`）
- 阶段 8 · P0：WS 上还能**停止**（前端发 `stop` → 协作式取消 + 墙钟，见 `agent/cancel.py`），
  跑任务期间后端还会推 `status` 事件（任务状态条：时长 / 步数 / 用量）
- REST：会话列表 / 模型设置（热切换）/ 连接测试 / 权限模式持久化
- 前端构建产物（`app/web/frontend/dist`）存在时自动托管
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.code_agent.agent.cancel import CancelToken, TaskCancelled, bind_cancel
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
    TASK_WALL_CLOCK,
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
        # 阶段 7 · T7.6：**还没有可用模型**时记下原因（界面要提示"去添加你的 API Key"）。
        # 以前没这个字段：缺 key 会在 import 期或启动期直接把进程掀掉，用户看不到界面。
        self.model_error = ""
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
        """重建 Executor / Verifier。

        阶段 7 · T7.6：**"还没有可用的模型"不再致命**。
        以前缺 key 时这里会抛 `ValueError("模型 API key 未配置…")`，把启动链
        （`lifespan → load() → rebuild_agents()`）整条掀掉 ⇒ 用户连界面都打不开，
        也就没法在界面里填 key（而"填完 key 自动生效"正是现在的产品路径：
        保存设置 → `apply_settings()` → 这里重建）。
        现在改成：记下原因、把两个 agent 置空，前端据此显示「添加你的 API Key」。
        """
        try:
            self.executor_agent = build_executor_agent(self.tools)
            self.verifier_agent = build_verifier_agent(self.tools)
            self.model_error = ""
        except ValueError as e:  # 典型：还没有模型 / key 未配置
            self.executor_agent = None
            self.verifier_agent = None
            self.model_error = str(e)
            logger.warning(f"还没有可用的模型（等用户在「模型设置」里添加）：{e}")


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


def _clean_context_window(value: Any) -> int | None:
    """阶段 8 · P2：把前端传来的"上下文窗口"规整成正整数（空/脏值 = 不声明）。"""
    try:
        window = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return window if window > 0 else None


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
                # 阶段 8 · P2：模型声明的上下文窗口（三项阈值按它自动算；空 = 按默认 128k）
                "context_window": _clean_context_window(item.get("context_window")),
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

    阶段 7 · T7.6：**没有可用模型时不再致命**。
    这里那句 `set_llm()` 会立刻去建一个 LLM 实例，于是"全新用户还没配 key"时
    会在启动阶段抛 `ValueError` 把服务掀掉（实测：`rebuild_agents()` 已经容错了，
    但 lifespan 里这一步又把整个进程带崩）。现在只记警告 ——
    用户界面里填完 key 保存后，`/api/settings` 会再走一次这个函数，那时就正常建起来了。
    """
    try:
        set_llm(
            model=settings.get("model") or None,
            base_url=settings.get("base_url") or None,
            api_key=settings.get("api_key") or None,
        )
    except ValueError as e:  # 典型：还没有任何可用的 key
        # ⚠️ 措辞（2026-10-08 按用户反馈改）：原来写「暂时建不起模型」——
        #    "建不起"是**内部视角**的词（用户不知道在建什么），而且只指路 `.env` 会把人带偏：
        #    面向用户的入口其实是界面里的「⚙ 模型设置 → 我的模型」（自带 API 地址 + 密钥）。
        #    所以改成"说人话 + 两条路都写清"。
        logger.warning("暂时没有可用模型（还没配置 API key）：%s", e)
        logger.warning(
            "→ 两种配法，任选其一：① 打开 http://127.0.0.1:8000 →「模型设置 → 我的模型」"
            "填上 API 地址与密钥（推荐，改完即生效、不用重启）；"
            "② 在 .env 里设置 MODEL_NAME / MODEL_BASE_URL / MODEL_API_KEY（改完要重启）。"
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
    # ⚠️ 顺序要紧（阶段 7 · T7.6 调整）：**先把本机设置灌进注册表，再建 agent**。
    #    以前是先 `runtime.load()`（里面就 rebuild 一次）再 `apply_settings()` ——
    #    于是第一次建 agent 时注册表还是空的：有 `.env` key 时靠兜底能过，
    #    而没有 `.env`、只有界面里配的模型时，第一遍必然失败并打一条吓人的警告
    #    （要等第二次 rebuild 才成功）。
    settings = load_settings()
    if settings:
        apply_settings(settings)  # 含 api_key 与各角色模型（改造前漏了 api_key）
        # ⚠️ 2026-10-08 按用户反馈改：原来把整个 settings 字典（含全部自定义模型 + 密钥尾号）
        #    dump 成一行 —— 一屏白字里最吵的就是它，而且密钥尾号没必要进日志。改成一句摘要。
        _roles = settings.get("roles") or {}
        _custom = settings.get("custom_models") or []
        _labels = [
            str(m.get("label") or m.get("id") or "?") for m in _custom if isinstance(m, dict)
        ]
        logger.info(
            "已应用本地模型设置：四个角色 → %s；自定义模型 %d 个%s；权限档=%s",
            next(iter(set(_roles.values())), "") or "跟随 .env",
            len(_labels),
            f"（{' / '.join(_labels)}）" if _labels else "",
            settings.get("permissionMode") or "confirm",
        )
    await runtime.load()
    # RAG 的两个本地模型**不在仓库里**（各 ≈87MB，见 scripts/fetch_models.py）——
    # 启动时就把状态说清楚，别让用户"用到一半"才发现（缺向量模型时 RAG 的 4 个工具
    # 会立刻报错并给出装法，见 store.embedding_model_hint）。
    # ⚠️ 局部 import：不让 Web 启动路径平白依赖 rag 那一串（虽然 store 是全懒加载的）。
    from app.code_agent.config import EMBEDDING_MODEL_PATH
    from app.code_agent.rag import store as rag_store

    if rag_store.embedding_model_ready():
        logger.info(f"[RAG] 向量模型已就位：{EMBEDDING_MODEL_PATH}")
    else:
        logger.warning(
            f"[RAG] 向量模型未安装（期望 {EMBEDDING_MODEL_PATH}）→ RAG 的 4 个工具不可用，"
            "其余工具正常。装法：uv run python scripts/fetch_models.py"
        )
    logger.info(
        "[RAG] 精排模型："
        + ("已就位" if rag_store.reranker_model_ready() else "未安装 → 降级为纯向量召回")
    )
    # 阶段 7：启动脚本会在拉起进程前设好这个环境变量 —— 这样"已就绪"这句话正好出现在
    # **能打开界面**的那一刻。脚本自己打的那句 URL 只能打在启动前（那时点开是打不开的），
    # 用户会以为项目坏了（界面第二轮反馈里就是这么踩的）。
    web_url = os.getenv("CODE_AGENT_WEB_URL", "").strip()
    if web_url:
        _log_ready_banner(web_url)
    yield


def _log_stream():
    """日志**真正**写去的那个流。

    ⚠️ 这个项目的 logger 刻意写 **stderr**（`config.get_logger`；MCP server 的 stdout 是 JSON-RPC
    通道，不能污染）⇒ 判断"是不是终端"必须看 stderr，而不是 stdout。
    """
    for handler in logging.getLogger().handlers + logging.getLogger("code_agent.web").handlers:
        stream = getattr(handler, "stream", None)
        if stream is not None:
            return stream
    return sys.stderr


def _enable_ansi(is_stderr: bool) -> bool:
    """Windows 控制台默认不解析 ANSI 转义 —— 显式开一下（失败就当作不支持）。"""
    if os.name != "nt":  # pragma: no cover - 非 Windows 分支
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-12 if is_stderr else -11)  # STD_ERROR/OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))  # VT_PROCESSING
    except Exception:  # noqa: BLE001 —— 任何异常都只是"没有颜色"
        return False


def _color_enabled() -> bool:
    """该不该上色：必须**是终端**（重定向到文件/CI 日志时不要污染），且尊重 `NO_COLOR`。"""
    if os.environ.get("NO_COLOR"):
        return False
    stream = _log_stream()
    try:
        if not stream.isatty():
            return False
    except Exception:  # noqa: BLE001
        return False
    return _enable_ansi(stream is sys.stderr)


def _log_ready_banner(web_url: str) -> None:
    """启动完成时的**醒目**横幅：地址单独一行、带色 —— 用户 Ctrl+左键就能点开。

    ⚠️ 原来的写法是一句普通 INFO（"`[OK] 已就绪 —— 在浏览器打开：…`"），它混在一屏白字里
    根本认不出来（用户反馈："下面一大堆白字看着有点蒙"）。标记 `[OK] 已就绪` **必须保留** ——
    启动脚本里写着"等出现「[OK] 已就绪」再打开浏览器"。
    """
    if _color_enabled():
        bold, cyan, green, dim, reset = (
            "\033[1m",
            "\033[36m",
            "\033[32m",
            "\033[2m",
            "\033[0m",
        )
        line = f"{cyan}{'=' * 62}{reset}"
        logger.info(
            "\n%s\n  %s[OK] 已就绪%s —— 在浏览器打开（%sCtrl + 左键点下面这行%s）：\n"
            "  %s%s➜  %s%s\n%s",
            line,
            f"{bold}{green}",
            reset,
            dim,
            reset,
            bold,
            green,
            web_url,
            reset,
            line,
        )
    else:  # 重定向 / CI / 不支持 ANSI：退化成原来那句（不加转义字符，免得更乱）
        logger.info("[OK] 已就绪 —— 在浏览器打开：%s", web_url)


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
                # 阶段 8 · P2：模型声明的上下文窗口（前端在模型设置里能看到/改到）
                "context_window": _clean_context_window(spec.get("context_window")),
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
    # 阶段 8 · P2：上下文窗口（不填 = 不声明 ⇒ 三项阈值按默认 128k 算）。
    # ⚠️ 语义与 api_key 一样：**留空保持原值**（前端拿不到原值来重填，不能因为没传就抹掉）。
    window = _clean_context_window(body.get("context_window"))
    if window is None:
        window = _clean_context_window((prev or {}).get("context_window"))
    if window is not None:
        item["context_window"] = window

    settings["custom_models"] = [it for it in items if str(it.get("id")) != mid] + [item]

    # 阶段 7 · T7.6：**用户添加的第一个模型 = 四个角色的默认模型**。
    # 为什么必须做：`roles` 为空时，每个角色都会回落到 `.env` 的 `MODEL_NAME`
    # （`llm.py:104-110`），而面向用户的场景里 `.env` 往往是空的 ⇒
    # 只把 Executor 指到新模型的话，其余三个角色会报"key 未配置"（实测踩过）。
    # ⚠️ 只在**完全没配过角色**时补：用户自己选过角色模型就别动它。
    if not (settings.get("roles") or {}):
        settings["roles"] = dict.fromkeys(ROLE_NAMES, mid)

    save_settings(settings)
    apply_settings(load_settings())
    runtime.rebuild_agents()
    return {"ok": True, "id": mid, **masked_settings(load_settings())}


@app.get("/api/knowledge")
async def list_knowledge():
    """列出知识库条目（Web 面板「知识库」）。

    ⚠️ 为什么要有这个界面：**自动沉淀会自己往 `data/knowledge/` 写东西**（用户实测一晚写了 3 条），
    而界面此前完全看不到 —— 「它到底记住了什么」「错的结论怎么删」都无从下手。
    ⚠️ `store` 走**局部 import**：它一 import 就会拉起 chromadb/torch（毫秒级变秒级），
    这是这个项目的既有约定（见 AGENTS「已知坑」的 RAG 一节）。
    """
    try:
        from app.code_agent.rag import store

        return {"ok": True, "items": store.list_documents()}
    except Exception as exc:  # noqa: BLE001 —— 接口不该因为知识库坏了而 500
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "items": []}


@app.delete("/api/knowledge/{name:path}")
async def delete_knowledge(name: str):
    """删掉一条知识：**文件 + 向量**一起删（否则检索还会命中"幽灵条目"）。

    🔴 只接受**目录内的单层文件名**且扩展名必须是 `.txt` / `.md` ——
    这是个"删文件"的接口，路径穿越（`../`）、别的前缀、隐藏文件一律拒绝。
    """
    cleaned = str(name or "").strip().replace("\\", "/")
    if (
        not cleaned
        or "/" in cleaned
        or cleaned.startswith(".")
        or ".." in cleaned
        or Path(cleaned).suffix.lower() not in (".txt", ".md")
    ):
        return {"ok": False, "error": "只接受知识库目录内的单层 .txt / .md 文件名"}

    try:
        from app.code_agent.rag import store

        deleted_file, deleted_vector = store.delete_document_file(cleaned)
        if not deleted_file and not deleted_vector:
            return {"ok": False, "error": f"没找到条目：{cleaned}"}
        logger.info("删除知识条目：%s（文件=%s 向量=%s）", cleaned, deleted_file, deleted_vector)
        return {
            "ok": True,
            "deletedFile": deleted_file,
            "deletedVector": deleted_vector,
            "items": store.list_documents(),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


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


@app.post("/api/settings/test-roles")
async def test_roles():
    """**当前配置里实际用到的每个模型**各测一次（自动去重 + 并发），并说明哪些角色在用它。

    为什么要单开这个接口（阶段 7 · 界面第二轮反馈）：
    - 用户四个角色可能配成不同模型（multi 模式下 Planner / Executor / Verifier / Router
      各干各的活），只测 Executor 说明不了"整条链路通不通"；
    - 但**盲目四个角色各测一次**又会浪费：`/api/settings/test` 是**真的发一次 completion**
      （`ainvoke("回复两个字：正常")`），而四个角色常常共用同一个模型 ⇒ 白跑三次。
    ⇒ 这里按"模型键"去重，一次拿到分组结果：`2 个模型分给 4 个角色` = 发 **2** 次请求。
    """
    roles_by_key: dict[str, list[str]] = {}
    for role in ROLE_NAMES:
        roles_by_key.setdefault(registry.model_key(role), []).append(role)

    effective = registry.effective_models()
    # ⚠️ 角色没单独指定模型时，`model_key()` 返回的是 `.env` 的 `MODEL_NAME`
    #    （一个"裸模型名"，**不在注册表里**）。那种情况要测的是**全局凭据**，
    #    而不是把这个名字当 id 去查注册表（否则会得到"没有这个模型：xxx"的假失败）。
    known = registry.all_models()

    async def probe(key: str) -> dict:
        fallback = key not in known
        result = await test_settings({} if fallback else {"model_id": key})
        info = effective.get(roles_by_key[key][0]) or {}
        return {
            "key": key,
            "label": info.get("label") or key,
            "model": info.get("model") or key,
            "custom": bool(info.get("custom")),
            "fallback": fallback,
            "roles": roles_by_key[key],
            **result,
        }

    groups = list(await asyncio.gather(*[probe(k) for k in roles_by_key]))
    return {"ok": all(g.get("ok") for g in groups), "groups": groups}


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

    阶段 8 · P0：这里创建**本次任务的停止开关**（`CancelToken`），并放进 `state`
    —— 主循环与跑任务的 task **不是同一个上下文**，主循环要能"点停止"就必须拿到同一个对象
    （与 `state["pending"]` 的传法同理）。
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
                # 界面据此显示"墙钟上限"（0 = 不限制）
                "wallClockSec": float(TASK_WALL_CLOCK or 0),
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
        # 阶段 8 · P0：一次任务一个停止开关（墙钟 + 界面「停止」）
        token = CancelToken(wall_clock=TASK_WALL_CLOCK or None)
        state["cancel"] = token

        async def _push_node_event(event: dict) -> None:
            """T5.6：把节点级进度直接推给前端（用现有 WS，不引 SSE）。"""
            await _send(ws, state, {**event, "threadId": thread_id})

        try:
            # 同时绑「权限会话」「进度事件接收器」「停止开关」（都在当前 task 的上下文里）
            with bind_session(session), bind_event_sink(_push_node_event), bind_cancel(token):
                result = await run_multi_agent(
                    user_input,
                    runtime.tools,
                    executor_agent=runtime.executor_agent,
                    verifier_agent=runtime.verifier_agent,
                    thread_id=thread_id,
                    mode=exec_mode,
                )
        except TaskCancelled:
            # 阶段 8 · P0 的**纵深防御**：正常路径下 `run_multi_agent` 自己会兜住停止信号
            # （见那里的 `except TaskCancelled`），这里接住的是"连它都没接住"的情况。
            # ⚠️ 必须显式写这一条：`TaskCancelled` 继承 `BaseException`（为了穿透 ToolNode），
            #    下面那条 `except Exception` 抓不到它 ⇒ 漏了这行用户就会"点了停止之后一直没有回包"。
            #    ⚠️ 也**不能**改成 `except BaseException`：那会把 `asyncio.CancelledError`
            #    （WS 断开时的正常取消）一起吞掉。
            logger.info("停止信号穿到了 WS 层（thread_id=%s）", thread_id)
            await _send(
                ws,
                state,
                {
                    "type": "error",
                    "code": "cancelled",
                    "message": "任务已停止（已产出的文件 / 数据一律保留）。",
                },
            )
            return
        except Exception as e:
            logger.exception("任务执行失败")
            await _send(ws, state, {"type": "error", "message": f"{type(e).__name__}: {e}"})
            return
        finally:
            state["cancel"] = None

        elapsed = round(time.time() - start, 1)
        status = result.get("status") or {}
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
                # token **明细**（计费原料）：输入 / 输出 / 缓存命中 / 调用次数 / 未计量次数 …
                # ⚠️ 只计量、不算钱（没有价格表）；结果卡片用它显示拆分，缺口径时显示「未计量」
                "tokenDetail": result.get("token_detail") or {},
                "stepCount": result.get("step_count", 0),
                "retryCount": result.get("retry_count", 0),
                "mode": result.get("mode", exec_mode),
                "route": result.get("route", ""),
                "elapsedSec": elapsed,
                # ── 阶段 8 · P0：停止 + 状态条口径 ──
                # ⚠️ `elapsedSec` 是**墙上时钟**（含人工确认等待），`status.elapsedSec` 是
                #    **扣除确认等待**的净任务时长 —— 两个都发，界面才能如实解释"为什么等了很久"。
                "cancelled": bool(result.get("cancelled")),
                "cancelReason": result.get("cancel_reason", ""),
                "cancelStage": result.get("cancel_stage", ""),
                # 阶段 8 · P1.5（§十五B）：入口发现"模板没替换" ⇒ 只回问、没进图；
                # 界面据此显示「需要你确认」而不是「本轮未验收」
                "needsClarification": bool(result.get("needs_clarification")),
                "placeholders": result.get("placeholders", []),
                "pausedSec": status.get("pausedSec", 0),
                "netElapsedSec": status.get("elapsedSec"),
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
        # 阶段 8 · P0：本次任务的停止开关（`_run_chat` 建、主循环点「停止」时置位）
        "cancel": None,
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
            # 阶段 7 · T7.6：界面要据此显示"还没有可用的模型 —— 去添加你的 API Key"
            "modelReady": runtime.executor_agent is not None,
            "modelError": runtime.model_error,
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

            if mtype == "stop":
                # 阶段 8 · P0：界面上的「停止」。
                # 它是**协作式**的：这里只置位，任务在下一个检查点（节点入口 / ReAct 每一步 /
                # 每次工具调用前）自己停下 —— 正在飞的那一次模型调用不会被打断。
                token = state.get("cancel")
                if token is None:
                    logger.info("收到 stop，但当前没有正在跑的任务（忽略）")
                    continue
                if token.cancel("user"):
                    logger.info("收到 stop：已请求停止 thread_id=%s", state["thread_id"])
                # ⚠️ 若此刻正卡在权限弹框上（任务在等答案），必须**立刻**把弹框收掉，
                #    否则要等确认超时（默认 120 秒）才有下一个检查点 —— 那"停止"就形同虚设。
                #    这里按"拒绝"回答；`permissions.enforce` 拿到答案后**先判停止、再判允许/拒绝**
                #    ⇒ 不会留下一条误导性的"用户拒绝了该工具"记录。
                for pending in list(state["pending"].values()):
                    if not pending.done():
                        pending.set_result({"allow": False, "always": False})
                await _send(ws, state, {"type": "stopping", "threadId": state["thread_id"]})
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

            # 阶段 7 · T7.6：**没有可用模型**这件事由**前端**处理（`session` 消息里带
            # `modelReady` / `modelError`：界面显示引导 + 禁用发送）。
            # ⚠️ **不要在这里拦 `chat`** —— 实测踩过：WS 层多一条"提前回错"的分支，
            #    会让"按协议等某条消息"的测试（`tests/test_web_permission.py`）直接卡死，
            #    而且那条分支对"Agent 能否运行"的判断是多余的（图自己会报错）。
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
