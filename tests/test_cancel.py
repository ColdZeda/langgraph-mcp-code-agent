"""阶段 8 · P0：**停止**（协作式取消）+ **墙钟** + 任务状态条。

守的是**状态断言**，不是文案：

| 要守的事 | 怎么测 |
|---|---|
| 停止发生在"下一步边界" | 第 1 步的产物**在**，第 2 步的**不在** |
| 已发生的副作用保留 | 同上（不回滚是最重要的语义） |
| 每步检查点真的生效 | 假执行器的 `astream` 迭代次数停在预期值 |
| 终止后不再白调 Verifier | 假 Verifier 的调用次数 == 0 |
| 墙钟走同一条停止路径 | 只有 `cancel_reason` 不同（`wall_clock`） |
| ⚠️ 人工确认期间不计时 | 弹框里等 0.3s、墙钟 0.05s ⇒ **不该**被判定超时 |
| 停止优先于"允许/拒绝" | 弹框被停止收掉时**不留** `denied_by_user` 审计 |
| 工具收口拦得住 | 直接调被包装的工具 ⇒ 抛 `TaskCancelled` 且**没执行** |

⚠️ 假执行器**必须复刻 `create_react_agent` 的块顺序**（工具调用发生在"吐下一块之前"）——
按别的顺序写会得到一个假象："循环开头的检查救下了已经跑掉的工具"。
真实链路里救下它的是**工具层那道检查**（`utils/tool_wrap.py::_process`），
异常穿透 LangGraph 的 `ToolNode` 解栈到节点，所以下面专门有一条测试守它。
"""

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.agent import cancel as cx  # noqa: E402
from app.code_agent.agent import multi_agent as ma  # noqa: E402
from app.code_agent.security import permissions as perm  # noqa: E402
from app.code_agent.utils import tool_wrap as tw  # noqa: E402
from tests.test_tool_wrap import _Tool  # noqa: E402

# ═══════════════════════════════════════════════════════════════════
# 替身
# ═══════════════════════════════════════════════════════════════════


