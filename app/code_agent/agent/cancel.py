"""阶段 8 · P0：任务级**停止**（协作式取消）与**墙钟**。

**要解决什么**（探索测试第 2 题的现场：30 分钟 / 56 步 / 20.8 万 token，全程没法让它停）：
- 用户点「停止」时任务必须真的停，而且停在**下一步边界**；
- 单个任务要有**墙钟上限**（默认 15 分钟），到点自动走**同一条**停止路径；
- ⚠️ **人工确认（权限弹框）期间不计时** —— 人看弹框的时间不是任务时间。
  （现场样本：02:54 → 03:18 有 24 分钟一次工具调用都没有，正是弹框/等待期。）

**协作式，不是强制**（刻意）：`asyncio.Task.cancel()` 只在 `await` 点生效，掐不断同步阻塞调用
（`subprocess`、文件 IO、`input()`），而且半路强杀会让"哪些副作用已经发生"变成未知。
所以这里只提供**检查点**：到点抛 `TaskCancelled`；**已经发生的副作用一律保留**
（不补偿、不回滚 —— 回滚自己也可能失败，还会掩盖现场）。

三个检查点（谁在哪调）：

| 检查点 | 位置 |
|---|---|
| 节点入口 | `multi_agent` 的 planner / executor / verifier 节点开头 |
| ReAct 每一步 | `executor_node` 的 `astream` 循环开头 |
| **每次工具调用前** | `utils/tool_wrap.py::_process` 第一句（工具调用的**唯一收口**） |
| 人工确认返回后 | `security/permissions.py::enforce` / `enforce_sync`（弹框被"停止"收掉时立刻解栈） |

**为什么 `TaskCancelled` 继承 `BaseException` 而不是 `Exception`**：
工具调用发生在 LangGraph 的 `ToolNode` 里，它 `except Exception` 会把异常**变成一条 ToolMessage**
交给模型 ⇒ "停止"会退化成"模型看到一条奇怪的错误、再决定下一步"（既多烧一次调用，也把停止变成建议）。
继承 `BaseException` 才能穿透 ToolNode、直接解栈到 executor 的检查点 —— 与 `asyncio.CancelledError`、
`GraphBubbleUp` 同理。
⚠️ 代价：`except Exception` **抓不到它** ⇒ 所有可能穿过它的入口都要显式接住
（本模块只负责抛；接线在 `multi_agent` 的节点里，Web 侧另有兜底）。

**依赖方向**：本模块是**叶子**（只用标准库），所以 `security/` 可以 import 它而不成环。
"""

from __future__ import annotations

import contextlib
import contextvars
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

from app.code_agent.agent.usage import merge_token_detail

#: 停止原因码（给用户看的文案在展示端，这里只给结构化字段）
REASON_USER = "user"
REASON_WALL_CLOCK = "wall_clock"


class TaskCancelled(BaseException):
    """任务被停止（用户点「停止」/ 墙钟到点）。

    ⚠️ **必须继承 `BaseException`**（理由见模块 docstring）：继承 `Exception` 会被
    LangGraph 的 `ToolNode` 吞成一条 ToolMessage，停止就变成了"建议"。
    `args[0]` / `reason` 是原因码。
    """

    def __init__(self, reason: str = REASON_USER) -> None:
        super().__init__(reason or REASON_USER)
        self.reason = reason or REASON_USER


