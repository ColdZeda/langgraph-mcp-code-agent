"""上下文管理测试（阶段 4 · T4.1 外置 / T4.2 压实 / T4.3 预算）。

三个都要有**反向验证**：
- 外置：短的**不能**被外置（否则每次读小文件都白搭一趟往返）；
- 压实：摘要失败时**必须原样返回**（省 token 不能把历史弄丢）；
- 预算：剪枝不能把最近的消息砍掉（当前任务不能丢）。
"""

import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.agent import context as ctx  # noqa: E402

# ── T4.1 工具结果外置 ──


def test_short_result_not_externalized():
    """短结果**原样返回**（连类型都不能变）—— 阈值存在的意义就在这。"""
    text = "短结果" * 10
    assert ctx.externalize_tool_result("execute_powershell_command", text) == text


def test_read_tools_are_exempt_from_externalization():
    """**反向验证（实测换来的）**：`read_file_range` / `read_file` 的输出不外置。

    这两类工具的产物就是模型点名要的那份内容；藏起来会逼它改用"分段读"绕过去 ——
    实测同一道"读全文并总结"的题 token 从 17,361 涨到 127,071（7.3 倍）。
    """
    content = "内容\n" * 5000  # 远超阈值

    assert ctx.should_externalize(content, "read_file_range") is False
    assert ctx.should_externalize(content, "read_file") is False
    assert ctx.externalize_tool_result("read_file_range", content) == content
    assert ctx.externalize_tool_result("read_file", content) == content
    # 对照：过程性输出照旧外置
    assert ctx.should_externalize(content, "execute_powershell_command") is True


