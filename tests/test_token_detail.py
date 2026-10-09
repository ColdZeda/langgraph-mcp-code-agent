"""token **计量明细**（计费原料）的回归测试。

覆盖（"只计量、不算钱"改造）：
  1. usage 字段归一化 —— 缓存命中的三种写法 + **langchain 实际给的形状**（已用真实 DeepSeek 响应核对：
     它同时返回 `prompt_cache_hit_tokens` 与 `prompt_tokens_details.cached_tokens`，后者被 langchain 映射成
     `input_token_details.cache_read`）
  2. **缺失 usage 不许静默按 0** —— 必须标 `unmetered`（否则预算闸门会失效）
  3. 累加语义：`None` 与 `0` 是两件事；`by_model` 分桶
  4. 状态条快照带出三项明细 + 未计量次数
  5. `token_detail` 是新通道 ⇒ 必须在 `PER_TURN_RESET` 里（跨轮不复位就会串味）
  6. `_msg_tokens` 仍然返回 total（老调用点与老测试不受影响）
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from app.code_agent.agent import multi_agent as ma
from app.code_agent.agent.cancel import CancelToken
from app.code_agent.agent.multi_agent import PER_TURN_RESET, _msg_tokens, _msg_usage, per_turn_reset
from app.code_agent.agent.usage import empty_detail, merge_token_detail, normalize_usage


def _msg(usage: dict | None, model: str = "deepseek-flash") -> AIMessage:
    """造一条带 usage 的假 AI 消息（字段形状与 langchain 的 `UsageMetadata` 一致）。"""
    kwargs: dict[str, Any] = {"content": "ok", "response_metadata": {"model_name": model}}
    if usage is not None:
        kwargs["usage_metadata"] = usage
    return AIMessage(**kwargs)


def _usage(inp: int, out: int, *, cache_read: int | None = None, extra: dict | None = None) -> dict:
    u = {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}
    if cache_read is not None:
        u["input_token_details"] = {"cache_read": cache_read}
    if extra:
        u.update(extra)
    return u


# ── 1. 字段归一化 ──────────────────────────────────────────────


def test_langchain_shape_cache_read_is_picked_up() -> None:
    """langchain 的形状：缓存命中在 `input_token_details.cache_read`（DeepSeek/OpenAI 都走这条）。"""
    u = normalize_usage(_msg(_usage(1000, 200, cache_read=800)))
    assert (u["input"], u["output"], u["total"], u["cache_read"]) == (1000, 200, 1200, 800)
    assert u["unmetered"] is False
    assert u["model"] == "deepseek-flash"
    assert u["ts"].endswith("+00:00")


def test_raw_deepseek_field_names_also_work() -> None:
    """兜底：若某天适配层直接透传 DeepSeek 原生字段名，也要认（`prompt_cache_hit_tokens`）。"""
    u = normalize_usage(_msg(_usage(500, 50, extra={"prompt_cache_hit_tokens": 400})))
    assert u["cache_read"] == 400


def test_raw_anthropic_field_names_also_work() -> None:
    """兜底：Anthropic 的 `cache_read_input_tokens`。"""
    u = normalize_usage(_msg(_usage(300, 30, extra={"cache_read_input_tokens": 200})))
    assert u["cache_read"] == 200


def test_null_usage_is_unmetered_and_not_zeroed() -> None:
    """🔴 provider 没返回 usage ⇒ 必须标 `unmetered`，且**明细不许写成 0**（0 与"没有"是两件事）。"""
    u = normalize_usage(_msg(None))
    assert u["total"] == 0
    assert u["unmetered"] is True
    assert u["input"] is None and u["output"] is None and u["cache_read"] is None


def test_only_total_present_means_metered_but_no_split() -> None:
    """只给 total（形状不合 langchain 规范）⇒ **算已计量**（总数是有的），但拆分留 None、不猜 0。"""
    msg = SimpleNamespace(usage_metadata={"total_tokens": 5}, response_metadata={})
    u = normalize_usage(msg)
    assert u["total"] == 5
    assert u["unmetered"] is False
    assert u["input"] is None and u["output"] is None and u["cache_read"] is None


def test_zero_cache_read_is_kept_as_zero_not_none() -> None:
    """真的返回 0（这次没命中缓存）⇒ 保留 0，别当成"缺字段"。"""
    u = normalize_usage(_msg(_usage(10, 1, cache_read=0)))
    assert u["cache_read"] == 0
    assert u["unmetered"] is False


# ── 2. 累加 ────────────────────────────────────────────────────


def test_merge_sums_and_counts_unmetered() -> None:
    a = normalize_usage(_msg(_usage(100, 10, cache_read=60)))
    b = normalize_usage(_msg(None))  # 未计量的一次
    d = merge_token_detail(merge_token_detail(None, a), b)
    assert d["input"] == 100 and d["output"] == 10 and d["cache_read"] == 60
    assert d["calls"] == 2
    assert d["unmetered_calls"] == 1
    assert d["first_ts"] and d["last_ts"]


def test_merge_all_none_stays_none() -> None:
    """两次都没明细 ⇒ 明细仍是 None（**不能变成 0**，否则界面会显示"缓存命中 0"这种假结论）。"""
    d = merge_token_detail(None, normalize_usage(_msg(None)))
    assert d["input"] is None and d["output"] is None and d["cache_read"] is None


def test_merge_buckets_by_model() -> None:
    d = merge_token_detail(None, normalize_usage(_msg(_usage(60, 40), model="m-a")))
    d = merge_token_detail(d, normalize_usage(_msg(_usage(200, 100), model="m-b")))
    d = merge_token_detail(d, normalize_usage(_msg(_usage(60, 40), model="m-a")))
    assert d["by_model"]["m-a"] == {"calls": 2, "total": 200}
    assert d["by_model"]["m-b"] == {"calls": 1, "total": 300}


def test_merge_does_not_mutate_input() -> None:
    base = empty_detail()
    merge_token_detail(base, normalize_usage(_msg(_usage(5, 4))))
    assert base["calls"] == 0


# ── 3. 状态条 / 通道 / 兼容性 ───────────────────────────────────


def test_cancel_token_snapshot_carries_detail_and_unmetered() -> None:
    t = CancelToken()
    t.tokens = 1200
    t.add_usage(normalize_usage(_msg(_usage(1000, 100, cache_read=900))))
    t.add_usage(normalize_usage(_msg(_usage(90, 10))))
    snap = t.snapshot()
    assert snap["tokens"] == 1200
    assert snap["inputTokens"] == 1090
    assert snap["outputTokens"] == 110
    assert snap["cacheReadTokens"] == 900
    assert snap["unmeteredCalls"] == 0
    for key in (
        "elapsedSec",
        "pausedSec",
        "steps",
        "usageLimit",
        "wallClockSec",
        "stage",
        "cancelled",
        "cancelReason",
    ):
        assert key in snap, f"老字段 {key} 不能丢（前端与既有测试都在用）"


def test_token_detail_is_reset_every_turn() -> None:
    """🔴 新通道必须进 `PER_TURN_RESET`：LangGraph 不会自动清"没传进 ainvoke"的通道。"""
    assert "token_detail" in PER_TURN_RESET
    assert PER_TURN_RESET["token_detail"] == {}
    a, b = per_turn_reset(), per_turn_reset()
    a["token_detail"]["input"] = 1
    assert b["token_detail"] == {}, "两次复位必须互不影响（否则可变默认值会串）"


def test_msg_tokens_still_returns_total() -> None:
    """`_msg_tokens` 保留语义（老的调用点/测试不用改）。"""
    assert _msg_tokens(_msg(_usage(30, 12))) == 42
    assert _msg_tokens(_msg(None)) == 0


def test_msg_usage_uses_server_reported_model_name() -> None:
    u = _msg_usage(_msg(_usage(5, 2), model="mimo-v2.6-flash"))
    assert u["model"] == "mimo-v2.6-flash"
    assert u["total"] == 7


# ── 4. 与节点集成：**只有模型输出才计用量**（真机踩过的误报） ─────────


class _FakeInvokeLLM:
    """假路由模型（同步 `invoke`）。"""

    def __init__(self, resp: AIMessage) -> None:
        self._resp = resp

    def invoke(self, prompt: str) -> AIMessage:  # noqa: ARG002
        return self._resp


class _FakeStreamAgent:
    """假 Executor/Verifier：按 astream 的块协议吐出给定的消息。"""

    def __init__(self, chunks: list[dict]) -> None:
        self._chunks = chunks

    async def astream(self, inputs, config=None):  # noqa: ANN001, ARG002
        for chunk in self._chunks:
            yield chunk


def _exec_state() -> dict:
    return {
        "user_input": "测试任务",
        "plan": "",
        "executor_trace_list": [],
        "executor_messages": [],
        "verifier_messages": [],
        "messages": [],
        "token_usage": 0,
        "token_detail": {},
        "step_count": 0,
        "retry_count": 0,
        "pruned_messages": 0,
        "verdict": "",
        "executor_result": "",
    }


async def test_tool_messages_are_not_counted_as_calls() -> None:
    """🔴 工具结果（ToolMessage）**不是**一次模型调用。

    真机现场（2026-10-09）：14 步的任务在结果卡上写「8 次调用未返回用量」——
    原因就是累积循环把工具结果也当成了模型调用（它们当然没有 usage）。
    """
    ai = _msg(_usage(100, 20, cache_read=60))
    tool = ToolMessage(content="工具结果", tool_call_id="call-1")
    agent = _FakeStreamAgent([{"agent": {"messages": [ai, tool]}}])

    out = await ma.executor_node(_exec_state(), agent)  # type: ignore[arg-type]

    assert out["token_usage"] == 120
    assert out["token_detail"]["calls"] == 1, "只有 AI 消息算一次调用"
    assert out["token_detail"]["unmetered_calls"] == 0, "工具结果不许被算成「未计量」"
    assert out["token_detail"]["input"] == 100
    assert out["token_detail"]["cache_read"] == 60


async def test_ai_message_without_usage_is_flagged() -> None:
    """真的没返回 usage 的**模型输出** ⇒ 必须被标出来（这正是这个功能要抓的情况）。"""
    ai = AIMessage(content="done")  # 故意不带 usage_metadata
    agent = _FakeStreamAgent([{"agent": {"messages": [ai]}}])

    out = await ma.executor_node(_exec_state(), agent)  # type: ignore[arg-type]

    assert out["token_usage"] == 0
    assert out["token_detail"]["calls"] == 1
    assert out["token_detail"]["unmetered_calls"] == 1


async def test_router_usage_is_counted(monkeypatch) -> None:
    """路由那一次模型调用**也要计**（以前只取结论、把用量丢了）。"""
    llm = _FakeInvokeLLM(_msg(_usage(80, 5)))
    monkeypatch.setattr(ma, "get_llm", lambda *a, **k: llm)
    monkeypatch.setattr(ma, "COMPLEX_KEYWORDS", ())  # 逼它走 LLM 分类那条路

    out = await ma.route_node({"user_input": "随便一个任务", "token_usage": 0})  # type: ignore[arg-type]

    assert out["route"] == "simple"
    assert out["token_usage"] == 85
    assert out["token_detail"]["calls"] == 1
    assert out["token_detail"]["input"] == 80
