"""token 用量的**归一化与聚合**（纯函数；只计量、不算钱）。

为什么单独一个模块：`agent/cancel.py`（状态条）与 `agent/multi_agent.py`（节点累加）都要用它，
放任何一边都会造成互相 import；纯函数也方便单测。

⚠️ **两个口径分清**（面试与文档都别混）：
  · `total` = **计费口径**：每次 LLM 调用累加（同一段历史会被反复计费）⇒ 数字大是正常的；
  · **上下文长度** = 单次请求的输入规模（窗口够不够看这个），与本模块无关。

本模块**不做**的事：不查价格、不算钱。价格表是另一件事（各家单价不同、还会调价），
这里只负责把"计费原料"（输入 / 输出 / 缓存命中 / 时间 / 模型名）留下来。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

#: 缓存命中 token 的字段名 —— 各家不同，按顺序取第一个存在的：
#: DeepSeek `prompt_cache_hit_tokens` · Anthropic `cache_read_input_tokens` · OpenAI `cached_tokens`
CACHE_READ_KEYS: tuple[str, ...] = (
    "prompt_cache_hit_tokens",
    "cache_read_input_tokens",
    "cached_tokens",
)


def _int_or_none(value: Any) -> int | None:
    """能转成 int 就转，否则 None（**不把"缺失"写成 0** —— 0 会被误当成"真的没有缓存命中"）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def usage_model(msg: Any) -> str:
    """取服务端**真实回报**的模型名（`response_metadata.model_name`）。取不到返回空串。"""
    meta = getattr(msg, "response_metadata", None) or {}
    if isinstance(meta, dict):
        return str(meta.get("model_name") or meta.get("model") or "")
    return ""


def normalize_usage(msg: Any, *, model: str | None = None) -> dict:
    """把一个 LLM 响应的 usage 归一化成明细字典。

    返回：`{total, input, output, cache_read, model, ts, unmetered}`

    · `total` 缺省 **0**（调用方要知道"这次没计量"，看 `unmetered`）；
    · `input` / `output` / `cache_read` 缺省 **None**（**不猜 0**：0 与"没这个字段"是两件事）；
    · `unmetered=True` ⇒ provider 没返回 usage ⇒ **累计值偏低**，预算闸门会偏松，界面要提示；
    · `ts` 是**本地累计时刻**（UTC），**不是**服务端计费时刻 —— 峰谷计价要用"逐调用时间戳"，
      当前只留首末时间，属已知局限（见 AGENTS「已知坑」）。
    """
    usage = getattr(msg, "usage_metadata", None) or {}
    if not isinstance(usage, dict):
        usage = {}

    total = _int_or_none(usage.get("total_tokens"))
    cache_read = None
    details = usage.get("input_token_details")
    if isinstance(details, dict):
        cache_read = _int_or_none(details.get("cache_read"))
    if cache_read is None:
        for key in CACHE_READ_KEYS:
            cache_read = _int_or_none(usage.get(key))
            if cache_read is not None:
                break

    return {
        "total": total or 0,
        "input": _int_or_none(usage.get("input_tokens") or usage.get("prompt_tokens")),
        "output": _int_or_none(usage.get("output_tokens") or usage.get("completion_tokens")),
        "cache_read": cache_read,
        "model": model if model is not None else usage_model(msg),
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "unmetered": total is None,
    }


def _add(a: int | None, b: int | None) -> int | None:
    """可空整数相加：**两边都没有 ⇒ None**（不能变成 0）。"""
    if a is None and b is None:
        return None
    return (a or 0) + (b or 0)


def empty_detail() -> dict:
    """空明细（新任务的起点）。`input/output/cache_read` 用 None 表示"还没有可用的数"。"""
    return {
        "input": None,
        "output": None,
        "cache_read": None,
        "calls": 0,
        "unmetered_calls": 0,
        "first_ts": "",
        "last_ts": "",
        "by_model": {},
    }


def merge_token_detail(detail: dict | None, usage: dict | None) -> dict:
    """把一次调用并进累计明细，**返回新字典（不改入参）**。

    `by_model` 按模型名分桶累计 `{calls, total}` —— 将来按各家单价折算时按桶算。
    """
    out = {**empty_detail(), **(detail or {})}
    out["by_model"] = dict(out.get("by_model") or {})
    if not usage:
        return out

    out["input"] = _add(out.get("input"), usage.get("input"))
    out["output"] = _add(out.get("output"), usage.get("output"))
    out["cache_read"] = _add(out.get("cache_read"), usage.get("cache_read"))
    out["calls"] = int(out.get("calls") or 0) + 1
    if usage.get("unmetered"):
        out["unmetered_calls"] = int(out.get("unmetered_calls") or 0) + 1
    ts = str(usage.get("ts") or "")
    if ts:
        out["first_ts"] = out.get("first_ts") or ts
        out["last_ts"] = ts
    name = str(usage.get("model") or "") or "（未知）"
    bucket = dict(out["by_model"].get(name) or {"calls": 0, "total": 0})
    bucket["calls"] = int(bucket.get("calls") or 0) + 1
    bucket["total"] = int(bucket.get("total") or 0) + int(usage.get("total") or 0)
    out["by_model"][name] = bucket
    return out
