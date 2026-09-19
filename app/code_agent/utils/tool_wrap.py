"""工具包装层：T4.1 结果外置 + T4.5 结果缓存（阶段 4）。

为什么要在**工具层**做，而不是在节点里做：
`create_react_agent` 内部自己执行工具、自己把结果回填成 ToolMessage，外部插不进手；
所以在工具被交给 agent **之前**统一包一层，是最集中的做法。

⚠️ **接口形状是实测确认的，两条路径都要处理**（照抄第六版示例会炸）：

```
langchain_mcp_adapters/tools.py
  StructuredTool(name=…, description=…, args_schema=…,
                 coroutine=call_tool,                    ← 只有协程，没有 func
                 response_format="content_and_artifact")  ← 返回 (content, artifact) 二元组
```

- **32 个 MCP 工具**：只有 `tool.coroutine`，**没有 `func`** → 对它们取 `tool._run` 会 raise；
  但 `coroutine` 是 `StructuredTool` **声明过的字段**，可以直接替换。
- **FileManagementToolkit 的 7 个工具**：**只有同步 `_run`**，而且它们的类型是 **pydantic 模型**
  → 往实例上塞 `coroutine` 字段会直接报
  `ValueError: "CopyFileTool" object has no field "coroutine"`（实测踩到）。
  所以这类工具**不改造原对象**，而是返回一个**同名 StructuredTool 代理**
  （名字 / 描述 / args_schema 全沿用，模型看到的接口不变），
  代理的协程用 `asyncio.to_thread` 跑原来的 `_run`。

⚠️ 因此 `wrap_tools` **必须接收返回值**（同步工具会被换成新对象，不是原地改）：
`tools = wrap_tools(tools, cache)`。

⚠️ **必须保留 `(content, artifact)` 二元组**：`response_format="content_and_artifact"` 的工具
若返回字符串，LangChain 会在格式化时直接报错。缓存只缓存 `artifact is None` 的纯文本结果，
且**缓存命中时也要补回 `None` artifact**（否则命中一次就炸）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.code_agent.agent.context import externalize_tool_result
from app.code_agent.config import TOOL_RESULTS_DIR
from app.code_agent.utils.tool_cache import CACHEABLE_TOOL_NAMES, ToolCache


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_as_text(v) for v in value)
    return str(value)


def split_tool_output(result: Any) -> tuple[str, Any, bool]:
    """把工具返回值拆成 `(文本, artifact, 是否为二元组格式)`。

    MCP 工具返回 `(content, artifact)`；普通工具返回裸字符串。
    """
    if isinstance(result, tuple) and len(result) == 2:
        return _as_text(result[0]), result[1], True
    return _as_text(result), None, False


def _args_key(args: tuple, kwargs: dict) -> dict:
    """把调用参数规整成一个可哈希的字典（用于拼缓存 key）。"""
    if kwargs:
        return dict(kwargs)
    if len(args) == 1 and isinstance(args[0], dict):
        return dict(args[0])
    if args:
        return {"__args__": [_as_text(a) for a in args]}
    return {}


def _reads_externalized_file(key_args: dict) -> bool:
    """这次调用是不是在读 `runtime/tool_results/` 里的外置文件？

    是的话就不能再外置 —— 否则「外置 → 提示去看文件 → 读文件 → 又外置」死循环。
    """
    marker = TOOL_RESULTS_DIR.name  # "tool_results"
    for value in key_args.values():
        if isinstance(value, str) and marker in value:
            return True
        if isinstance(value, list) and any(isinstance(v, str) and marker in v for v in value):
            return True
    return False


def wrap_tool(tool: Any, cache: ToolCache | None = None) -> Any:
    """包装一个工具（有 `coroutine` 字段的原位替换，没有的返回一个代理工具）。"""
    name = getattr(tool, "name", "unknown")
    cacheable = name in CACHEABLE_TOOL_NAMES and cache is not None
    # ⚠️ 缓存命中时也要返回**正确的返回形状**：MCP 工具声明了
    #    `response_format="content_and_artifact"`，返回裸字符串会被 LangChain 直接拒绝
    #    （"response_format is content_and_artifact but output is not a tuple"）。
    #    缓存里只存文本，所以命中时补一个 `None` artifact。
    state = {"pair": getattr(tool, "response_format", None) == "content_and_artifact"}

    def _make_coroutine(call):
        async def _coroutine(*args: Any, **kwargs: Any) -> Any:
            return await _process(args, kwargs, lambda: call(*args, **kwargs))

        return _coroutine

    async def _process(args: tuple, kwargs: dict, call) -> Any:
        key_args = _args_key(args, kwargs)

        if cacheable:
            hit = await cache.get(name, key_args)  # type: ignore[union-attr]
            if hit is not None:
                return (hit, None) if state["pair"] else hit

        try:
            raw = call()
            if asyncio.iscoroutine(raw):
                raw = await raw
        except BaseException:
            # 写操作失败也可能留下半成品 → 保守起见照样失效缓存
            if cache is not None and not cacheable:
                await cache.invalidate()
            raise

        text, artifact, is_pair = split_tool_output(raw)
        state["pair"] = state["pair"] or is_pair
        # 防死循环：如果这次调用读的就是**外置文件本身**，绝不能再外置一次 ——
        # 同一份内容 → 同一个 hash → 又指向同一个文件，模型永远看不到内容。
        if not _reads_externalized_file(key_args):
            text = externalize_tool_result(name, text)

        if cacheable:
            # artifact 不为 None（图片等非文本内容）时不缓存：缓存里只存得下文本
            if artifact is None:
                await cache.set(name, key_args, text)  # type: ignore[union-attr]
        elif cache is not None:
            # 写操作 → 清空本作用域缓存（否则"读→写→读"会拿到旧值）
            await cache.invalidate()

        return (text, artifact) if is_pair else text

    original_coroutine = getattr(tool, "coroutine", None)
    if original_coroutine is not None:
        # StructuredTool / Tool：`coroutine` 是**声明过的字段**，可以直接赋值；
        # 但它们没有可赋值的 `_run`（那是类方法，赋值会被 pydantic 拒绝）。
        tool.coroutine = _make_coroutine(original_coroutine)
        return tool

    # ── 只有同步 _run 的工具（FileManagementToolkit）──
    # ⚠️ 它们的类型是 **pydantic 模型**，往实例上塞新字段会直接报
    #    `ValueError: "CopyFileTool" object has no field "coroutine"`（实测踩到）。
    #    所以这里**不改造原对象**，而是返回一个同名的 StructuredTool 代理：
    #    名字 / 描述 / 参数 schema 全部沿用，模型看到的接口不变。
    return _proxy_sync_tool(tool, _make_coroutine)


def _proxy_sync_tool(tool: Any, make_coroutine) -> Any:
    """给「只有同步 `_run`」的工具造一个等价的 StructuredTool 代理。"""
    from langchain_core.tools import StructuredTool

    original_run = tool._run
    name = tool.name
    # 同步入口也保留（万一有同步调用方），行为与外置一致
    sync_coroutine = make_coroutine(lambda *a, **k: asyncio.to_thread(original_run, *a, **k))

    def _sync(*args: Any, **kwargs: Any) -> Any:
        text, artifact, is_pair = split_tool_output(original_run(*args, **kwargs))
        text = externalize_tool_result(name, text)
        return (text, artifact) if is_pair else text

    kwargs: dict[str, Any] = {
        "name": name,
        "description": getattr(tool, "description", "") or "",
        "args_schema": getattr(tool, "args_schema", None),
        "func": _sync,
        "coroutine": sync_coroutine,
        "response_format": getattr(tool, "response_format", "content"),
    }
    for extra in ("return_direct", "metadata", "tags"):
        value = getattr(tool, extra, None)
        if value:
            kwargs[extra] = value
    return StructuredTool(**kwargs)


def wrap_tools(tools: list, cache: ToolCache | None = None) -> list:
    """批量包装（原地修改并返回同一个列表）。"""
    return [wrap_tool(t, cache) for t in tools]


__all__ = ["split_tool_output", "wrap_tool", "wrap_tools"]
