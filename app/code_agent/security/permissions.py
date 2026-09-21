"""阶段 5 · T5.1 三档权限模式 + T5.2 权限判定（**工具级白名单 = 主防线**）。

**三层防线，判据不同，不要合并**：

| 层 | 在哪 | 管什么 | 随权限模式变吗 |
|---|---|---|---|
| ① **工具级档位表**（本模块） | `security/permissions.py` | 这个工具在当前模式下**该不该**执行 | ✅ 就是它 |
| ② 内容级危险命令黑名单 | `mcp_servers/vm.py` / `powershell_tools.py` | 同一个工具，**这次参数**危不危险 | ❌ 不变（选「放开」也照样拦） |
| ③ MySQL 语句白名单 + 只读账号 | `mysql_tools.py` / `scripts/mysql-init/*.sql` | 只读工具被拿来写数据 | ❌ 不变 |

⚠️ **本模块的档位表与 `utils/tool_cache.py` 的 `CACHEABLE_TOOL_NAMES` 不是一回事**：
缓存问的是"**结果会不会变**"，权限问的是"**有没有副作用**" ——
判据不同，所以是两张表，**不要合并**（`mysql_execute_query` 就是典型：只读、但不该缓存）。

**三条不可违背的实现约束**（B1+，都有测试守着，别"优化"掉）：

1. **权限判定在缓存查询之前**（`utils/tool_wrap.py` 的 `_process` 第一句）——
   否则"曾经允许过"的缓存值会让**已被拒绝**的调用照样返回结果（比"拒绝结果被缓存"更隐蔽）；
2. **被拒绝 / 被否决的调用绝不写缓存**（缓存里只能是真实执行结果）；
3. **拒绝用独立异常类型 `PermissionDenied`** —— 它在 `_process` 的
   `try/except BaseException` **之外**抛出，所以不会被"写失败 → 清缓存"分支误当成执行失败。

档位表来自 `program-fix第八版/阶段5_权限档位候选表.md`（32 个工具，**已审核**）；
`tests/test_permissions.py` 守着"32 个工具一个不多一个不少"，新增工具漏归类会让测试红。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from app.code_agent.config import CONFIRM_TIMEOUT, PERMISSION_MODE, PERMISSIONS_LOG

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# 三档权限模式（命名与用户确认：执行模式是另一条轴，两个名字不能都叫"模式"）
# ═══════════════════════════════════════════════════════════════════

MODE_READONLY = "readonly"
MODE_CONFIRM = "confirm"
MODE_OPEN = "open"
MODES: tuple[str, ...] = (MODE_READONLY, MODE_CONFIRM, MODE_OPEN)

MODE_LABELS: dict[str, str] = {
    MODE_READONLY: "只读",
    MODE_CONFIRM: "需确认",
    MODE_OPEN: "放开",
}

DEFAULT_MODE = MODE_CONFIRM  # D4：CLI 默认「需确认」；Web 新会话也回落它

#: **无人值守入口**（评估脚本 / 无头调用）固定用的档位。
#:
#: 为什么不让它们走默认的「需确认」：B2 规定"超时 / 无人应答 → 自动拒绝"，
#: 而这些入口**没有人可以问** → 每个写工具都会被自动拒绝，任务全线失败
#: （fail-closed 生效得太彻底，等于入口不可用）。
#: 为什么不选「只读」：这些入口要跑的正是"会写文件 / 会写库"的端到端任务，只读档会把它们全拒掉。
#: ⚠️ 这是**过渡值**：评估题集与评分器已随阶段 5 删除、阶段 6 重建，
#:    重建时再连同口径一起定"评估侧到底用哪一档"。
HEADLESS_PERMISSION_MODE = MODE_OPEN

# 判定结果（`decide()` 的返回值）
ALLOW = "allow"
ASK = "ask"
DENY = "deny"


def mode_label(mode: str) -> str:
    """给用户看的档位名（未知档位原样返回，方便排查）。"""
    return MODE_LABELS.get(mode, mode or "（未设置）")


def normalize_mode(value: Any, default: str = DEFAULT_MODE) -> str:
    """把外部输入（前端下拉框 / 配置文件 / 环境变量）规整成合法档位。

    ⚠️ **只读**与**需确认**之外的任何值（含被手改成 `open` 的配置文件）都要能安全落地：
    调用方对「放开」另做处理（D4：永不持久化），这里只保证不把脏值传下去。
    """
    text = str(value or "").strip().lower()
    if text in MODES:
        return text
    # 容忍中文与常见写法（前端/文档里出现过）
    alias = {"只读": MODE_READONLY, "需确认": MODE_CONFIRM, "放开": MODE_OPEN}
    return alias.get(str(value or "").strip(), default)


# ═══════════════════════════════════════════════════════════════════
# T5.1 档位表（32 个工具）
# ═══════════════════════════════════════════════════════════════════
#
# 判据（来自候选表 §二）：
#   只读   = 不改变任何持久状态（文件 / 数据库 / 知识库 / 进程），含"网络只读"（搜索）
#   写/执行 = 会改上面任何一项，或执行任意命令
#   高危   = 在"写/执行"里再标一类：**影响面超出 workspace 沙盒，或不可逆**
#
# ⚠️ 高危**不是第四档**，只是附加信息，只影响两件事：
#   ① 「需确认」档下确认框的提示强度；② 「放开」档下**仍然必须留审计记录**。
#   它**不影响"弹不弹"** —— 粒度是工具级，只要是 B 类工具，参数再无害也会弹。

# A 类 · 只读（14）—— 三个模式都放行
READONLY_TOOLS: frozenset[str] = frozenset(
    {
        # code_tools
        "read_file_range",
        "analyze_ast",
        "generate_diff",
        "list_project_structure",
        # rag
        "query_rag",
        # mysql（有应用层语句白名单 + 只读账号双保险）
        "mysql_list_databases",
        "mysql_list_tables",
        "mysql_describe_tables",
        "mysql_execute_query",
        # FileManagementToolkit
        "read_file",
        "list_directory",
        "file_search",
        # browser（只读；不在 Verifier 只读白名单里，因为验收不需要它）
        "search_in_searxng",
        # vm（效果纯只读：实现就是 `ls -al`）
        "list_files_in_vm",
    }
)

# B 类 · 写 / 执行（18）—— 只读档拒绝、需确认档弹框、放开档执行
WRITE_TOOLS: frozenset[str] = frozenset(
    {
        # FileManagementToolkit
        "write_file",
        "copy_file",
        "move_file",
        "file_delete",
        # rag（写知识库）
        "save_knowledge",
        "update_knowledge",
        "delete_knowledge",
        # mysql
        "mysql_insert_data",
        "mysql_update_data",
        "mysql_delete_data",
        "mysql_create_table",
        "mysql_create_database",
        "mysql_execute_command",
        # powershell
        "execute_powershell_command",
        "close_powershell",
        # vm
        "make_dir_in_vm",
        "write_file_to_vm",
        "upload_directory_to_vm",
    }
)

# 🔴 高危（6）—— 影响面超出 workspace 或不可逆
HIGH_RISK_TOOLS: frozenset[str] = frozenset(
    {
        "execute_powershell_command",
        "mysql_execute_command",
        "write_file_to_vm",
        "upload_directory_to_vm",
        "file_delete",
        "mysql_delete_data",
    }
)

# 确认框里给高危操作加的那句"影响面说明"
HIGH_RISK_NOTES: dict[str, str] = {
    "execute_powershell_command": "将执行任意 PowerShell 命令，影响面是整台 Windows 机器。",
    "mysql_execute_command": "将执行任意 SQL（含 DROP / TRUNCATE），影响面是整个 MySQL 实例。",
    "write_file_to_vm": "将写入 WSL 宿主路径（如 nginx 发布目录），已超出 workspace 沙盒。",
    "upload_directory_to_vm": "将整个目录上传到 WSL 宿主路径，已超出 workspace 沙盒。",
    "file_delete": "删除不可逆，删掉就找不回来了。",
    "mysql_delete_data": "删除不可逆，删掉就找不回来了。",
}

ALL_TOOLS: frozenset[str] = READONLY_TOOLS | WRITE_TOOLS


def classify(tool_name: str) -> str:
    """工具属于哪一类：`"readonly"` / `"write"` / `"unknown"`（未归类）。"""
    if tool_name in READONLY_TOOLS:
        return "readonly"
    if tool_name in WRITE_TOOLS:
        return "write"
    return "unknown"


def is_high_risk(tool_name: str) -> bool:
    return tool_name in HIGH_RISK_TOOLS


def decide(tool_name: str, mode: str) -> str:
    """三档模式的判定（**纯函数，不做 I/O、不弹框**）。返回 `allow` / `ask` / `deny`。

    未归类的工具在**任何模式**下都拒绝（白名单语义）—— 给将来新增的工具兜底：
    忘了归类就默认拒绝，而不是默认放行。
    """
    if mode not in MODES:
        # 脏档位（配置被手改坏）：按最严的「只读」处理，绝不因为读到脏值而放行
        logger.warning("未知权限模式 %r，按「只读」处理", mode)
        mode = MODE_READONLY

    tier = classify(tool_name)
    if tier == "unknown":
        return DENY
    if tier == "readonly":
        return ALLOW
    if mode == MODE_READONLY:
        return DENY
    if mode == MODE_CONFIRM:
        return ASK
    return ALLOW  # MODE_OPEN


# ═══════════════════════════════════════════════════════════════════
# 拒绝：独立异常类型（约束 3）
# ═══════════════════════════════════════════════════════════════════


class PermissionDenied(Exception):
    """权限层拒绝。

    ⚠️ **独立异常类型**，不复用工具自身的异常 —— 否则会被 `tool_wrap._process`
    的"写失败 → 清缓存"分支误当成执行失败。

    `args[0]` 就是**给模型看的完整说明**（LangChain 的 `ToolNode` 会把它原样放进
    `ToolMessage.content`），所以文案必须含"原因 + 出路"，否则模型会反复重试白烧 token。
    """

    def __init__(self, message: str, *, tool_name: str, mode: str, decision: str) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.mode = mode
        self.decision = decision  # 拒绝原因码，供测试与审计用


def _remedy(mode: str) -> str:
    """给模型的"出路"提示（不同档位出路不同）。"""
    if mode == MODE_READONLY:
        return "如需写入，请把权限模式切到「需确认」（CLI：--permission confirm；Web：左侧「权限模式」下拉框）。"
    return "请改用只读工具获取信息，或向用户说明需要更高权限。"


# ═══════════════════════════════════════════════════════════════════
# 审计留痕（T5.4）：高危操作 + 所有确认决定
# ═══════════════════════════════════════════════════════════════════

_AUDIT_LOCK = threading.Lock()
_ARGS_LIMIT = 300


def _brief_args(args: Any) -> str:
    """把参数压成一行供审计/弹框显示（截断，避免把整个文件内容写进日志）。"""
    try:
        text = json.dumps(args, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 —— 参数可能含无法序列化的对象
        text = str(args)
    return text if len(text) <= _ARGS_LIMIT else text[:_ARGS_LIMIT] + f"…(共 {len(text)} 字符)"


def audit(event: dict) -> None:
    """追加一行 JSON 到 `runtime/permissions.log`。

    **绝不抛异常**：审计是旁路，写不进去也不能让工具调用失败（顶多丢一条记录）。
    """
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **event}
    try:
        PERMISSIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _AUDIT_LOCK, PERMISSIONS_LOG.open("a", encoding="utf-8") as fp:
            fp.write(line + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("审计日志写入失败（忽略）：%s: %s", type(exc).__name__, exc)


# ═══════════════════════════════════════════════════════════════════
# 会话（模式 + 确认通道 + 本会话总是允许）
# ═══════════════════════════════════════════════════════════════════

#: 异步确认通道：拿到请求 → 返回"允许吗"。超时由 `enforce` 统一处理（B2：超时自动拒绝）。
Approver = Callable[["PermissionRequest"], Awaitable[bool]]
#: 同步确认通道：只给"只有同步 `_run`"的工具路径用（CLI 的 `input()` 就走它）。
SyncApprover = Callable[["PermissionRequest"], bool]


@dataclass(frozen=True)
class PermissionRequest:
    """一次待确认的调用（确认框要展示的内容就是它）。"""

    tool_name: str
    args: Any
    mode: str
    high_risk: bool
    note: str  # 高危时的"影响面说明"，非高危为空串
    scope: str

    def args_brief(self) -> str:
        return _brief_args(self.args)

    def to_payload(self, request_id: str, timeout: float | None) -> dict:
        """给前端 `permission_request` 消息用的载荷（键名 camelCase，与其它消息一致）。"""
        return {
            "type": "permission_request",
            "requestId": request_id,
            "tool": self.tool_name,
            "args": self.args_brief(),
            "mode": self.mode,
            "modeLabel": mode_label(self.mode),
            "highRisk": self.high_risk,
            "note": self.note,
            "timeoutSec": timeout,
        }


@dataclass
class Session:
    """一次会话的权限上下文。

    `always_allow`（B7）：**「本会话内对该工具总是允许」** 的集合，
    元素是 `(模式, 工具名)` 二元组 —— 这样**切模式即失效**，不用额外清理；
    集合对象由会话持有、跨多次任务复用（每次任务新建一个集合的话，勾选框就白勾了）。
    """

    mode: str = DEFAULT_MODE
    scope: str = "default"
    approver: Approver | None = None
    sync_approver: SyncApprover | None = None
    timeout: float | None = CONFIRM_TIMEOUT
    always_allow: set[tuple[str, str]] = field(default_factory=set)

    @classmethod
    def from_config(cls, scope: str = "default") -> Session:
        return cls(mode=normalize_mode(PERMISSION_MODE), scope=scope, timeout=CONFIRM_TIMEOUT)

    def with_mode(self, mode: str) -> Session:
        """同一会话内换档位：确认通道与「总是允许」集合都保留（后者按模式做键，自动失效）。"""
        return Session(
            mode=normalize_mode(mode),
            scope=self.scope,
            approver=self.approver,
            sync_approver=self.sync_approver,
            timeout=self.timeout,
            always_allow=self.always_allow,
        )


_session_var: ContextVar[Session | None] = contextvars.ContextVar(
    "permission_session", default=None
)


def current_session() -> Session:
    """取当前会话的权限上下文（没有就用配置里的默认值建一个并缓存住）。

    用 `ContextVar` 而不是全局变量：Web 端多个 WebSocket 会话**并发**跑任务，
    权限模式与确认通道必须是**每条连接各自**的（否则 A 会话切档会改掉 B 会话的行为）。
    """
    session = _session_var.get()
    if session is None:
        session = Session.from_config()
        _session_var.set(session)
    return session


@contextlib.contextmanager
def bind_session(session: Session) -> Iterator[Session]:
    """绑定会话上下文（CLI / Web / 非交互入口各绑一次）。"""
    token = _session_var.set(session)
    try:
        yield session
    finally:
        _session_var.reset(token)


@contextlib.contextmanager
def use_mode(mode: str) -> Iterator[Session]:
    """临时换档位（测试与"运行中切档"用）。"""
    with bind_session(current_session().with_mode(mode)) as session:
        yield session


def grant_always(tool_name: str, mode: str | None = None) -> None:
    """记下"本会话内对该工具总是允许"（只在当前模式内有效，切模式即失效）。"""
    session = current_session()
    session.always_allow.add((normalize_mode(mode or session.mode), tool_name))


def revoke_all_always() -> None:
    """清空「本会话内总是允许」（换会话时调用）。"""
    current_session().always_allow.clear()


# ═══════════════════════════════════════════════════════════════════
# T5.2 判定入口（被 `utils/tool_wrap.py` 的 `_process` 调用）
# ═══════════════════════════════════════════════════════════════════


def build_request(tool_name: str, args: Any, session: Session) -> PermissionRequest:
    return PermissionRequest(
        tool_name=tool_name,
        args=args,
        mode=session.mode,
        high_risk=is_high_risk(tool_name),
        note=HIGH_RISK_NOTES.get(tool_name, ""),
        scope=session.scope,
    )


def _log(tool_name: str, session: Session, decision: str, args: Any, note: str = "") -> None:
    audit(
        {
            "scope": session.scope,
            "tool": tool_name,
            "mode": session.mode,
            "tier": classify(tool_name),
            "high_risk": is_high_risk(tool_name),
            "decision": decision,
            "args": _brief_args(args),
            **({"note": note} if note else {}),
        }
    )


def _deny(tool_name: str, session: Session, decision: str, message: str, args: Any) -> None:
    _log(tool_name, session, decision, args)
    raise PermissionDenied(message, tool_name=tool_name, mode=session.mode, decision=decision)


def _deny_message(tool_name: str, session: Session, decision: str) -> str:
    label = mode_label(session.mode)
    if decision == "deny_unknown_tool":
        return (
            f"[权限拒绝] 工具 {tool_name} **不在权限档位表里**（未归类），按白名单语义拒绝执行"
            "（未执行、未缓存）。这通常意味着新增工具后忘了在 "
            "app/code_agent/security/permissions.py 里归类 —— 请补上档位表再重试，"
            "不要重复调用同一个工具。"
        )
    if decision == "deny_mode":
        return (
            f"[权限拒绝] 当前权限模式为「{label}」，工具 {tool_name} 属于写/执行类，已被拒绝"
            f"（未执行、未缓存）。{_remedy(session.mode)}"
        )
    if decision == "deny_no_channel":
        return (
            f"[权限拒绝] 当前权限模式为「{label}」，但这次调用没有可用的人工确认通道"
            f"（无人值守入口），按安全默认拒绝执行 {tool_name}（未执行、未缓存）。"
            "无人值守请显式指定权限模式后重跑（只读=只允许读，放开=不再逐次确认）。"
        )
    if decision == "deny_timeout":
        return (
            f"[权限拒绝] 工具 {tool_name} 的确认请求在 {session.timeout:g} 秒内无人应答，"
            "已按安全默认**自动拒绝**（未执行、未缓存）。请先与用户确认，不要重复调用。"
        )
    if decision == "deny_sync_no_channel":
        return (
            f"[权限拒绝] 工具 {tool_name} 走的是**同步调用路径**，同步路径没有人工确认通道，"
            f"在「{label}」档下按安全默认拒绝（未执行、未缓存）。"
            "请改用异步调用（`await tool.ainvoke(...)`）。"
        )
    # denied_by_user
    return (
        f"[权限拒绝] 用户在确认框中**拒绝**了工具 {tool_name} 的执行（未执行、未缓存）。"
        "不要重试同一个操作；请换一种不需要该工具的方式，或向用户说明为什么需要它。"
    )


async def enforce(tool_name: str, args: Any = None) -> None:
    """权限判定的**唯一入口**：放行则正常返回，否则抛 `PermissionDenied`。

    ⚠️ 调用点必须在 `tool_wrap._process` 的**第一句**（缓存查询之前）——
    见模块 docstring 的三条约束。
    """
    session = current_session()
    session.mode = normalize_mode(session.mode)  # 脏值（配置被手改）就地纠正
    decision = decide(tool_name, session.mode)

    if decision == ALLOW:
        # 放开档下的高危操作**仍然强制留痕**（候选表 §一「两处差异」之一）
        if is_high_risk(tool_name) and session.mode == MODE_OPEN:
            _log(
                tool_name,
                session,
                "allowed_open_high_risk",
                args,
                HIGH_RISK_NOTES.get(tool_name, ""),
            )
        return

    if decision == DENY:
        reason = "deny_unknown_tool" if classify(tool_name) == "unknown" else "deny_mode"
        _deny(tool_name, session, reason, _deny_message(tool_name, session, reason), args)

    # ── 需确认 ──
    if (session.mode, tool_name) in session.always_allow:
        _log(tool_name, session, "allowed_always_in_session", args)
        return
    if session.approver is None:
        _deny(
            tool_name,
            session,
            "deny_no_channel",
            _deny_message(tool_name, session, "deny_no_channel"),
            args,
        )

    request = build_request(tool_name, args, session)
    try:
        # B2：超时 / 无人应答 → 自动拒绝（安全侧）
        allowed = await asyncio.wait_for(session.approver(request), timeout=session.timeout)
    except TimeoutError:
        _deny(
            tool_name,
            session,
            "deny_timeout",
            _deny_message(tool_name, session, "deny_timeout"),
            args,
        )
    except Exception as exc:  # noqa: BLE001 —— 确认通道自身故障也必须**拒绝**，不能放行
        logger.warning("确认通道异常（按拒绝处理）：%s: %s", type(exc).__name__, exc)
        _deny(
            tool_name,
            session,
            "deny_no_channel",
            _deny_message(tool_name, session, "deny_no_channel"),
            args,
        )

    if not allowed:
        _deny(
            tool_name,
            session,
            "denied_by_user",
            _deny_message(tool_name, session, "denied_by_user"),
            args,
        )

    _log(tool_name, session, "allowed_by_user", args)


def enforce_sync(tool_name: str, args: Any = None) -> None:
    """同步调用路径的权限判定（只有同步 `_run` 的工具走这里）。

    与 `enforce` 的唯一区别：确认环节**没法 await**，只能用 `sync_approver`；
    没有同步通道时**拒绝**（不能因为"路径不同"就绕过人工确认）。
    """
    session = current_session()
    session.mode = normalize_mode(session.mode)
    decision = decide(tool_name, session.mode)

    if decision == ALLOW:
        if is_high_risk(tool_name) and session.mode == MODE_OPEN:
            _log(
                tool_name,
                session,
                "allowed_open_high_risk",
                args,
                HIGH_RISK_NOTES.get(tool_name, ""),
            )
        return

    if decision == DENY:
        reason = "deny_unknown_tool" if classify(tool_name) == "unknown" else "deny_mode"
        _deny(tool_name, session, reason, _deny_message(tool_name, session, reason), args)

    if (session.mode, tool_name) in session.always_allow:
        _log(tool_name, session, "allowed_always_in_session", args)
        return
    if session.sync_approver is None:
        _deny(
            tool_name,
            session,
            "deny_sync_no_channel",
            _deny_message(tool_name, session, "deny_sync_no_channel"),
            args,
        )

    request = build_request(tool_name, args, session)
    try:
        allowed = bool(session.sync_approver(request))
    except Exception as exc:  # noqa: BLE001
        logger.warning("同步确认通道异常（按拒绝处理）：%s: %s", type(exc).__name__, exc)
        allowed = False
    if not allowed:
        _deny(
            tool_name,
            session,
            "denied_by_user",
            _deny_message(tool_name, session, "denied_by_user"),
            args,
        )
    _log(tool_name, session, "allowed_by_user", args)


__all__ = [
    "ALLOW",
    "ALL_TOOLS",
    "ASK",
    "DEFAULT_MODE",
    "DENY",
    "HEADLESS_PERMISSION_MODE",
    "HIGH_RISK_NOTES",
    "HIGH_RISK_TOOLS",
    "MODE_CONFIRM",
    "MODE_LABELS",
    "MODE_OPEN",
    "MODE_READONLY",
    "MODES",
    "PermissionDenied",
    "PermissionRequest",
    "READONLY_TOOLS",
    "Session",
    "WRITE_TOOLS",
    "audit",
    "bind_session",
    "build_request",
    "classify",
    "current_session",
    "decide",
    "enforce",
    "enforce_sync",
    "grant_always",
    "is_high_risk",
    "mode_label",
    "normalize_mode",
    "revoke_all_always",
    "use_mode",
]
