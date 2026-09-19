import asyncio
import os
import sys
import time

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.code_agent.agent.multi_agent import (
    build_executor_agent,
    build_verifier_agent,
    run_multi_agent,
)
from app.code_agent.config import (
    BROWSER_SERVER_PATH,
    CODE_TOOLS_SERVER_PATH,
    MYSQL_SERVER_PATH,
    POWERSHELL_SERVER_PATH,
    RAG_SERVER_PATH,
    VM_SERVER_PATH,
    setup_logging,
)
from app.code_agent.tools.file_tools import file_tools
from app.code_agent.utils.mcp import load_mcp_tools

# 统一 stdio 为 UTF-8（Windows 控制台默认是 GBK）。
# 做能力判断而不是直接调用：pytest 会把 sys.stdin 换成没有 reconfigure 的替身，
# 直接调用会让本模块无法被测试/被当库导入。
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def format_debug_output(step_name: str, content: str, is_tool_call: bool = False) -> None:
    if is_tool_call:
        print(f"✂️ [工具调用] {step_name}")
        print("-" * 40)
        print(content.strip())
    else:
        print(f"🤔 [{step_name}]")
        print("-" * 40)
        print(content.strip())
        print("-" * 40)


async def run_agent(thread_id: str = "default", debug: bool = False):
    if debug:
        os.environ["LOG_LEVEL"] = "DEBUG"
    logger = setup_logging()
    logger.info("Agent 启动中...")

    logger.info("加载 MCP 工具...")
    (*tool_sets,) = await asyncio.gather(
        load_mcp_tools(client_id="powershell", server_path=POWERSHELL_SERVER_PATH),
        load_mcp_tools(client_id="rag", server_path=RAG_SERVER_PATH),
        load_mcp_tools(client_id="browser", server_path=BROWSER_SERVER_PATH),
        load_mcp_tools(client_id="vm", server_path=VM_SERVER_PATH),
        load_mcp_tools(client_id="mysql", server_path=MYSQL_SERVER_PATH),
        load_mcp_tools(client_id="code_tools", server_path=CODE_TOOLS_SERVER_PATH),
    )
    tools = [t for tool_set in tool_sets for t in tool_set]
    tools.extend(file_tools)
    logger.info(f"共加载 {len(tools)} 个工具")

    executor_agent = build_executor_agent(tools)
    verifier_agent = build_verifier_agent(tools)

    logger.info("多 Agent（Planner→Executor→Verifier）创建完成，进入对话循环")

    # 跨轮记忆不再手写：由 checkpointer 按 thread_id 持久化（见 run_multi_agent）
    while True:
        user_input = input("用户: ")

        if user_input.lower() in ("exit", "quit", "q", "退出", "bye"):
            break

        print("\n🤖 多 Agent 协作中（Planner → Executor → Verifier）...")
        print("=" * 60)
        start_time = time.time()

        result = await run_multi_agent(
            user_input,
            tools,
            executor_agent=executor_agent,
            verifier_agent=verifier_agent,
            thread_id=thread_id,
        )

        elapsed = time.time() - start_time
        print(f"\n📝 [Planner] 计划（{elapsed:.1f}s）：")
        print("-" * 30)
        print(result["plan"])

        print("\n🔍 [Verifier] 验收结果：")
        print("-" * 30)
        print(result["verdict"])

        print("\n🤖 [Executor] 最终回复：")
        print("-" * 30)
        print(result["final_response"])
        print("=" * 60)

        print()


def main(thread_id: str = "default", debug: bool = False):
    try:
        asyncio.run(run_agent(thread_id=thread_id, debug=debug))
    except KeyboardInterrupt:
        print("\n再见！")
    except Exception:
        logger = setup_logging()
        logger.exception("Agent 异常退出")


# ── Evals 非交互接口 ──


