"""MCP 工具的生命周期事实 + run_single_task 冒烟测试。

## 为什么有这个文件（实测校正）

第六版/第七版方案里 T1.8 的前提是"`run_single_task` 超时取消后会漏关 6 个 MCP 子进程"，
并据此要求"修好 client 清理"。**实测把这个前提推翻了**：

1. `langchain-mcp-adapters 0.1.1` 的 `MultiServerMCPClient.get_tools()` 文档字符串写着
   "**NOTE: a new session will be created for each tool call**" ——
   它**每次工具调用自建并自关**一个会话（stdio 子进程同理）→ **没有长期存活的 client 需要关闭**；
2. `MultiServerMCPClient.__aexit__` 是**普通函数**（调用即抛 `NotImplementedError`），
   该适配器明确不支持把 client 当上下文管理器用。

所以 `code_agent.py` 里那段 `await _client.__aexit__(...)` **一直是空操作**
（异常被 `except Exception` 静默吞掉），而"修"它反而会让 evals 路径直接崩。

下面的测试把这两条事实固化下来：将来升级适配器改变了行为，这里会先失败提醒。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.agent import code_agent as ca


def test_adapter_client_cannot_be_used_as_context_manager():
    """固化事实：MultiServerMCPClient 不支持 async with / __aexit__（0.1.x）。"""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient({})
    with pytest.raises(NotImplementedError):
        client.__aexit__(None, None, None)


async def test_run_single_task_does_not_need_client_cleanup(monkeypatch):
    """`run_single_task` 不依赖任何 client 清理 API，应正常返回结果。"""

    async def fake_loader(*, client_id, server_path):
        return []  # 工具列表（0.1.x 下无需保留 client）

    async def fake_run_multi_agent(*args, **kwargs):
        assert kwargs.get("thread_id") == "probe", "evals 路径必须把 thread_id 传下去"
        return {
            "plan": "{}",
            "executor_result": "已完成",
            "executor_trace": "",
            "verdict": '{"verdict": "PASS"}',
            "retry_count": 0,
            "token_usage": 7,
            "executor_trace_list": [],
            "verifier_trace_list": [],
            "executor_messages": [],
            "verifier_messages": [],
            "step_count": 1,
            "final_response": "已完成",
        }

    monkeypatch.setattr(ca, "load_mcp_tools", fake_loader)
    monkeypatch.setattr(ca, "run_multi_agent", fake_run_multi_agent)
    monkeypatch.setattr(ca, "build_executor_agent", lambda tools, **kw: None)
    monkeypatch.setattr(ca, "build_verifier_agent", lambda tools: None)

    result = await ca.run_single_task("任务", thread_id="probe")

    assert result["response"] == "已完成"
    assert result["ok"] is True, f"不应因清理/适配器问题失败：{result.get('error')}"
    assert result["token_usage"] == 7
    assert result["step_count"] == 1
    # 阶段 6：验收结论必须**带出来**（旧版 6 元组把它丢掉了 → 对抗题无法判定）
    assert result["verdict_passed"] is True
    assert result["retry_count"] == 0
    # 权限痕迹：档位必须是「需确认」+ 逐条留痕的自动批准器（阶段 6 决策）
    assert result["permission"]["mode"] == "confirm"
    assert result["permission"]["approver"] == "eval_auto"


def test_rag_server_imports_native_extensions_at_module_level():
    """**阶段 5 订正 #27 的回归守卫**：`rag.py` 必须在 **import 阶段**就把重库导进来。

    背景（实测，不是推测）：如果等到**工具被调用时**才惰性加载 `sentence_transformers`
    （→ sklearn → scipy 的扩展模块），会卡在 **Windows DLL 加载**上死锁；
    表现是"`query_rag` 永远不返回、**但副作用已经发生**（文件和向量都写好了）"——
    Claude 那边看到的是"前端一直转圈、且不烧 token"。

    ⚠️ 为什么用**源码检查**而不是 `import app.code_agent.rag.rag`：
    后者会真的把 torch 拉起来（8 秒上下），给整套单测平白加一大截耗时。
    这里只要守住"这一行还在模块顶层"——有人把它当"冗余 import"删掉时，这条会红。
    """
    import ast

    source = (Path(__file__).resolve().parents[1] / "app/code_agent/rag/rag.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    module_level = {
        alias.name
        for node in tree.body  # 只看模块顶层语句
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    assert "sentence_transformers" in module_level, (
        "rag.py 顶层少了 `import sentence_transformers` —— 别删它，"
        "删了 RAG 工具会在 MCP 子进程里死锁（见订正 #27 与 rag.py 里的注释）"
    )
