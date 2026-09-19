"""执行模式路由测试（T3.1）。

覆盖三件事：
1. 路由判定：规则兜底（不调 LLM）/ LLM 分类 / **失败时保守走 complex**；
2. `route_node` 必须把结论写进 state（LangGraph 条件边函数不能写 state，所以必须单独有节点）；
3. 三种执行模式的图接线：single 不跑规划与验收、auto+simple 同 single、auto+complex 走完整三阶段。
"""

import sys
from pathlib import Path
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.agent import multi_agent as ma

PLAN_JSON = '{"goal": "g", "steps": ["s1"], "verify_tools": ["read_file_range"]}'
PASS_VERDICT = '{"verdict": "PASS", "reason": "ok"}'


class _FakeLLM:
    def __init__(self, content: str = PLAN_JSON) -> None:
        self.content = content

    async def ainvoke(self, messages, **kwargs):
        return AIMessage(
            content=self.content,
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    def invoke(self, prompt, **kwargs):
        return AIMessage(content=self.content)


class _BoomLLM:
    """任何调用都抛异常（模拟模型不可用）。"""

    def invoke(self, *a, **k):
        raise RuntimeError("model unavailable")

    async def ainvoke(self, *a, **k):
        raise RuntimeError("model unavailable")


class _FakeAgent:
    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.calls = 0
        self.inputs: list = []

    async def astream(self, inputs, config=None):
        idx = min(self.calls, len(self.replies) - 1)
        self.calls += 1
        self.inputs.append(inputs)
        yield {"agent": {"messages": [AIMessage(content=self.replies[idx])]}}


@pytest.fixture
def thread_id(monkeypatch, tmp_path):
    monkeypatch.setattr(ma, "CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(ma, "get_llm", lambda *a, **k: _FakeLLM())
    return f"route-test-{uuid4().hex[:8]}"


# ── ① 路由判定 ──


def test_route_keyword_fallback_does_not_call_llm(monkeypatch):
    """含"删除/重构"等关键词 → 必走 complex，且**不调用 LLM**。"""
    called = []
    monkeypatch.setattr(ma, "_llm_classify_complexity", lambda t: called.append(t) or "simple")

    assert ma.route_task("删除所有临时文件") == "complex"
    assert ma.route_task({"user_input": "帮我重构整个项目"}) == "complex"
    assert called == [], "规则命中时不该再调用 LLM"


def test_route_simple_query_uses_llm_classifier(monkeypatch):
    """简单查询 + LLM 判 simple → 走 simple。"""
    monkeypatch.setattr(ma, "_llm_classify_complexity", lambda t: "simple")
    assert ma.route_task("查看 config.py 的前 20 行") == "simple"


def test_route_llm_failure_falls_back_to_complex(monkeypatch):
    """LLM 分类失败 → 保守走 complex（宁可多花 token 也别漏验证）。"""
    monkeypatch.setattr(ma, "get_llm", lambda *a, **k: _BoomLLM())
    assert ma.route_task("查看 config.py 的前 20 行") == "complex"


async def test_route_node_persists_route_key(monkeypatch):
    """route_node 必须把结论写进 state —— 否则条件边拿不到 route。"""
    monkeypatch.setattr(ma, "route_task", lambda state: "simple")
    assert await ma.route_node({"user_input": "x"}) == {"route": "simple"}


def test_route_decide_reads_state_and_defaults_conservative():
    assert ma._route_decide({"route": "simple"}) == "simple"
    assert ma._route_decide({"route": "complex"}) == "complex"
    assert ma._route_decide({}) == "complex", "读不到 route 时应保守走复杂路径"


def test_after_executor_uses_route():
    assert ma.after_executor({"route": "simple"}) == "end"
    assert ma.after_executor({"route": "complex"}) == "verifier"


# ── ② 三种执行模式的图接线 ──


async def test_single_mode_skips_planner_and_verifier(thread_id):
    """single：只跑 Executor，不规划、不验收。"""
    executor = _FakeAgent(["done"])
    verifier = _FakeAgent([PASS_VERDICT])

    result = await ma.run_multi_agent(
        "查看 config.py 的前 20 行",
        [],
        executor_agent=executor,
        verifier_agent=verifier,
        thread_id=thread_id,
        mode="single",
    )

    assert executor.calls == 1
    assert verifier.calls == 0, "single 模式不应调用 Verifier"
    assert result["mode"] == "single"
    assert result["plan"] == "" and result["verdict"] == "", "single 模式没有计划与验收结论"


async def test_auto_simple_route_skips_planner_and_verifier(thread_id, monkeypatch):
    """auto + 判为 simple：等价于 single（不规划、不验收），且 route 记录在结果里。"""
    monkeypatch.setattr(ma, "route_task", lambda state: "simple")
    executor = _FakeAgent(["done"])
    verifier = _FakeAgent([PASS_VERDICT])

    result = await ma.run_multi_agent(
        "今天有什么新闻",
        [],
        executor_agent=executor,
        verifier_agent=verifier,
        thread_id=thread_id,
        mode="auto",
    )

    assert executor.calls == 1
    assert verifier.calls == 0
    assert result["route"] == "simple"
    assert result["mode"] == "auto"


async def test_auto_complex_route_runs_full_graph(thread_id, monkeypatch):
    """auto + 判为 complex：走完整 Planner → Executor → Verifier。"""
    monkeypatch.setattr(ma, "route_task", lambda state: "complex")
    executor = _FakeAgent(["done"])
    verifier = _FakeAgent([PASS_VERDICT])

    result = await ma.run_multi_agent(
        "重构整个项目",
        [],
        executor_agent=executor,
        verifier_agent=verifier,
        thread_id=thread_id,
        mode="auto",
    )

    assert executor.calls == 1
    assert verifier.calls == 1, "complex 路径必须经过验收"
    assert result["route"] == "complex"
    assert result["plan"], "complex 路径应产出计划"
    assert "PASS" in result["verdict"].upper()


async def test_multi_mode_always_runs_full_graph(thread_id):
    """multi：跳过路由，恒定走完整三阶段。"""
    executor = _FakeAgent(["done"])
    verifier = _FakeAgent([PASS_VERDICT])

    result = await ma.run_multi_agent(
        "查看 config.py",  # 即使是最简单的任务也走完整流程
        [],
        executor_agent=executor,
        verifier_agent=verifier,
        thread_id=thread_id,
        mode="multi",
    )

    assert executor.calls == 1 and verifier.calls == 1
    assert result["mode"] == "multi"
    assert result["route"] == "", "multi 模式不经过路由，route 应为空"