async def run_single_task(
    task_prompt: str, thread_id: str = "eval"
) -> tuple[str, list[dict], list[dict], int, int]:
    """向多 Agent 架构发送单次任务，返回 (最终回复, 工具调用trace, 对话存档, 步数, token用量)。

    流程：Planner（纯 LLM 规划）→ Executor（ReAct 全量工具执行）→ Verifier（只读验收，
    失败带原因打回，最多 2 轮）。
    - trace 结构: [{name, args, result}, ...]（工具调用摘要，result 截断 500 字符）
    - conversation 结构: 明文对话存档（人可读，用于失败题定位）
    """
    conversation: list[dict] = [{"role": "user", "content": task_prompt[:2000]}]

    _clients: list = []  # MCP client 生命周期管理（用完必须关闭，否则子进程累积）
    try:
        # ── 加载 MCP 工具（return_exceptions=True：单个 server 失败不整体崩溃，便于定位）──
        _MCP_SERVERS = [
            ("powershell", POWERSHELL_SERVER_PATH),
            ("rag", RAG_SERVER_PATH),
            ("browser", BROWSER_SERVER_PATH),
            ("vm", VM_SERVER_PATH),
            ("mysql", MYSQL_SERVER_PATH),
            ("code_tools", CODE_TOOLS_SERVER_PATH),
        ]
        _results = await asyncio.gather(
            *(load_mcp_tools(client_id=cid, server_path=sp) for cid, sp in _MCP_SERVERS),
            return_exceptions=True,
        )
        tool_sets = []
        for (cid, _sp), res in zip(_MCP_SERVERS, _results, strict=True):
            if isinstance(res, Exception):
                raise RuntimeError(
                    f"MCP server '{cid}' 加载失败: {type(res).__name__}: {res}"
                ) from res
            tool_sets.append(res)
        tools = [t for tool_set in tool_sets for t in tool_set]
        tools.extend(file_tools)

        executor_agent = build_executor_agent(tools)
        verifier_agent = build_verifier_agent(tools)

        result = await run_multi_agent(
            task_prompt,
            tools,
            executor_agent=executor_agent,
            verifier_agent=verifier_agent,
            thread_id=thread_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # 异常时不抛：保留已收集的对话，返回错误信息（供 evals 定位）
        err = f"[ERROR] {type(e).__name__}: {e}"
        return err, [], conversation, 0, 0
    # ⚠️ 这里**故意没有**"关闭 MCP client"的清理块 —— 实测（langchain-mcp-adapters 0.1.1）：
    #   1) `MultiServerMCPClient.get_tools()` 是"每次工具调用自建一个会话"（其 docstring 明写
    #      "a new session will be created for each tool call"），用完即关（stdio 子进程同理）
    #      → **没有长期存活的 client/子进程需要清理**；
    #   2) `MultiServerMCPClient.__aexit__` 是普通函数，调用即抛 NotImplementedError
    #      （该适配器不支持当上下文管理器）→ 原先的 `await _client.__aexit__(...)`
    #      只是被 `except Exception` 吞掉的空操作，写着反而误导。
    #   若将来升级适配器、改为长连接会话，再按新 API 补清理。

    # ── 组装 conversation（明文，人可读）──
    conversation.append({"role": "planner", "content": result["plan"][:2000]})
    for msg in result["executor_messages"]:
        if isinstance(msg, HumanMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            conversation.append({"role": "user", "content": content[:2000]})
        elif isinstance(msg, AIMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            entry: dict = {"role": "ai", "content": content}
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:
                entry["tool_calls"] = [
                    {"name": tc.get("name", "unknown"), "args": tc.get("args", {})}
                    for tc in tool_calls
                ]
            conversation.append(entry)
        elif isinstance(msg, ToolMessage):
            name = getattr(msg, "name", "unknown")
            content = str(msg.content)
            conversation.append({"role": "tool", "name": name, "content": content[:5000]})
    for msg in result["verifier_messages"]:
        if isinstance(msg, AIMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            conversation.append({"role": "verifier", "content": content[:2000]})

    # ── 组装 tool_trace（仅 Executor 的工具调用；Verifier 的只读核实不计入，
    #    避免 tool_call_count 等 verifier 口径被验收环节干扰）──
    tool_trace: list[dict] = []
    for tc in result["executor_trace_list"]:
        tool_trace.append({"name": tc["name"], "args": tc["args"], "result": ""})
    # 从 conversation 中的 tool 消息回填 result
    for m in conversation:
        if m.get("role") == "tool":
            for t in reversed(tool_trace):
                if t["name"] == m.get("name") and not t.get("result"):
                    t["result"] = str(m.get("content", ""))[:500]
                    break

    return (
        result["final_response"],
        tool_trace,
        conversation,
        result["step_count"],
        result["token_usage"],
    )


if __name__ == "__main__":
    main()