def _writing_tool(tmp_path, name="write_file", calls=None):
    """一个"会留下副作用"的被包装工具：每次调用写一个文件（副作用可观测）。"""

    async def _coro(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        (tmp_path / str(kwargs["file"])).write_text("x", encoding="utf-8")
        return "ok"

    return tw.wrap_tool(_Tool(name, coroutine=_coro))


class _StepwiseExecutor:
    """执行器替身：**按真实图的块顺序**一步步走，每一步的工具调用都是真的。

    真实链路是 `create_react_agent` 的 `agent → tools → agent → …`：`astream` **每完成一个
    图节点吐一块**，所以顺序是

        [agent: AIMessage(含 tool_calls)] → [tools: ToolMessage] → [agent: …] → …

    ⚠️ 替身必须照这个顺序吐块，否则测出来的是假象：
      · 若把"工具调用"放在 yield **之后**，会变成"取下一块时才调工具"，
        于是"循环开头的检查"看起来救下了工具 —— 但真实链路里救下它的是
        **工具层那道检查**（`tool_wrap._process`），异常再穿透 ToolNode 解栈回节点；
      · 若放在 yield **之前**，第 1 步的 AIMessage 都还没被记录，轨迹会少一笔。
    """

    def __init__(
        self,
        token,
        tool,
        *,
        steps: int = 3,
        cancel_after: int | None = 1,
        reason: str = cx.REASON_USER,
        sleep_between: float = 0.0,
    ) -> None:
        self.token = token
        self.tool = tool
        self.steps = steps
        self.cancel_after = cancel_after
        self.reason = reason
        self.sleep_between = sleep_between
        self.ran: list[int] = []
        self.calls = 0

    async def astream(self, inputs, config=None):
        self.calls += 1
        for i in range(1, self.steps + 1):
            # ① 模型那一块：模型说"这一步要调 write_file"
            yield {
                "agent": {
                    "messages": [
                        AIMessage(
                            content=f"第 {i} 步：调用 write_file",
                            tool_calls=[
                                {
                                    "name": "write_file",
                                    "args": {"file": f"step{i}.txt"},
                                    "id": f"call{i}",
                                }
                            ],
                        )
                    ]
                }
            }
            # ② 工具那一块：**真正的工具调用发生在取这一块的时候**（也就是两次 yield 之间）
            if i > 1 and self.sleep_between:
                await asyncio.sleep(self.sleep_between)  # 让墙钟有机会到点
            await self.tool.coroutine(file=f"step{i}.txt")  # 可能被停止检查拦下
            self.ran.append(i)
            yield {
                "tools": {
                    "messages": [
                        ToolMessage(content="ok", name="write_file", tool_call_id=f"call{i}")
                    ]
                }
            }
            if self.cancel_after is not None and i == self.cancel_after:
                # 用户在这一步**做完之后**点了停止（下一块要靠取出来才知道，所以这就是"下一步边界"）
                self.token.cancel(self.reason)


class _CountingAgent:
    """假 Verifier：只数被调了几次（用来断言"终止后不再白调验收"）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def astream(self, inputs, config=None):
        self.calls += 1
        yield {"agent": {"messages": [AIMessage(content='{"verdict": "PASS", "reason": "ok"}')]}}


@pytest.fixture
def thread_id(monkeypatch, tmp_path):
    """隔离的 checkpoint DB + 独立 thread_id（与 test_multi_agent.py 同一套做法）。"""
    monkeypatch.setattr(ma, "CHECKPOINT_DB", tmp_path / "checkpoints.db")
    return f"cancel-{uuid4().hex[:8]}"


class _FakeRegistry:
    """假注册表 —— ⚠️ 只 patch `get_llm` 是不够的：`planner_node` 走 `registry.chain("planner")`，
    不换掉就会**真的去调模型**（本机实测：那会打成 401，而不是"测试失败"）。
    （与 `tests/test_multi_agent.py` 同一个理由、同一套写法。）"""

    def __init__(self, llm):
        self._llm = llm

    def chain(self, role="executor"):
        return [self._llm]


class _FakePlannerLLM:
    """假 Planner：固定返回计划 JSON，并记下被调了几次。"""

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        return AIMessage(content='{"goal": "g", "steps": ["s1", "s2"]}')


@pytest.fixture
def fake_planner(monkeypatch):
    llm = _FakePlannerLLM()
    monkeypatch.setattr(ma, "get_llm", lambda *a, **k: llm)
    monkeypatch.setattr(ma, "registry", _FakeRegistry(llm))
    return llm


async def _run(tmp_path, token, agent, *, mode="single", thread="t", verifier=None):
    """跑一次任务（**在调用方绑好停止开关** —— 与 Web / CLI 的接线方式一致）。"""
    with cx.bind_cancel(token):
        return await ma.run_multi_agent(
            "把 step1..step3 写到目标目录",
            [],
            executor_agent=agent,
            verifier_agent=verifier or _CountingAgent(),
            thread_id=thread,
            mode=mode,
            auto_inject=False,
            auto_deposit=False,
        )


# ═══════════════════════════════════════════════════════════════════
# 一、停止开关本身
# ═══════════════════════════════════════════════════════════════════


def test_task_cancelled_is_a_base_exception():
    """🔴 它必须**不是** `Exception` 的子类。

    工具调用发生在 LangGraph 的 `ToolNode` 里，它 `except Exception` 会把异常变成一条
    ToolMessage 交给模型 ⇒ "停止"会退化成"模型看到一条奇怪的错误、再决定下一步"。
    继承 `BaseException` 才能穿透它（与 `asyncio.CancelledError` 同理）。
    """
    assert issubclass(cx.TaskCancelled, BaseException)
    assert not issubclass(cx.TaskCancelled, Exception)

    def _swallowed_by_except_exception() -> bool:
        try:
            raise cx.TaskCancelled(cx.REASON_USER)
        except Exception:  # noqa: BLE001 —— 这里就是要证明它**抓不到**
            return True
        except BaseException:  # noqa: BLE001 —— 只有它抓得到
            return False

    assert _swallowed_by_except_exception() is False, (
        "TaskCancelled 被 `except Exception` 抓到了 ⇒ 停止会被 LangGraph 的 ToolNode 吞成一条 ToolMessage"
    )


def test_cancel_reason_keeps_the_first_one():
    """原因码只记第一次：用户先点停止，之后墙钟到点不该把原因改写掉。"""
    token = cx.CancelToken(wall_clock=0.01)
    assert token.cancel(cx.REASON_USER) is True
    assert token.cancel(cx.REASON_WALL_CLOCK) is False, "重复置位应返回 False"
    assert token.reason == cx.REASON_USER

    asyncio.run(asyncio.sleep(0.02))
    assert token.cancelled is True
    assert token.reason == cx.REASON_USER, "已经是 user 的不该被墙钟改写"


def test_wall_clock_marks_itself_expired():
    token = cx.CancelToken(wall_clock=0.01)
    assert token.cancelled is False
    asyncio.run(asyncio.sleep(0.02))
    assert token.cancelled is True
    assert token.reason == cx.REASON_WALL_CLOCK


def test_no_wall_clock_means_never_expires():
    token = cx.CancelToken(wall_clock=0)  # 0 / None = 不限制
    assert token.cancelled is False
    assert cx.CancelToken().cancelled is False


def test_snapshot_shape_is_what_the_status_bar_needs():
    token = cx.CancelToken(wall_clock=900)
    token.steps, token.tokens, token.usage_limit, token.stage = 3, 1234, 200000, "executor"
    snap = token.snapshot()

    assert snap["steps"] == 3
    assert snap["tokens"] == 1234
    assert snap["usageLimit"] == 200000
    assert snap["wallClockSec"] == 900
    assert snap["stage"] == "executor"
    assert snap["cancelled"] is False
    assert snap["elapsedSec"] >= 0


def test_raise_if_cancelled_is_a_noop_without_a_token():
    """没有绑定停止开关（evals / 单测）时，检查点必须是空操作。"""
    cx.raise_if_cancelled()  # 不抛就算过


# ═══════════════════════════════════════════════════════════════════
# 二、工具收口（每次工具调用之前）
# ═══════════════════════════════════════════════════════════════════


async def test_wrapped_tool_does_not_run_after_stop(tmp_path):
    """停止之后**下一次工具调用**不执行，而之前那次留下的副作用还在。"""
    calls: list[dict] = []
    token = cx.CancelToken()
    tool = _writing_tool(tmp_path, calls=calls)

    with cx.bind_cancel(token):
        assert await tool.coroutine(file="a.txt") == "ok"
        token.cancel(cx.REASON_USER)
        with pytest.raises(cx.TaskCancelled) as excinfo:
            await tool.coroutine(file="b.txt")

    assert excinfo.value.reason == cx.REASON_USER
    assert (tmp_path / "a.txt").exists(), "停止前的副作用必须保留（不回滚）"
    assert not (tmp_path / "b.txt").exists(), "停止之后不该再执行任何工具"
    assert len(calls) == 1


async def test_sync_tool_path_is_guarded_too(tmp_path):
    """只有同步 `_run` 的工具（FileManagementToolkit 那一类）走的是另一条分支，同样要拦。"""
    ran: list[str] = []

    def _run(**kwargs):
        ran.append(str(kwargs["file"]))
        return "ok"

    token = cx.CancelToken()
    tool = tw.wrap_tool(_Tool("write_file", run=_run))

    with cx.bind_cancel(token):
        assert tool.func(file="a.txt") == "ok"
        token.cancel(cx.REASON_USER)
        with pytest.raises(cx.TaskCancelled):
            tool.func(file="b.txt")

    assert ran == ["a.txt"]


async def test_real_tool_node_lets_the_stop_signal_through():
    """🔴 **这条守的是"停止能不能穿透真 LangGraph"**（其余用例用的是假执行器，验不到它）。

    真链路：工具调用发生在 `ToolNode` 里，而它 `except Exception`（`handle_tool_errors` 默认 True）
    会把异常**变成一条 ToolMessage**交给模型。所以 `TaskCancelled` 必须是 `BaseException`，
    否则"停止"会变成"模型看到一条奇怪的错误、再决定下一步"（多烧一次调用，还可能换个办法接着干）。

    这里用**真的 `ToolNode` + 真的被包装工具**，不造假：
      - 第 1 次：正常执行；
      - 置停止位后第 2 次：抛 `TaskCancelled`，且**工具一次都没执行**。
    """
    from langchain_core.tools import StructuredTool
    from langgraph.prebuilt.tool_node import ToolNode
    from pydantic import BaseModel, Field

    class _Args(BaseModel):
        file_path: str = Field(description="要读的路径")

    calls: list[str] = []

    async def _coro(file_path: str) -> str:
        calls.append(file_path)
        return "文件内容"

    # 工具名取只读工具（任何档位都放行）—— 这条测的是停止，不是权限
    tool = StructuredTool(
        name="read_file", description="读文件", args_schema=_Args, coroutine=_coro
    )
    wrapped = tw.wrap_tool(tool)
    node = ToolNode([wrapped])
    call = AIMessage(
        content="",
        tool_calls=[{"name": "read_file", "args": {"file_path": "a.py"}, "id": "c1"}],
    )
    token = cx.CancelToken()

    with cx.bind_cancel(token):
        first = await node.ainvoke({"messages": [call]})
        assert calls == ["a.py"], "第一次应当正常执行"
        assert first["messages"], "正常路径仍要拿到工具结果"

        token.cancel(cx.REASON_USER)
        with pytest.raises(cx.TaskCancelled):
            await node.ainvoke({"messages": [call]})

    assert calls == ["a.py"], "停止之后 ToolNode 绝不能再执行工具（它没被吞成一条 ToolMessage）"


# ═══════════════════════════════════════════════════════════════════
# 三、执行器：停在下一步边界（P0 的核心断言）
# ═══════════════════════════════════════════════════════════════════


class _ScriptedToolModel(BaseChatModel):
    """假模型：按脚本吐 `tool_calls` / 最终答复（**不发任何网络请求**）。

    为什么自己写而不用现成的 `GenericFakeChatModel`：后者的 `bind_tools()` 直接
    `raise NotImplementedError`，而 `create_react_agent` 一定会调它（本机验过）。
    """

    script: list = []
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=self.script[idx])])

    def bind_tools(self, tools, **kwargs):  # type: ignore[override]
        return self


async def test_real_react_graph_stops_between_two_model_calls(tmp_path, thread_id, fake_planner):
    """🔴 **真图端到端**：`create_react_agent` + 真 `ToolNode` + 真包装工具。

    要证的是一条很硬的话：**停止之后不该再发生第二次模型调用**。
    脚本是"调工具 → 调工具 → 收尾"，而停止位在**第一次工具执行完**时置上 ⇒
    第 2 次模型调用根本不该发生（token 也就不会再烧）。
    """
    from langgraph.prebuilt import create_react_agent

    from app.code_agent.agent import events as ev

    token = cx.CancelToken()
    ran: list[str] = []

    async def _coro(file: str) -> str:
        ran.append(file)
        (tmp_path / file).write_text("x", encoding="utf-8")
        if len(ran) == 1:
            token.cancel(cx.REASON_USER)  # 用户第一次工具刚做完就点了停止
        return "ok"

    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, Field

    class _Args(BaseModel):
        file: str = Field(description="文件名")

    tool = tw.wrap_tool(
        StructuredTool(name="write_file", description="写文件", args_schema=_Args, coroutine=_coro)
    )
    model = _ScriptedToolModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[{"name": "write_file", "args": {"file": "a.txt"}, "id": "c1"}],
            ),
            AIMessage(
                content="",
                tool_calls=[{"name": "write_file", "args": {"file": "b.txt"}, "id": "c2"}],
            ),
            AIMessage(content="都写完了"),
        ]
    )
    executor = create_react_agent(model=model, tools=[tool], prompt="按计划执行")

    seen: list[dict] = []
    with ev.bind_sink(lambda e: seen.append(e)):
        result = await _run(tmp_path, token, executor, thread=thread_id)

    assert ran == ["a.txt"], f"只该执行第一次工具调用，实际 {ran}"
    assert (tmp_path / "a.txt").exists(), "停止前那一次副作用必须保留"
    assert not (tmp_path / "b.txt").exists()
    assert model.calls == 1, f"停止之后不该再有模型调用（实际 {model.calls} 次）⇒ 否则照样烧 token"
    assert result["cancelled"] is True
    assert result["cancel_reason"] == cx.REASON_USER
    assert [t["name"] for t in result["executor_trace_list"]] == ["write_file"]
    assert "验收未通过" not in result["final_response"]


async def test_stop_leaves_the_finished_step_and_blocks_the_next(tmp_path, thread_id):
    """**P0 的验收断言**：中途停止 ⇒ 下一步不执行 + 副作用保留 + 返回 `cancelled`。"""
    token = cx.CancelToken()
    agent = _StepwiseExecutor(token, _writing_tool(tmp_path), steps=3, cancel_after=1)

    result = await _run(tmp_path, token, agent, thread=thread_id)

    assert agent.ran == [1], f"只该跑完第 1 步，实际跑了 {agent.ran}"
    assert (tmp_path / "step1.txt").exists(), "已完成那一步的产物必须保留"
    assert not (tmp_path / "step2.txt").exists(), "下一步的产物不该出现"
    assert not (tmp_path / "step3.txt").exists()

    assert result["cancelled"] is True
    assert result["cancel_reason"] == cx.REASON_USER
    assert result["cancel_stage"] == "executor"
    # 步数口径沿用既有定义 = `astream` 的块数（真实图里"走一步"会吐 agent / tools 两块）
    assert result["step_count"] == 2, (
        f"步数要如实（跑了 1 步、吐了 2 块），实际 {result['step_count']}"
    )
    assert [t["name"] for t in result["executor_trace_list"]] == ["write_file"], (
        "已经发生的那次工具调用要留在轨迹里（它是「到底干了什么」的唯一记录）"
    )
    assert result["final_response"].startswith("【已停止】")
    assert "验收未通过" not in result["final_response"], "停止不能被说成验收未通过"


async def test_stop_does_not_call_the_verifier(tmp_path, thread_id, fake_planner):
    """终止路径**不再白调 Verifier**（multi 模式的出边以前是无条件边 —— P1 的账）。"""
    token = cx.CancelToken()
    verifier = _CountingAgent()
    agent = _StepwiseExecutor(token, _writing_tool(tmp_path), steps=3, cancel_after=1)

    result = await _run(tmp_path, token, agent, mode="multi", thread=thread_id, verifier=verifier)

    assert result["cancelled"] is True
    assert verifier.calls == 0, "任务已经停了，还去跑一次验收 = 白花一次模型调用"
    assert result["verdict"] == ""


async def test_wall_clock_takes_the_same_path(tmp_path, thread_id):
    """墙钟到点 ⇒ **同一条**停止路径，只有原因码不同。"""
    token = cx.CancelToken(wall_clock=0.05)
    # 每步之间睡 80ms > 墙钟 50ms ⇒ 第 2 步的工具调用会被拦下
    agent = _StepwiseExecutor(
        token, _writing_tool(tmp_path), steps=3, cancel_after=None, sleep_between=0.08
    )

    result = await _run(tmp_path, token, agent, thread=thread_id)

    assert agent.ran == [1], f"墙钟到点后不该再跑下一步，实际跑了 {agent.ran}"
    assert (tmp_path / "step1.txt").exists()
    assert not (tmp_path / "step2.txt").exists()
    assert result["cancelled"] is True
    assert result["cancel_reason"] == cx.REASON_WALL_CLOCK
    assert "CODE_AGENT_TASK_WALL_CLOCK" in result["final_response"], "要说清是哪种停止"


async def test_stop_before_the_node_runs_does_nothing_at_all(tmp_path, thread_id):
    """**节点入口**的检查：还没开始执行就停了 ⇒ 一步都不跑（连 `astream` 都不该被调）。"""
    token = cx.CancelToken()
    token.cancel(cx.REASON_USER)
    agent = _StepwiseExecutor(token, _writing_tool(tmp_path), steps=3)

    result = await _run(tmp_path, token, agent, thread=thread_id)

    assert agent.calls == 0, "节点入口就该停下，不该进 ReAct 循环"
    assert agent.ran == []
    assert list(tmp_path.glob("step*.txt")) == []
    assert result["cancelled"] is True
    assert result["executor_trace_list"] == []
    assert "一步都没跑" in result["final_response"]


async def test_status_events_carry_elapsed_steps_and_tokens(tmp_path, thread_id):
    """状态条数据：绑了停止开关才推 `status` 事件；步数/用量/上限都要如实。"""
    from app.code_agent.agent import events as ev

    seen: list[dict] = []
    token = cx.CancelToken(wall_clock=900)
    agent = _StepwiseExecutor(token, _writing_tool(tmp_path), steps=2, cancel_after=None)

    with ev.bind_sink(lambda e: seen.append(e)):
        result = await _run(tmp_path, token, agent, thread=thread_id)

    status = [e for e in seen if e["type"] == "status"]
    assert status, "绑了停止开关就该推状态条事件"
    last = status[-1]
    # 步数口径 = executor 的 `astream` 块数（真实图里一步会吐 agent/tools 两块）
    assert last["steps"] == result["step_count"] >= 1
    assert last["wallClockSec"] == 900
    assert last["usageLimit"] == ma.TASK_TOKEN_BUDGET
    assert isinstance(last["elapsedSec"], float)
    assert last["stage"] == "executor"
    assert result["status"]["steps"] == last["steps"]

    # 没绑 ⇒ 一条都不发（evals / 单测零改动）
    seen.clear()
    await _run(tmp_path, None, agent, thread=f"{thread_id}-x")
    assert [e for e in seen if e["type"] == "status"] == []


# ═══════════════════════════════════════════════════════════════════
# 四、人工确认（弹框）与墙钟的关系
# ═══════════════════════════════════════════════════════════════════


async def test_permission_wait_is_not_counted_by_the_wall_clock(monkeypatch, tmp_path):
    """⚠️ **弹框期间不计时**：人看弹框的时间不是任务时间（现场样本：24 分钟无调用）。"""
    log = tmp_path / "permissions.log"
    monkeypatch.setattr(perm, "PERMISSIONS_LOG", log)
    token = cx.CancelToken(wall_clock=0.05)

    async def slow_allow(_request):
        await asyncio.sleep(0.3)  # 人在看弹框
        return True

    session = perm.Session(mode=perm.MODE_CONFIRM, scope="t", timeout=5.0, approver=slow_allow)
    with cx.bind_cancel(token), perm.bind_session(session):
        await perm.enforce("write_file", {"path": "a.py"})

    assert token.cancelled is False, "确认期间的 0.3 秒不该把 0.05 秒的墙钟走完"
    assert token.paused_seconds() >= 0.25
    assert token.elapsed() < 0.05, f"净任务时长应≈0，实际 {token.elapsed():.3f}s"


async def test_stop_wins_over_a_pending_confirmation(monkeypatch, tmp_path):
    """点「停止」时弹框还开着：按**停止**解栈，而不是留一条"用户拒绝了该工具"。

    为什么这条重要：`web/server.py` 收到 stop 时会把待确认的 future 按"拒绝"收掉
    （否则要等确认超时才有下一个检查点），若 `enforce` 把它当成"用户拒绝"，
    轨迹里就会出现一条误导性的 `denied_by_user`，模型还会据此换别的办法接着干。
    """
    log = tmp_path / "permissions.log"
    monkeypatch.setattr(perm, "PERMISSIONS_LOG", log)
    token = cx.CancelToken()

    async def stop_while_asking(_request):
        token.cancel(cx.REASON_USER)  # 弹框还开着，用户点了「停止」
        return False  # server 收掉弹框时给的就是这个

    session = perm.Session(
        mode=perm.MODE_CONFIRM, scope="t", timeout=5.0, approver=stop_while_asking
    )
    with cx.bind_cancel(token), perm.bind_session(session):
        with pytest.raises(cx.TaskCancelled):
            await perm.enforce("write_file", {"path": "a.py"})

    decisions = []
    if log.exists():
        decisions = [
            json.loads(line)["decision"] for line in log.read_text("utf-8").splitlines() if line
        ]
    assert "denied_by_user" not in decisions, f"不该留一条「用户拒绝」的记录：{decisions}"


def test_sync_permission_path_is_guarded_too(monkeypatch, tmp_path):
    """同步确认通道（CLI 的 input() 走它）同样受停止约束。"""
    monkeypatch.setattr(perm, "PERMISSIONS_LOG", tmp_path / "permissions.log")
    token = cx.CancelToken()

    def sync_stop(_request):
        token.cancel(cx.REASON_USER)
        return False

    session = perm.Session(mode=perm.MODE_CONFIRM, scope="t", sync_approver=sync_stop)
    with cx.bind_cancel(token), perm.bind_session(session):
        with pytest.raises(cx.TaskCancelled):
            perm.enforce_sync("write_file", {"path": "a.py"})


# ═══════════════════════════════════════════════════════════════════
# 五、出边：停止之后不许再打回
# ═══════════════════════════════════════════════════════════════════


def test_edges_end_the_graph_when_cancelled():
    """停止 / 预算击穿 ⇒ 直接结束（既不打回 Executor，也不去验收）。"""
    assert ma.after_executor({"cancelled": True, "route": "complex"}) == "end"
    assert ma.after_executor({"budget_exceeded": True, "route": "complex"}) == "end"
    assert ma.after_executor({"route": "complex"}) == "verifier"
    assert ma.decide_after_verify({"cancelled": True, "retry_count": 0, "verdict": ""}) == "end"
