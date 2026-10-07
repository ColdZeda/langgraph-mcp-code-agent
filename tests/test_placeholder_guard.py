"""阶段 8 · P1.5：**"模板没替换"这一类**的识别与拦截（候选池 §十五B）。

三件事要守：

| 要守的事 | 怎么测 |
|---|---|
| 命中 ⇒ **只回问、不进图**（0 次模型调用 / 0 次工具调用） | 数假 Executor 的 `astream` 调用次数 |
| 🔴 **不误伤真任务** | 30 道评估题的 prompt 全过一遍 + 一串真实形状的正反例（HTML / JS 模板串 / Jinja / 泛型 / JSON） |
| 工具层的**路径类参数**同样拦（但正文参数不拦） | 包装工具直接调，断言"没执行"与"内容里的尖括号放行" |

⚠️ 两道**真会被误伤的评估题**（写守卫前先扫出来的，必须一直是绿的）：
  · `E016`：`GET /health 返回 {"status": "ok"}` —— 单花括号是 JSON 例子；
  · `E029`：`每个文件里有一行 TOTAL=<数字>` —— `<数字>` 是文件内容的**描述**，不是占位符。
"""

import sys
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.agent import multi_agent as ma  # noqa: E402
from app.code_agent.utils import placeholder_guard as guard  # noqa: E402
from app.code_agent.utils import tool_wrap as tw  # noqa: E402
from tests.test_cancel import _CountingAgent  # noqa: E402
from tests.test_tool_wrap import _Tool  # noqa: E402

# ═══════════════════════════════════════════════════════════════════
# 一、判据本身（正例 / 反例）
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("把文件传到 <你的WSL用户名>/nginx/uploads/qa/", ["<你的WSL用户名>"]),
        ("把文件传到 <YOUR_WSL_USER>/nginx/uploads/", ["<YOUR_WSL_USER>"]),
        ("把文件放到 ${WORKSPACE_DIR}/out 下", ["${WORKSPACE_DIR}"]),
        ("用我的API密钥 ${你的密钥} 调一次接口", ["我的API密钥", "${你的密钥}"]),
        ("把结果写到 {{你的项目路径}}/out.txt", ["{{你的项目路径}}"]),
    ],
)
def test_placeholder_shapes_are_detected(text, expected):
    assert guard.find_placeholders(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        'GET /health 返回 {"status": "ok"}',  # ← E016 的真实文本
        "每个文件里有一行 TOTAL=<数字>，请加起来",  # ← E029 的真实文本
        "写一个 HTML 文件，包含 <h1>你好</h1>",
        "用 JS 模板字符串 ${name} 输出到控制台",
        "写一个 Jinja 模板 {{ name }}",
        "你的代码里有个 bug，帮我看看",
        "用泛型 <T> 写个函数，再用 <K, V> 做映射",
        '把 <div class="x"> 改成 <span>',
    ],
)
def test_normal_code_tasks_are_not_flagged(text):
    """**反例同样重要**：拦错比不拦更糟（用户会以为项目坏了）。"""
    assert guard.find_placeholders(text) == []


def test_every_eval_task_prompt_passes_the_guard():
    """🔴 **护栏**：30 道评估题的任务文本都不能被入口守卫拦下。

    以后谁写了一道带**真占位符**的题，这里会当场变红 —— 而不是等评估跑完才发现
    "有几道题根本没进图"。这是"30/30 不会被新守卫打挂"的机械保证。
    """
    from evals.tasks import TASKS

    offenders = [
        (t.id, guard.find_placeholders(t.prompt))
        for t in TASKS
        if guard.find_placeholders(t.prompt)
    ]
    assert offenders == [], f"这些评估题会被入口守卫拦下：{offenders}"


def test_literal_hint_is_an_escape_hatch():
    """明确说"按字面处理"就放行 —— 守卫不许把用户卡死（§十五A：绝不卡住不动）。"""
    assert guard.is_explicitly_literal("把 <你的用户名> 原样写进文件，按字面处理")
    assert not guard.is_explicitly_literal("把文件传到 <你的用户名>/uploads/")


# ═══════════════════════════════════════════════════════════════════
# 二、入口：只回问、不进图
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def thread_id(monkeypatch, tmp_path):
    monkeypatch.setattr(ma, "CHECKPOINT_DB", tmp_path / "checkpoints.db")
    return f"clarify-{uuid4().hex[:8]}"


