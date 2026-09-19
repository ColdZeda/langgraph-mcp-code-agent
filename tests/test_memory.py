"""自动注入 / 自动沉淀测试（阶段 4 · T4.4 ②③）。

两条底线：
- 注入**只在任务开始查一次**，且寒暄/太短的输入不查；
- 沉淀**宁缺毋滥**：模型说没经验就不写；任何失败都只降级，不影响任务结果；
- 沉淀必须**可关闭**（评估时关掉，否则评测经验会污染知识库、改写后续题目的检索）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.agent import memory as mem  # noqa: E402
from app.code_agent.rag import store  # noqa: E402


class _FakeSaveTool:
    def __init__(self, boom: bool = False):
        self.saved: list[dict] = []
        self.boom = boom

    async def ainvoke(self, payload):
        if self.boom:
            raise RuntimeError("写库失败")
        self.saved.append(payload)
        return f"已保存 {payload['title']}"


def _patch_search(monkeypatch, items, boom=False):
    def fake_search(query, **kwargs):
        if boom:
            raise RuntimeError("检索炸了")
        return items

    monkeypatch.setattr(store, "search_knowledge", fake_search, raising=False)
    monkeypatch.setattr(store, "ensure_seeded", lambda: None, raising=False)


# ── ② 自动注入 ──


def test_short_input_not_searched():
    assert mem.is_worth_searching("你好") is False
    assert mem.is_worth_searching("hi") is False
    assert mem.is_worth_searching("查表") is False


def test_normal_input_is_searched():
    assert mem.is_worth_searching("帮我查一下 MySQL 里 agent_test 库有哪些表") is True


def test_inject_disabled_by_config(monkeypatch):
    monkeypatch.setattr(mem, "RAG_AUTO_INJECT", False)
    text, items = mem.inject_relevant_knowledge("帮我查一下 MySQL 里有哪些表")
    assert text == "" and items == []


def test_inject_formats_hits(monkeypatch):
    monkeypatch.setattr(mem, "RAG_AUTO_INJECT", True)
    _patch_search(
        monkeypatch,
        [
            {"id": "real_knowledge/mysql_safety.txt#0", "text": "参数化查询要这样写"},
            {"id": "real_knowledge/wsl_safety.txt#1", "text": "WSL 命令要加超时"},
        ],
    )

    text, items = mem.inject_relevant_knowledge("帮我查一下 MySQL 里有哪些表")

    assert text.startswith(mem.INJECT_HEADER)
    assert "参数化查询要这样写" in text and "WSL 命令要加超时" in text
    assert len(items) == 2


def test_inject_no_hits_returns_empty(monkeypatch):
    monkeypatch.setattr(mem, "RAG_AUTO_INJECT", True)
    _patch_search(monkeypatch, [])
    assert mem.inject_relevant_knowledge("帮我查一下 MySQL 里有哪些表") == ("", [])


def test_inject_failure_degrades_to_no_injection(monkeypatch):
    """检索失败不能让任务跑不起来 —— 只降级为"不注入"。"""
    monkeypatch.setattr(mem, "RAG_AUTO_INJECT", True)
    _patch_search(monkeypatch, [], boom=True)
    assert mem.inject_relevant_knowledge("帮我查一下 MySQL 里有哪些表") == ("", [])


# ── ③ 自动沉淀 ──


class _FakeLLM:
    def __init__(self, content):
        self.content = content


def _patch_llm(monkeypatch, content):
    import app.code_agent.model.llm as llm_mod

    async def fake_invoke(chain, messages):
        return _FakeLLM(content)

    monkeypatch.setattr(llm_mod, "invoke_with_fallback", fake_invoke, raising=False)


async def test_deposit_disabled_does_nothing(monkeypatch):
    tool = _FakeSaveTool()
    _patch_llm(monkeypatch, '{"save": true, "items": [{"title": "t", "content": "c"}]}')

    out = await mem.maybe_deposit_knowledge("任务", "结果", chain=[], save_tool=tool, enabled=False)

    assert out == [] and tool.saved == []


async def test_deposit_defaults_to_config_switch(monkeypatch):
    """不显式传 enabled 时跟随配置（conftest 把测试环境关掉了）。"""
    monkeypatch.setattr(mem, "RAG_AUTO_DEPOSIT", False)
    tool = _FakeSaveTool()
    _patch_llm(monkeypatch, '{"save": true, "items": [{"title": "t", "content": "c"}]}')

    assert await mem.maybe_deposit_knowledge("任务", "结果", chain=[], save_tool=tool) == []
    assert tool.saved == []


async def test_deposit_without_save_tool_skips(monkeypatch):
    _patch_llm(monkeypatch, '{"save": true, "items": [{"title": "t", "content": "c"}]}')
    assert (
        await mem.maybe_deposit_knowledge("任务", "结果", chain=[], save_tool=None, enabled=True)
        == []
    )


async def test_deposit_no_experience_writes_nothing(monkeypatch):
    tool = _FakeSaveTool()
    _patch_llm(monkeypatch, '{"save": false, "items": []}')

    out = await mem.maybe_deposit_knowledge("任务", "结果", chain=[], save_tool=tool, enabled=True)

    assert out == [] and tool.saved == [], "模型说没经验就不该写任何东西"


async def test_deposit_saves_items(monkeypatch):
    tool = _FakeSaveTool()
    _patch_llm(
        monkeypatch,
        '{"save": true, "items": [{"title": "MySQL 端口", "content": "本项目 MySQL 在 3307"}]}',
    )

    out = await mem.maybe_deposit_knowledge("任务", "结果", chain=[], save_tool=tool, enabled=True)

    assert len(out) == 1
    assert tool.saved == [{"title": "MySQL 端口", "content": "本项目 MySQL 在 3307"}]


async def test_deposit_caps_at_two_items(monkeypatch):
    tool = _FakeSaveTool()
    _patch_llm(
        monkeypatch,
        '{"save": true, "items": ['
        '{"title": "a", "content": "1"}, {"title": "b", "content": "2"},'
        '{"title": "c", "content": "3"}]}',
    )

    out = await mem.maybe_deposit_knowledge("任务", "结果", chain=[], save_tool=tool, enabled=True)

    assert len(out) == 2, "最多存 2 条，避免把知识库刷屏"


async def test_deposit_skips_incomplete_items(monkeypatch):
    tool = _FakeSaveTool()
    _patch_llm(
        monkeypatch,
        '{"save": true, "items": [{"title": "", "content": "x"}, {"title": "t", "content": "y"}]}',
    )

    out = await mem.maybe_deposit_knowledge("任务", "结果", chain=[], save_tool=tool, enabled=True)

    assert len(out) == 1 and tool.saved[0]["title"] == "t"


async def test_deposit_bad_json_is_ignored(monkeypatch):
    tool = _FakeSaveTool()
    _patch_llm(monkeypatch, "模型今天不想输出 JSON")

    out = await mem.maybe_deposit_knowledge("任务", "结果", chain=[], save_tool=tool, enabled=True)

    assert out == [] and tool.saved == []


async def test_deposit_llm_failure_degrades(monkeypatch):
    import app.code_agent.model.llm as llm_mod

    async def boom(chain, messages):
        raise RuntimeError("模型挂了")

    monkeypatch.setattr(llm_mod, "invoke_with_fallback", boom, raising=False)
    tool = _FakeSaveTool()

    assert (
        await mem.maybe_deposit_knowledge("任务", "结果", chain=[], save_tool=tool, enabled=True)
        == []
    )
    assert tool.saved == []


async def test_deposit_write_failure_degrades(monkeypatch):
    """单条写失败不该把整个任务搞崩，也不该记进返回列表。"""
    tool = _FakeSaveTool(boom=True)
    _patch_llm(monkeypatch, '{"save": true, "items": [{"title": "t", "content": "c"}]}')

    assert (
        await mem.maybe_deposit_knowledge("任务", "结果", chain=[], save_tool=tool, enabled=True)
        == []
    )


def test_extract_json_handles_code_fence():
    parsed = mem._extract_json('```json\n{"save": true}\n```')
    assert parsed == {"save": True}
    assert mem._extract_json("no json here") is None