def test_long_result_externalized_to_file(tmp_path, monkeypatch):
    """超长结果落盘，context 里只留预览 + 路径。"""
    monkeypatch.setattr(ctx, "TOOL_RESULTS_DIR", tmp_path / "tool_results")
    monkeypatch.setattr(ctx, "RUNTIME_DIR", tmp_path)
    content = "行内容\n" * 3000  # 远超 6000 字符

    out = ctx.externalize_tool_result("execute_powershell_command", content)

    assert out != content
    assert "[工具结果已外置]" in out
    assert "execute_powershell_command" in out
    assert f"{len(content)} 字符" in out
    # 预览只能是前 N 字符，不能把全文又塞回去
    assert len(out) < len(content) / 10

    files = list((tmp_path / "tool_results").glob("*.txt"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == content, "外置文件必须与原文逐字节一致"
    assert files[0].name in out, "context 里必须给出可回读的文件名"


def test_externalized_path_is_readable_by_read_file_range(tmp_path, monkeypatch):
    """验收项：**Agent 需要时能重新读取完整内容** —— 用真正的 read_file_range 读回来。

    这里刻意不 mock：路径写法（相对项目根）、编码、行数格式都要真的能用。
    """
    monkeypatch.setattr(ctx, "TOOL_RESULTS_DIR", tmp_path / "tool_results")
    monkeypatch.setattr(ctx, "RUNTIME_DIR", tmp_path)
    content = "".join(f"第{i}行：内容\n" for i in range(3000))

    out = ctx.externalize_tool_result("execute_powershell_command", content)
    assert "[工具结果已外置]" in out
    stored = next((tmp_path / "tool_results").glob("*.txt"))

    from app.code_agent.mcp_servers.code_tools import read_file_range

    back = read_file_range(str(stored), 1, 5)
    assert "第0行：内容" in back and "第4行：内容" in back


def test_externalize_is_idempotent(tmp_path, monkeypatch):
    """同一份内容重复外置 → 同一个文件（不产生重复副本）。"""
    monkeypatch.setattr(ctx, "TOOL_RESULTS_DIR", tmp_path / "tool_results")
    monkeypatch.setattr(ctx, "RUNTIME_DIR", tmp_path)
    content = "x" * 7000

    first = ctx.externalize_tool_result("execute_powershell_command", content)
    second = ctx.externalize_tool_result("execute_powershell_command", content)

    assert first == second
    assert len(list((tmp_path / "tool_results").glob("*.txt"))) == 1


def test_many_short_lines_externalized_by_line_rule():
    """行数兜底：字符数没超阈值、但行数极多（长清单）也要外置。"""
    short_lines = "\n".join("ab" for _ in range(ctx.EXTERNALIZE_MAX_LINES + 5))
    assert len(short_lines) < ctx.EXTERNALIZE_THRESHOLD, "构造的数据要满足：字符数不超标"
    assert ctx.should_externalize(short_lines, "list_project_structure") is True

    few_lines = "\n".join("ab" for _ in range(10))
    assert ctx.should_externalize(few_lines, "list_project_structure") is False


def test_threshold_is_the_calibrated_value():
    """阈值是**实测校准**的 6000，不是第六版的 2000（那个会把 71 行的文件读取也外置）。"""
    assert ctx.EXTERNALIZE_THRESHOLD == 6000


# ── T4.3 token 估算与预算 ──


def test_estimate_tokens_orders_of_magnitude():
    assert ctx.estimate_tokens("") == 0
    # 中文大致 1 字 1 token
    assert ctx.estimate_tokens("这是一个中文句子") == len("这是一个中文句子")
    # 英文大致 4 字符 1 token
    assert ctx.estimate_tokens("abcdefgh") == 2


def test_estimate_messages_tokens_counts_content():
    msgs = [HumanMessage(content="你好"), AIMessage(content="world")]
    assert ctx.estimate_messages_tokens(msgs) > 0


def test_over_task_budget():
    assert ctx.over_task_budget(100, 200) is False
    assert ctx.over_task_budget(200, 200) is True
    assert ctx.over_task_budget(10**9, 0) is False, "budget<=0 视为不限制"


def test_prune_messages_drops_oldest_tool_result_first(monkeypatch):
    """剪枝优先砍**最老的工具结果**，且最近 keep_last 条绝不被动。"""
    monkeypatch.setattr(ctx, "estimate_messages_tokens", ctx.estimate_messages_tokens)
    msgs = [
        HumanMessage(content="任务"),
        ToolMessage(content="老工具结果" * 500, tool_call_id="1"),
        AIMessage(content="中间结论"),
        ToolMessage(content="新工具结果", tool_call_id="2"),
        AIMessage(content="最终回复"),
    ]
    out = ctx.prune_messages(msgs, budget=1, keep_last=2)

    assert len(out) < len(msgs), "超预算必须真的砍掉东西"
    assert out[-2:] == msgs[-2:], "最近 keep_last 条必须原样保留"
    assert all("老工具结果" not in str(getattr(m, "content", "")) for m in out)


def test_prune_messages_noop_when_under_budget():
    msgs = [HumanMessage(content="短")]
    assert ctx.prune_messages(msgs, budget=100000) == msgs


def test_prune_messages_never_empties_small_list():
    """消息本来就很少时，宁可超预算也不清空（清空等于任务直接没法跑）。"""
    msgs = [HumanMessage(content="长" * 100000)]
    assert len(ctx.prune_messages(msgs, budget=1, keep_last=4)) == 1


# ── T4.2 对话压实 ──


class _FakeChainLLM:
    def __init__(self, content: str = "【目标】做 A\n【约束】用 B\n【已完成】C\n【未决】D"):
        self.content = content
        self.calls = 0

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        return AIMessage(content=self.content)


class _BoomLLM:
    async def ainvoke(self, *a, **k):
        raise RuntimeError("model down")


def _history(n: int) -> list:
    out = []
    for i in range(n):
        out.append(HumanMessage(content=f"第{i}轮任务" * 20))
        out.append(AIMessage(content=f"第{i}轮回复" * 20))
    return out


async def test_compact_noop_when_under_threshold(monkeypatch):
    monkeypatch.setattr(ctx, "COMPACT_THRESHOLD_TOKENS", 10**9)
    llm = _FakeChainLLM()
    msgs = _history(5)

    out, changed = await ctx.compact_history(msgs, [llm])

    assert changed is False
    assert out == msgs
    assert llm.calls == 0, "没超阈值就不该调用模型"


async def test_compact_replaces_old_with_four_section_summary(monkeypatch):
    """超阈值 → 最老的一段被换成四段式摘要，最近若干条逐字保留。"""
    monkeypatch.setattr(ctx, "COMPACT_THRESHOLD_TOKENS", 50)
    monkeypatch.setattr(ctx, "COMPACT_KEEP_MESSAGES", 2)
    llm = _FakeChainLLM("【目标】做 A\n【约束】用 B\n【已完成】C\n【未决】D")
    msgs = _history(6)

    out, changed = await ctx.compact_history(msgs, [llm])

    assert changed is True
    assert llm.calls == 1, "每次压实只该花一次模型调用"
    assert len(out) == 3, "1 条摘要 + 最近 2 条"
    assert out[0].content.startswith(ctx.SUMMARY_MARKER)
    assert "【目标】" in out[0].content and "【约束】" in out[0].content
    assert out[1:] == msgs[-2:], "最近的消息必须逐字保留"


async def test_compact_keeps_first_turn_constraint_in_summary(monkeypatch):
    """验收项：长会话不丢**早期**的目标与约束 —— 摘要的输入里必须含第 1 轮。"""
    monkeypatch.setattr(ctx, "COMPACT_THRESHOLD_TOKENS", 50)
    monkeypatch.setattr(ctx, "COMPACT_KEEP_MESSAGES", 2)

    seen: dict = {}

    class _SpyLLM:
        async def ainvoke(self, messages, **kwargs):
            seen["prompt"] = messages[0].content
            return AIMessage(content="【目标】x\n【约束】不许改技术栈\n【已完成】y\n【未决】z")

    msgs = [HumanMessage(content="【约束】不许改技术栈，也不许引入新前端库")] + _history(6)
    out, changed = await ctx.compact_history(msgs, [_SpyLLM()])

    assert changed is True
    assert "不许改技术栈" in seen["prompt"], "第 1 轮的约束必须进入摘要的输入"
    assert "不许改技术栈" in out[0].content


async def test_compact_failure_returns_history_unchanged(monkeypatch):
    """**反向验证**：摘要失败时宁可保留全部历史（多花 token），也不能丢消息。"""
    monkeypatch.setattr(ctx, "COMPACT_THRESHOLD_TOKENS", 50)
    monkeypatch.setattr(ctx, "COMPACT_KEEP_MESSAGES", 2)
    msgs = _history(6)

    out, changed = await ctx.compact_history(msgs, [_BoomLLM()])

    assert changed is False
    assert out == msgs


async def test_compact_empty_summary_returns_unchanged(monkeypatch):
    monkeypatch.setattr(ctx, "COMPACT_THRESHOLD_TOKENS", 50)
    monkeypatch.setattr(ctx, "COMPACT_KEEP_MESSAGES", 2)
    out, changed = await ctx.compact_history(_history(6), [_FakeChainLLM("   ")])

    assert changed is False


async def test_compact_noop_when_too_few_messages(monkeypatch):
    monkeypatch.setattr(ctx, "COMPACT_THRESHOLD_TOKENS", 1)
    monkeypatch.setattr(ctx, "COMPACT_KEEP_MESSAGES", 8)
    msgs = _history(2)
    out, changed = await ctx.compact_history(msgs, [_FakeChainLLM()])

    assert changed is False and out == msgs


@pytest.mark.parametrize("n", [0, 1])
async def test_compact_handles_empty_history(monkeypatch, n):
    out, changed = await ctx.compact_history(_history(n), [_FakeChainLLM()])
    assert changed is False
