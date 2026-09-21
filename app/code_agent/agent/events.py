"""阶段 5 · T5.6：**节点级进度事件**（用现有 WebSocket 推送，不引 SSE）。

**要解决什么**：改造前后端只在任务结束时一次性推 `result`，中间那段（Planner 规划、
Executor 一步步调工具、Verifier 验收）在界面上就是一坨"协作中..."的转圈 ——
"执行过程实时可见"这件事在演示时是能加分的，而且任务卡住时也能一眼看出卡在哪个节点。

**怎么做的（刻意做得很轻）**：
- 一个 `ContextVar` 装"事件接收器"（sink）；节点里 `await emit({...})`；
- **没绑定 sink 时 `emit()` 直接返回** —— 所以 evals、单测、任何不关心进度的入口
  一行都不用改，且没有性能负担；
- 用 ContextVar 而不是全局变量：Web 端**多个会话并发**，各自的进度必须发到各自的连接上
  （与权限层同一个理由）；
- **`emit()` 永不抛异常**：推进度是旁路，推不出去（连接断了）绝不能影响任务本身。

事件形状（前端与 CLI 共用，**只发结构化字段，文案由展示端决定**）：

```json
{"type": "node", "node": "planner",  "status": "start"}
{"type": "node", "node": "planner",  "status": "end",   "steps": 4}
{"type": "node", "node": "route",    "status": "end",   "route": "complex"}
{"type": "node", "node": "executor", "status": "start"}
{"type": "node", "node": "executor", "status": "step",  "step": 3, "tools": ["read_file_range"]}
{"type": "node", "node": "executor", "status": "end",   "steps": 6}
{"type": "node", "node": "verifier", "status": "start"}
{"type": "node", "node": "verifier", "status": "end",   "passed": true}
```
"""

from __future__ import annotations

import contextlib
import contextvars
import inspect
import logging
from collections.abc import Awaitable, Callable, Iterator

logger = logging.getLogger(__name__)

#: 事件接收器：可以返回协程，也可以是普通函数（同步写法更省事时用）。
EventSink = Callable[[dict], Awaitable[None] | None]

_sink_var: contextvars.ContextVar[EventSink | None] = contextvars.ContextVar(
    "agent_event_sink", default=None
)


async def emit(event: dict) -> None:
    """发一个进度事件。**没有 sink 就静默跳过；任何异常都吞掉**（进度是旁路）。"""
    sink = _sink_var.get()
    if sink is None:
        return
    try:
        result = sink(event)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001 —— 推不出去绝不能影响任务
        logger.debug("进度事件推送失败（忽略）：%s: %s", type(exc).__name__, exc)


@contextlib.contextmanager
def bind_sink(sink: EventSink | None) -> Iterator[EventSink | None]:
    """绑定事件接收器（Web / CLI 各绑一次）。"""
    token = _sink_var.set(sink)
    try:
        yield sink
    finally:
        _sink_var.reset(token)


__all__ = ["EventSink", "bind_sink", "emit"]