@dataclass
class CancelToken:
    """一次任务的停止开关 + 墙钟 + 状态条数据。

    ⚠️ **一次任务一个**（CLI 每轮新建、Web 每条消息新建）：`_started_at` 就是任务开始时刻。
    复用同一个 token 会让"墙钟"变成"进程跑了多久" —— 进程开满 15 分钟后，
    **每个**新任务都会被立刻掐掉。
    """

    wall_clock: float | None = None
    """单任务最长**执行**时间（秒）。None / <= 0 = 不限制。人工确认期间不计入。"""

    steps: int = 0
    """已执行的 ReAct 步数（状态条用；由 executor 节点更新）。"""

    tokens: int = 0
    """本任务累计 token（状态条用；由各节点更新）。

    ⚠️ **计费口径**：每次 LLM 调用累加（同一段历史会被反复计费），**不是**上下文长度。
    """

    token_detail: dict = field(default_factory=dict)
    """本任务累计的 **token 明细**（计费原料）：`{input, output, cache_read, calls, unmetered_calls, first_ts, last_ts, by_model}`。

    为什么要留：① 状态条能显示"输入/输出/缓存命中"三项；② 将来要按各家单价折算成本时，
    这三项 + 时间戳就是**必需原料**（只留 total 就永远算不回来）。**这里只计量、不算钱**（价格表是另一件事）。
    """

    unmetered_calls: int = 0
    """provider **没返回 usage** 的调用次数。>0 表示累计值**偏低**（预算闸门会偏松），界面要显式提示。"""

    usage_limit: int = 0
    """token 上限（状态条里 "已用 / 上限" 的分母；0 = 不显示分母）。"""

    stage: str = ""
    """当前节点名（planner / executor / verifier；状态条用）。"""

    reason: str = ""
    """停止原因码；空串 = 还没停。"""

    _event: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _started_at: float = field(default_factory=time.monotonic, repr=False)
    _paused_total: float = field(default=0.0, repr=False)
    _pause_started: float | None = field(default=None, repr=False)
    _pause_depth: int = field(default=0, repr=False)

    # ── 停止 ──────────────────────────────────────────────────────

    def cancel(self, reason: str = REASON_USER) -> bool:
        """请求停止；返回"这次调用是否真的改变了状态"（重复点停止返回 False）。

        **原因码只记第一次**：用户先点了停止，之后墙钟到点不该把原因改写成 `wall_clock`
        （排查时会看不懂是谁停的）。
        """
        with self._lock:
            first = not self._event.is_set()
            if first:
                self.reason = reason or REASON_USER
            self._event.set()
            return first

    def _expired(self) -> bool:
        """纯判据：墙钟是否已到点（**不写状态**，供 `snapshot()` 用）。"""
        return bool(self.wall_clock) and self.elapsed() >= float(self.wall_clock)

    @property
    def cancelled(self) -> bool:
        """是否该停（含墙钟到点）。

        ⚠️ 这是**有副作用的读**：墙钟到点时会写回"已停 + 原因码"，
        这样 `reason` 与"谁先谁后"在后续任何一次读取里都一致。
        """
        if self._event.is_set():
            return True
        if self._expired():
            self.cancel(REASON_WALL_CLOCK)
            return True
        return False

    # ── 计时（不含人工确认的等待）────────────────────────────────

    def elapsed(self) -> float:
        """任务已执行时长（秒），**扣除**人工确认的等待时间。"""
        with self._lock:
            now = time.monotonic()
            paused = self._paused_total
            if self._pause_started is not None:
                paused += now - self._pause_started
            return max(0.0, now - self._started_at - paused)

    def paused_seconds(self) -> float:
        """累计**被暂停**（人工确认）的时长，秒。"""
        with self._lock:
            total = self._paused_total
            if self._pause_started is not None:
                total += time.monotonic() - self._pause_started
            return max(0.0, total)

    @contextlib.contextmanager
    def paused(self) -> Iterator[None]:
        """暂停计时（人工确认期间用）。**可重入**：嵌套时只在最外层记账。"""
        with self._lock:
            self._pause_depth += 1
            if self._pause_depth == 1:
                self._pause_started = time.monotonic()
        try:
            yield
        finally:
            with self._lock:
                self._pause_depth -= 1
                if self._pause_depth == 0 and self._pause_started is not None:
                    self._paused_total += time.monotonic() - self._pause_started
                    self._pause_started = None

    # ── 状态条数据 ────────────────────────────────────────────────

    def add_usage(self, usage: dict) -> None:
        """把一个 LLM 调用的用量并进状态条明细（**只计量、不算钱**）。

        `usage` 来自 `multi_agent._msg_usage()`（内部即 `usage.normalize_usage`）；
        `tokens`（总数）仍由各节点按原口径赋值，这里只维护"计费原料"明细与未计量计数。
        """
        with self._lock:
            self.token_detail = merge_token_detail(self.token_detail, usage)
            self.unmetered_calls = int(self.token_detail.get("unmetered_calls") or 0)

    def snapshot(self) -> dict:
        """任务状态条要显示的东西（**结构化字段，文案在展示端**）。"""
        detail = dict(self.token_detail or {})
        return {
            "elapsedSec": round(self.elapsed(), 1),
            "pausedSec": round(self.paused_seconds(), 1),
            "steps": self.steps,
            "tokens": self.tokens,
            "usageLimit": self.usage_limit,
            # 计费口径的三项明细（取不到就是 None ⇒ 展示端隐藏对应字段，别显示 0）
            "inputTokens": detail.get("input"),
            "outputTokens": detail.get("output"),
            "cacheReadTokens": detail.get("cache_read"),
            # >0 = 有调用没拿到 usage ⇒ 累计值偏低，界面要提示「未计量」
            "unmeteredCalls": self.unmetered_calls,
            "wallClockSec": float(self.wall_clock or 0),
            "stage": self.stage,
            "cancelled": self._event.is_set() or self._expired(),
            "cancelReason": self.reason,
        }


_token_var: contextvars.ContextVar[CancelToken | None] = contextvars.ContextVar(
    "agent_cancel_token", default=None
)


def current_token() -> CancelToken | None:
    """取当前任务的停止开关；**没有就返回 None**。

    ⚠️ 与 `permissions.current_session()` 不同：那里"没有会话"会**建一个**默认会话；
    这里**绝不新建** —— "没有 token"的语义就是"这个入口不参与停止"（evals / 单测零改动），
    悄悄造一个带墙钟的 token 会让那些入口凭空多出一条终止路径。
    """
    return _token_var.get()


@contextlib.contextmanager
def bind_cancel(token: CancelToken | None) -> Iterator[CancelToken | None]:
    """绑定一次任务的停止开关（CLI 每轮 / Web 每条消息各绑一次）。

    用 `ContextVar` 而不是全局变量：Web 端多个会话**并发**，各自的停止开关必须互不干扰
    （与事件接收器 `events.bind_sink`、权限会话 `permissions.bind_session` 同一个理由）。
    """
    reset = _token_var.set(token)
    try:
        yield token
    finally:
        _token_var.reset(reset)


def raise_if_cancelled() -> None:
    """**检查点**：已停（或墙钟到点）就抛 `TaskCancelled`；没有 token 时是空操作。"""
    token = current_token()
    if token is not None and token.cancelled:
        raise TaskCancelled(token.reason)


@contextlib.contextmanager
def pause_clock() -> Iterator[None]:
    """人工确认期间暂停墙钟（没有 token 时是空操作，调用方不必判空）。"""
    token = current_token()
    if token is None:
        yield
        return
    with token.paused():
        yield


__all__ = [
    "REASON_USER",
    "REASON_WALL_CLOCK",
    "CancelToken",
    "TaskCancelled",
    "bind_cancel",
    "current_token",
    "pause_clock",
    "raise_if_cancelled",
]
