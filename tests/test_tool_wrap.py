"""工具包装层测试（阶段 4 · T4.1 外置 + T4.5 缓存）。

**这个文件的重点是把两条接口形状固定下来**（第六版方案照抄会炸的地方）：

- MCP 工具：`StructuredTool(coroutine=…, response_format="content_and_artifact")`
  → 只有 `coroutine`，**没有 `func`/`_run`**，且返回 **(content, artifact) 二元组**；
- FileManagementToolkit 工具：**只有同步 `_run`**，没有 `coroutine`。

包装器必须两条都能处理，而且**不能把二元组拆坏**
（`content_and_artifact` 的工具返回字符串，LangChain 会直接报错）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.utils import tool_wrap as tw  # noqa: E402
from app.code_agent.utils.tool_cache import ToolCache  # noqa: E402


class _Tool:
    """工具替身。

    ⚠️ 刻意复刻 **pydantic 模型的字段约束**（都是实测踩过的坑）：
      - 只有「声明过的字段」可赋值：MCP 工具的 `StructuredTool` 声明了 `coroutine` 字段
        → 可以原地替换；而 FileManagementToolkit 的工具没声明
        → 赋值会抛 `ValueError: "CopyFileTool" object has no field "coroutine"`（真实报错）；
      - `_run` 在真实工具里是**类方法**，不属于声明字段 → 同样不可赋值。
    """

    def __init__(self, name, *, coroutine=None, run=None, response_format="content"):
        object.__setattr__(self, "_declared", {"name", "response_format"})
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "response_format", response_format)
        if coroutine is not None:
            object.__setattr__(self, "coroutine", coroutine)
            self._declared.add("coroutine")
        if run is not None:
            object.__setattr__(self, "_run", run)

    def __setattr__(self, key, value):
        if key in self._declared:
            object.__setattr__(self, key, value)
        else:
            raise ValueError(f'"{type(self).__name__}" object has no field "{key}"')


@pytest.fixture
def externalize_into(tmp_path, monkeypatch):
    """把外置目录指到 tmp（并让 RUNTIME_DIR 的父目录解析成立）。"""
    import app.code_agent.agent.context as ctx

    monkeypatch.setattr(ctx, "TOOL_RESULTS_DIR", tmp_path / "tool_results")
    monkeypatch.setattr(ctx, "RUNTIME_DIR", tmp_path)
    return tmp_path / "tool_results"


@pytest.fixture
def fake_cache(monkeypatch):
    from tests.test_tool_cache import _FakeRedis

    cache = ToolCache(scope="wrap", enabled=True)
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr(cache, "_get_client", _client)
    cache._fake = fake
    return cache


# ── 返回值形状 ──


def test_split_tool_output_handles_both_shapes():
    text, artifact, is_pair = tw.split_tool_output(("内容", None))
    assert (text, artifact, is_pair) == ("内容", None, True)

    text, artifact, is_pair = tw.split_tool_output("裸字符串")
    assert (text, artifact, is_pair) == ("裸字符串", None, False)


def test_split_tool_output_joins_text_blocks():
    """MCP 的内容可能是文本块列表。"""
    text, _artifact, is_pair = tw.split_tool_output((["a", "b"], None))
    assert text == "a\nb" and is_pair is True


# ── MCP 工具（只有 coroutine，返回二元组）──


async def test_mcp_tool_tuple_shape_preserved(externalize_into):
    async def call_tool(**kwargs):
        return ("短结果", None)

    tool = tw.wrap_tool(
        _Tool("read_file_range", coroutine=call_tool, response_format="content_and_artifact")
    )

    out = await tool.coroutine(path="x")

    assert isinstance(out, tuple) and len(out) == 2, "必须保留 (content, artifact) 二元组"
    assert out == ("短结果", None)


async def test_cache_hit_keeps_tuple_shape(externalize_into, fake_cache):
    """**缓存命中也要返回二元组** —— 否则命中一次 LangChain 就报
    "response_format is content_and_artifact but output is not a tuple"。"""

    async def call_tool(**kwargs):
        return ("短结果", None)

    tool = tw.wrap_tool(
        _Tool("read_file_range", coroutine=call_tool, response_format="content_and_artifact"),
        fake_cache,
    )

    await tool.coroutine(path="a")
    cached = await tool.coroutine(path="a")

    assert cached == ("短结果", None), "命中缓存时形状不能变"
    assert fake_cache.hits == 1


async def test_mcp_tool_long_result_externalized(externalize_into):
    """过程性长输出（这里用 list_project_structure 代表）会被外置。"""
    content = "长内容\n" * 3000

    async def call_tool(**kwargs):
        return (content, None)

    tool = tw.wrap_tool(
        _Tool("list_project_structure", coroutine=call_tool, response_format="content_and_artifact")
    )
    out_text, artifact = await tool.coroutine(path="x")

    assert artifact is None
    assert "[工具结果已外置]" in out_text
    assert len(list(externalize_into.glob("*.txt"))) == 1


async def test_read_tool_not_externalized_through_wrapper(externalize_into):
    """`read_file_range` 的长结果**照原样返回**（豁免名单，实测换来的结论）。"""
    content = "代码行\n" * 5000

    async def call_tool(**kwargs):
        return (content, None)

    tool = tw.wrap_tool(
        _Tool("read_file_range", coroutine=call_tool, response_format="content_and_artifact")
    )
    out_text, _artifact = await tool.coroutine(file_path="app/x.py")

    assert out_text == content, "取内容类工具不能被外置，否则模型只能看到预览"
    assert not list(externalize_into.glob("*.txt"))


async def test_reading_back_externalized_file_is_not_re_externalized(externalize_into):
    """**防死循环**：读 `runtime/tool_results/` 里的文件时不能再外置。

    否则「外置 → 提示去看文件 → 读文件 → 又外置」永远拿不到内容。
    """
    content = "内容\n" * 5000

    async def call_tool(**kwargs):
        return (content, None)

    tool = tw.wrap_tool(
        _Tool("list_project_structure", coroutine=call_tool, response_format="content_and_artifact")
    )
    out_text, _artifact = await tool.coroutine(
        file_path="runtime/tool_results/list_project_structure-abc123.txt"
    )

    assert out_text == content


async def test_mcp_tool_artifact_not_lost(externalize_into):
    """带 artifact（图片等）的结果：artifact 必须原样带回去。"""

    async def call_tool(**kwargs):
        return ("文本", {"image": "base64..."})

    tool = tw.wrap_tool(
        _Tool("read_file_range", coroutine=call_tool, response_format="content_and_artifact")
    )
    text, artifact = await tool.coroutine(path="x")

    assert text == "文本" and artifact == {"image": "base64..."}


# ── 同步工具（只有 _run，且是 pydantic 模型：不能就地加 coroutine）──


async def test_sync_only_tool_is_replaced_by_proxy(externalize_into):
    """FileManagementToolkit 只有 `_run`，且**不允许**就地加 `coroutine` 字段。

    包装器必须返回一个**代理工具**（同名、可协程调用），而不是去改原对象。
    """
    original = _Tool("read_file", run=lambda path: f"内容:{path}")

    wrapped = tw.wrap_tool(original)

    assert wrapped is not original, "同步工具无法就地改造 → 必须换成代理对象"
    assert wrapped.name == "read_file", "名字必须沿用（Verifier 白名单按名字匹配）"
    assert await wrapped.coroutine(path="a") == "内容:a"


async def test_sync_tool_proxy_keeps_sync_entry(externalize_into, monkeypatch):
    """代理工具也要保留同步入口（万一有同步调用方）。

    这里把豁免名单清空，好让"同步路径也会外置"这件事被真正测到。
    """
    import app.code_agent.agent.context as ctx

    monkeypatch.setattr(ctx, "EXTERNALIZE_EXEMPT_TOOLS", set())
    content = "同步长内容\n" * 2000
    wrapped = tw.wrap_tool(_Tool("read_file", run=lambda: content))

    out = wrapped.invoke({})

    assert "[工具结果已外置]" in out


async def test_mcp_tool_is_wrapped_in_place(externalize_into):
    """有 `coroutine` 字段的工具就地替换（返回同一个对象）。"""

    async def call_tool(**kwargs):
        return ("x", None)

    original = _Tool("read_file_range", coroutine=call_tool)
    assert tw.wrap_tool(original) is original


# ── 缓存 ──


async def test_readonly_tool_second_call_hits_cache(externalize_into, fake_cache):
    calls = {"n": 0}

    async def call_tool(**kwargs):
        calls["n"] += 1
        return (f"结果{calls['n']}", None)

    tool = tw.wrap_tool(
        _Tool("read_file_range", coroutine=call_tool, response_format="content_and_artifact"),
        fake_cache,
    )

    first = await tool.coroutine(path="a")
    second = await tool.coroutine(path="a")
    third = await tool.coroutine(path="a", other=1)  # 参数不同 → 不该命中

    assert first == second == ("结果1", None)
    assert calls["n"] == 2, "同参数第 2 次必须走缓存，不同参数照常执行"
    assert third == ("结果2", None)
    assert fake_cache.hits == 1


async def test_non_cacheable_tool_always_executes(externalize_into, fake_cache):
    calls = {"n": 0}

    async def call_tool(**kwargs):
        calls["n"] += 1
        return (f"写结果{calls['n']}", None)

    tool = tw.wrap_tool(_Tool("write_file", coroutine=call_tool), fake_cache)

    await tool.coroutine(path="a")
    await tool.coroutine(path="a")

    assert calls["n"] == 2, "写操作绝不能命中缓存（副作用会被跳过）"
    assert fake_cache.hits == 0


async def test_write_invalidates_cache(externalize_into, fake_cache):
    """**反向验证**：读 → 写 → 再读，第二次读必须**真的重新执行**（不能拿到写之前的旧值）。"""
    value = {"v": "旧"}
    reads = {"n": 0}

    async def read_tool(**kwargs):
        reads["n"] += 1
        return (value["v"], None)

    async def write_tool(**kwargs):
        value["v"] = "新"
        return ("已写入", None)

    read = tw.wrap_tool(_Tool("read_file_range", coroutine=read_tool), fake_cache)
    write = tw.wrap_tool(_Tool("write_file", coroutine=write_tool), fake_cache)

    assert (await read.coroutine(path="a"))[0] == "旧"
    assert (await read.coroutine(path="a"))[0] == "旧"  # 命中缓存
    await write.coroutine(path="a", content="新")
    assert (await read.coroutine(path="a"))[0] == "新", "写之后必须失效缓存"
    assert reads["n"] == 2


async def test_failed_write_still_invalidates(externalize_into, fake_cache):
    """写操作失败也可能留下半成品 → 照样失效缓存。"""

    async def read_tool(**kwargs):
        return ("缓存值", None)

    async def bad_write(**kwargs):
        raise RuntimeError("boom")

    read = tw.wrap_tool(_Tool("read_file_range", coroutine=read_tool), fake_cache)
    write = tw.wrap_tool(_Tool("write_file", coroutine=bad_write), fake_cache)

    await read.coroutine(path="a")
    with pytest.raises(RuntimeError):
        await write.coroutine(path="a")

    assert fake_cache._fake.data == {}, "写失败后缓存必须被清空"


async def test_artifact_results_are_not_cached(externalize_into, fake_cache):
    """带 artifact 的结果不进缓存（缓存里只存得下文本，存了会丢 artifact）。"""
    calls = {"n": 0}

    async def call_tool(**kwargs):
        calls["n"] += 1
        return ("文本", {"img": 1})

    tool = tw.wrap_tool(_Tool("read_file_range", coroutine=call_tool), fake_cache)
    await tool.coroutine(path="a")
    await tool.coroutine(path="a")

    assert calls["n"] == 2
    assert fake_cache._fake.set_calls == 0


async def test_wrap_tools_batch(externalize_into, fake_cache):
    async def call_tool(**kwargs):
        return ("x", None)

    tools = [tw.wrap_tool(_Tool("read_file_range", coroutine=call_tool), fake_cache)]
    assert tw.wrap_tools(tools, fake_cache) == tools


async def test_wrapper_without_cache_still_externalizes(externalize_into):
    async def call_tool(**kwargs):
        return ("长" * 9000, None)

    tool = tw.wrap_tool(_Tool("list_project_structure", coroutine=call_tool), None)
    out = await tool.coroutine()
    assert "[工具结果已外置]" in out[0]


# ── 三条入口都要接上包装层（阶段 4 收尾补漏）──


async def test_web_runtime_wraps_tools(externalize_into, monkeypatch):
    """**Web 入口也必须包装工具**。

    阶段 4 主体只给 CLI（`run_agent`）与 evals（`run_single_task`）接了包装层，
    Web（`app/web/server.py` 的 `AgentRuntime`）漏了 —— 结果是 Web 端既不做结果外置、
    也不走 Redis 缓存，于是"包装层一次覆盖全部工具"这句话在 Web 路径上并不成立。
    """
    from app.web import server as web_server

    async def fake_loader(*, client_id, server_path):
        async def call_tool(**kwargs):
            return ("内容\n" * 3000, None)

        return [
            _Tool(
                "list_project_structure",
                coroutine=call_tool,
                response_format="content_and_artifact",
            )
        ]

    monkeypatch.setattr(web_server, "load_mcp_tools", fake_loader)
    monkeypatch.setattr(web_server, "file_tools", [])
    monkeypatch.setattr(web_server, "build_executor_agent", lambda tools, **kw: None)
    monkeypatch.setattr(web_server, "build_verifier_agent", lambda tools: None)

    runtime = web_server.AgentRuntime()
    await runtime.load()

    assert runtime.tools, "应该加载到工具"
    text, artifact = await runtime.tools[0].coroutine(path="x")
    assert artifact is None
    assert "[工具结果已外置]" in text, "Web 入口的长结果也必须被外置"


async def test_web_runtime_wraps_sync_file_tools(externalize_into, monkeypatch):
    """Web 入口同样要处理「只有同步 `_run`」的文件工具（会被换成代理对象）。"""
    from app.web import server as web_server

    async def fake_loader(*, client_id, server_path):
        return []

    sync_tool = _Tool("read_file", run=lambda file_path: "内容:" + file_path)

    monkeypatch.setattr(web_server, "load_mcp_tools", fake_loader)
    monkeypatch.setattr(web_server, "file_tools", [sync_tool])
    monkeypatch.setattr(web_server, "build_executor_agent", lambda tools, **kw: None)
    monkeypatch.setattr(web_server, "build_verifier_agent", lambda tools: None)

    runtime = web_server.AgentRuntime()
    await runtime.load()

    assert runtime.tools[0] is not sync_tool, "同步工具应被换成代理对象"
    assert runtime.tools[0].name == "read_file"
    assert await runtime.tools[0].coroutine(file_path="a.py") == "内容:a.py"


async def test_eval_entry_verifier_gets_wrapped_tools(externalize_into, monkeypatch):
    """**评估入口给 Verifier 的工具也必须是包装过的**（第三条入口的补漏）。

    来历（已用 git 核实，不是猜的）：
      - `build_verifier_agent(tools)` 来自**改造前基线** `f2f3bbb`；
      - 阶段 6（`946e04c`）重写 `run_single_task` 时，在前面加了一句
        `build_verifier_agent(wrapped)`，**却没删掉旧的那句** —— 后者把结果覆盖了回去；
      - ⇒ Verifier 拿到的是**未包装的原始工具**：权限判定 / 结果外置 / 缓存全部绕过。
    实际安全影响有限（Verifier 只拿只读工具，只读工具在任何档位都是 ALLOW），
    但它违反了「三条入口都要接包装层」这条已经写进文档的不变式。
    """
    from app.code_agent.agent import code_agent as ca

    seen: list[list] = []

    async def fake_loader():
        async def call_tool(**kwargs):
            return ("内容\n" * 3000, None)

        return [
            _Tool(
                "list_project_structure",
                coroutine=call_tool,
                response_format="content_and_artifact",
            )
        ]

    async def fake_run_multi_agent(*args, **kwargs):
        return {
            "plan": "1. 步骤",
            "executor_result": "x",
            "executor_trace": "",
            "executor_trace_list": [],
            "verifier_trace_list": [],
            "executor_messages": [],
            "verifier_messages": [],
            "final_response": "x",
            "verdict": "",
            "verdict_passed": None,
            "retry_count": 0,
            "route": "",
            "mode": "multi",
            "token_usage": 0,
            "step_count": 0,
            "knowledge_injected": [],
            "compacted": False,
            "budget_exceeded": False,
            "pruned_messages": 0,
            "deposited": [],
        }

    monkeypatch.setattr(ca, "_load_all_tools", fake_loader)
    monkeypatch.setattr(ca, "build_executor_agent", lambda tools, **kw: None)
    monkeypatch.setattr(ca, "build_verifier_agent", lambda tools, **kw: seen.append(tools))
    monkeypatch.setattr(ca, "run_multi_agent", fake_run_multi_agent)

    await ca.run_single_task("任务")

    assert seen, "Verifier 没被构建"
    verifier_tools = seen[-1]
    assert verifier_tools, "Verifier 应该有工具"
    text, artifact = await verifier_tools[0].coroutine(path="x")
    assert artifact is None
    assert "[工具结果已外置]" in text, "Verifier 的工具没走包装层（结果外置没生效）"