async def test_entry_clarifies_without_running_anything(thread_id):
    """命中占位符 ⇒ **0 次模型调用、0 次工具调用**，只回一段带默认建议的问话。"""
    executor = _CountingAgent()
    verifier = _CountingAgent()

    result = await ma.run_multi_agent(
        "把 runtime/workspace 里的文件传到 <你的WSL用户名>/nginx/uploads/qa/",
        [],
        executor_agent=executor,
        verifier_agent=verifier,
        thread_id=thread_id,
        mode="multi",
        auto_inject=False,
        auto_deposit=False,
    )

    assert executor.calls == 0, "命中占位符就该不进图（0 次模型调用）"
    assert verifier.calls == 0
    assert result["needs_clarification"] is True
    assert result["placeholders"] == ["<你的WSL用户名>"]
    assert result["token_usage"] == 0
    assert result["step_count"] == 0
    assert result["executor_trace_list"] == []
    body = result["final_response"]
    assert "需要你先确认" in body
    assert "<你的WSL用户名>" in body, "要把命中的占位符原样指给用户看"
    assert "按字面处理" in body, "要给出路（提问必须带默认建议）"


async def test_clarification_is_written_into_the_thread_memory(thread_id, monkeypatch):
    """这一轮 (任务, 回问) 要写回线程记忆 —— 否则下一轮看不到"我问过什么"。"""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    await ma.run_multi_agent(
        "把文件传到 <YOUR_WSL_USER>/uploads/",
        [],
        executor_agent=_CountingAgent(),
        verifier_agent=_CountingAgent(),
        thread_id=thread_id,
        auto_inject=False,
        auto_deposit=False,
    )

    async with AsyncSqliteSaver.from_conn_string(str(ma.CHECKPOINT_DB)) as saver:
        tup = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
    messages = (tup.checkpoint.get("channel_values") or {}).get("messages") or []
    texts = [m.content for m in messages if isinstance(m.content, str)]
    assert any("<YOUR_WSL_USER>" in t for t in texts), "用户那句没进记忆"
    assert any("需要你先确认" in t for t in texts), "回问没进记忆"


async def test_literal_escape_hatch_actually_runs(thread_id):
    """带「按字面处理」时不拦 —— 照常进图（假 Executor 被调用一次）。"""
    executor = _CountingAgent()
    result = await ma.run_multi_agent(
        "把 <你的用户名> 原样写进 readme.txt，按字面处理这个占位符",
        [],
        executor_agent=executor,
        verifier_agent=_CountingAgent(),
        thread_id=thread_id,
        mode="single",
        auto_inject=False,
        auto_deposit=False,
    )

    assert executor.calls == 1, "明确说了按字面处理，就该正常跑"
    assert not result.get("needs_clarification")


# ═══════════════════════════════════════════════════════════════════
# 三、工具层：路径类参数拦、正文参数不拦
# ═══════════════════════════════════════════════════════════════════


def _fake_tool(tmp_path, calls):
    async def _coro(**kwargs):
        calls.append(kwargs)
        (tmp_path / str(kwargs.get("file_path", "x")).split("/")[-1]).write_text(
            "x", encoding="utf-8"
        )
        return "ok"

    return tw.wrap_tool(_Tool("write_file", coroutine=_coro))


async def test_tool_layer_rejects_a_placeholder_path(tmp_path):
    """路径参数里是占位符 ⇒ **直接拒、不执行**（异常是普通 Exception ⇒ ToolNode 会交给模型自己改）。"""
    calls: list[dict] = []
    tool = _fake_tool(tmp_path, calls)

    with pytest.raises(guard.PlaceholderArgError) as excinfo:
        await tool.coroutine(file_path="<你的路径>/a.txt", text="x")

    assert calls == [], "参数是占位符时绝不能执行"
    assert "没替换的模板占位符" in str(excinfo.value)
    assert "file_path" in str(excinfo.value), "要点名是哪个参数"
    assert isinstance(excinfo.value, Exception), "必须是普通 Exception（让模型能收到 ToolMessage）"


async def test_tool_layer_lets_content_args_through(tmp_path):
    """**正文参数不拦**：写一个含 `<h1>` 的文件是正当需求。"""
    calls: list[dict] = []
    tool = _fake_tool(tmp_path, calls)

    assert await tool.coroutine(file_path="index.html", text="<h1>标题</h1>") == "ok"
    assert len(calls) == 1


@pytest.mark.parametrize("arg_name", ["file_path", "dir_path", "vm_dest_dir", "root_dir", "target"])
def test_vm_style_path_args_are_covered(arg_name):
    """VM / 文件工具的路径参数都要覆盖（`vm_dest_dir` / `dir_path` 这些名字也得算"路径类"）。"""
    with pytest.raises(guard.PlaceholderArgError):
        guard.check_tool_args("upload_directory_to_vm", {arg_name: "${DEST_DIR}/uploads"})
