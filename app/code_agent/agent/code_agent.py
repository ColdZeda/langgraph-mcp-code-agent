import asyncio
import json
import os
import sys
import time
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.code_agent.agent.events import bind_sink
from app.code_agent.agent.multi_agent import (
    build_executor_agent,
    build_verifier_agent,
    run_multi_agent,
)
from app.code_agent.config import (
    BROWSER_SERVER_PATH,
    CODE_TOOLS_SERVER_PATH,
    MYSQL_SERVER_PATH,
    PERMISSION_MODE,
    POWERSHELL_SERVER_PATH,
    RAG_SERVER_PATH,
    VM_SERVER_PATH,
    setup_logging,
)
from app.code_agent.security.permissions import (
    HEADLESS_PERMISSION_MODE,
    MODE_OPEN,
    MODE_READONLY,
    AutoApprover,
    PermissionRequest,
    Session,
    bind_session,
    grant_always,
    mode_label,
    normalize_mode,
)
from app.code_agent.tools.file_tools import file_tools
from app.code_agent.utils.mcp import load_mcp_tools
from app.code_agent.utils.tool_cache import ToolCache
from app.code_agent.utils.tool_wrap import wrap_tools

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


def ask_permission_in_terminal(request: PermissionRequest) -> bool:
    """**CLI 的确认通道**（阶段 5 · T5.3）：把"要干什么"打印清楚，然后问一句。

    三个选项，第三个就是 B7 那个勾选框的终端版：
    `y` = 只允许这一次；`a` = **本会话内对该工具总是允许**；其它 = 拒绝。

    ⚠️ **CLI 不做超时**（与 Web 不同，这是有意的）：
    终端里人就在键盘前，不需要超时；而且**异步超时取消不掉 `input()`** ——
    超时后那个线程仍卡在 stdin 上，下一次询问就会有两个线程抢同一份输入。
    所以 CLI 用阻塞式询问，超时机制只作用于 Web（`CODE_AGENT_CONFIRM_TIMEOUT`）。
    """
    print()
    print("─" * 64)
    high = "（🔴 高危操作）" if request.high_risk else ""
    print(f"⏸  需要你确认：{request.tool_name}{high}")
    if request.note:
        print(f"   影响面：{request.note}")
    print(f"   参数：{request.args_brief()}")
    print("─" * 64)
    try:
        answer = input("允许执行吗？ [y]允许 / [a]本会话内总是允许 / 其它=拒绝：").strip().lower()
    except (EOFError, OSError):
        # 读不到 stdin（管道 / 无人值守）→ 按拒绝处理（fail closed）
        print("（读不到输入，按拒绝处理）")
        return False
    if answer in ("a", "always", "总是"):
        grant_always(request.tool_name)
        print(f"已记下：本会话内对 {request.tool_name} 不再询问（切换权限模式 / 换会话后失效）")
        return True
    return answer in ("y", "yes", "是", "允许")


async def _ask_permission_async(request: PermissionRequest) -> bool:
    """异步包装：`input()` 是阻塞的，丢到线程里跑，别卡住事件循环。"""
    return await asyncio.to_thread(ask_permission_in_terminal, request)


async def run_agent(
    thread_id: str = "default",
    debug: bool = False,
    mode: str = "auto",
    permission: str | None = None,
):
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
    # 阶段 4：统一包一层「结果外置 + 只读结果缓存」（缓存作用域 = 本次会话）
    # ⚠️ 必须接返回值：只有同步 `_run` 的工具会被换成代理对象，不是原地改
    cache = ToolCache(scope=f"cli-{thread_id}")
    tools = wrap_tools(tools, cache)
    logger.info(f"共加载 {len(tools)} 个工具")

    executor_agent = build_executor_agent(tools, mode=mode)
    verifier_agent = build_verifier_agent(tools)

    # ── 阶段 5：绑定权限会话（CLI 的确认通道 = 终端提问）──
    permission_mode = normalize_mode(permission or PERMISSION_MODE)
    session = Session(
        mode=permission_mode,
        scope=f"cli-{thread_id}",
        approver=_ask_permission_async,
        sync_approver=ask_permission_in_terminal,  # 同步工具路径也能问（不留旁路）
        timeout=None,  # CLI 不超时，理由见 ask_permission_in_terminal 的 docstring
    )

    logger.info(f"Agent 创建完成（执行模式 {mode} / 权限模式 {permission_mode}），进入对话循环")
    print(
        f"权限模式：{mode_label(permission_mode)}（{'写操作会先问你' if permission_mode == 'confirm' else '见下方说明'}）"
    )
    if permission_mode == MODE_READONLY:
        print("  ⚠️ 只读档：所有写 / 执行类工具会被直接拒绝（不弹确认）。")
    elif permission_mode == MODE_OPEN:
        print("  ⚠️ 放开档：写操作不再逐次确认；**危险命令黑名单仍然生效**。")
    else:
        print("  写 / 执行类工具会先请你确认（高危的会显示影响面）。")

    # 跨轮记忆不再手写：由 checkpointer 按 thread_id 持久化（见 run_multi_agent）
    with bind_session(session):
        await _repl_loop(tools, executor_agent, verifier_agent, thread_id, mode)


async def _repl_loop(tools, executor_agent, verifier_agent, thread_id, mode) -> None:
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
            mode=mode,
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


def main(
    thread_id: str = "default",
    debug: bool = False,
    mode: str = "auto",
    permission: str | None = None,
):
    """CLI 入口。

    ⚠️ 参数必须在这里**逐个收下并转给 `run_agent`** ——
    阶段 3 的 `main.py` 已经在传 `mode=args.mode`，但这个函数当时没加参数，
    导致 `uv run python main.py` **直接 TypeError 崩掉**（阶段 4 发现并修复）。
    阶段 5 加 `permission` 时特意把这条注释留下，别再踩。
    """
    try:
        asyncio.run(run_agent(thread_id=thread_id, debug=debug, mode=mode, permission=permission))
    except KeyboardInterrupt:
        print("\n再见！")
    except Exception:
        logger = setup_logging()
        logger.exception("Agent 异常退出")


# ── Evals 非交互接口 ──

#: 6 个 MCP server 的 (client_id, 入口脚本) 清单（`run_single_task` 与评估 runner 共用）。
MCP_SERVERS: tuple[tuple[str, Any], ...] = (
    ("powershell", POWERSHELL_SERVER_PATH),
    ("rag", RAG_SERVER_PATH),
    ("browser", BROWSER_SERVER_PATH),
    ("vm", VM_SERVER_PATH),
    ("mysql", MYSQL_SERVER_PATH),
    ("code_tools", CODE_TOOLS_SERVER_PATH),
)


async def _load_all_tools() -> list:
    """加载全部 MCP 工具 + 7 个文件工具（**不加包装层**，包装由调用方按任务作用域做）。

    为什么抽出来：评估一轮要跑 60 趟（30 题 × 2 模式），而"起 6 个 server"实测 8~16 秒
    —— 每趟都重来一遍就是十几分钟白等。工具本身**无状态**
    （`get_tools()` 的 docstring：每次工具调用自建并自关一个会话），
    跨任务复用不会串味；每题的隔离靠**独立缓存作用域**与**独立 checkpoint 线程**。
    """
    results = await asyncio.gather(
        *(load_mcp_tools(client_id=cid, server_path=sp) for cid, sp in MCP_SERVERS),
        return_exceptions=True,
    )
    tools: list = []
    for (cid, _sp), res in zip(MCP_SERVERS, results, strict=True):
        if isinstance(res, Exception):
            raise RuntimeError(f"MCP server '{cid}' 加载失败: {type(res).__name__}: {res}") from res
        tools.extend(res)
    tools.extend(file_tools)
    return tools


def _verdict_of(verdict_text: str) -> bool | None:
    """从 Verifier 的结论文本里取 PASS/FAIL。

    ⚠️ **返回 None ≠ False**：single 模式与 auto-simple 路径**根本没有验收环节**，
    把它记成"验收失败"正是阶段 5 修过的那个前端 bug（single 模式误报"验收未通过"）。
    """
    text = (verdict_text or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and parsed.get("verdict"):
            text = str(parsed["verdict"])
    except (ValueError, TypeError):
        pass
    upper = text.upper()
    if "FAIL" in upper:
        return False
    if "PASS" in upper:
        return True
    return None


def _permission_stats(session: Session, approver: Any) -> dict:
    """本次任务的权限痕迹：档位 + 确认闸门被问了几次、批了几次、其中几次是高危。"""
    asked = getattr(approver, "requests", None)
    asked_list = asked if isinstance(asked, list) else []
    return {
        "mode": session.mode,
        "modeLabel": mode_label(session.mode),
        "approver": session.approver_label,
        "asked": len(asked_list),
        "granted": getattr(approver, "granted_count", 0),
        "highRiskAsked": getattr(approver, "high_risk_asked", 0),
        "highRiskTools": sorted({r["tool"] for r in asked_list if r.get("highRisk")}),
    }


def _summarize_node_events(events: list[tuple[float, dict]]) -> dict[str, dict]:
    """把节点事件时间线折成「每个节点花了多少毫秒 / 多少 token / 跑了几次」。

    为什么要折：报告要做**分节点分析**（"哪一段最费 token"）—— 只有整题汇总是看不出来的。
    同一节点会因 Verifier 打回而**跑多次**（executor/verifier），所以是累加 + 计数而不是覆盖。
    """
    open_at: dict[str, float] = {}
    summary: dict[str, dict] = {}
    for ts, ev in events:
        node = str(ev.get("node") or "?")
        status = str(ev.get("status") or "")
        slot = summary.setdefault(node, {"ms": 0.0, "calls": 0, "tokens": 0, "steps": 0})
        if status == "start":
            open_at[node] = ts
        elif status == "end":
            if node in open_at:
                slot["ms"] += (ts - open_at.pop(node)) * 1000
            slot["calls"] += 1
            slot["tokens"] += int(ev.get("tokens") or 0)
            slot["steps"] = max(slot["steps"], int(ev.get("steps") or 0))
    for slot in summary.values():
        slot["ms"] = round(slot["ms"], 1)
    return summary


async def run_single_task(
    task_prompt: str,
    thread_id: str = "eval",
    mode: str = "auto",
    *,
    permission_mode: str | None = None,
    approver: Any | None = None,
    timeout: float | None = None,
    tools: list | None = None,
) -> dict:
    """向多 Agent 架构发送单次任务，返回**完整结果字典**（阶段 6 起；此前是 6 元组）。

    流程：Planner（纯 LLM 规划）→ Executor（ReAct 全量工具执行）→ Verifier（只读验收，
    失败带原因打回，最多 2 轮）。

    返回键：
      - `ok` / `error`：异常**不再伪装成一条"回复"**（旧版把 `[ERROR] ...` 塞进 response，
        评估侧只能靠字符串前缀分辨，很容易把"炸了"记成"答了"）；
      - `response` / `tool_trace` / `conversation` / `step_count` / `token_usage`；
      - `mode` / `route` / `verdict` / `verdict_passed` / `retry_count`（**旧版全被丢掉**，
        导致"假成功诱导应该判 FAIL"这类题根本无法判定）；
      - `budget_exceeded` / `pruned_messages` / `knowledge_injected`；
      - `elapsed_ms` / `node_timings`（分节点耗时与 token）/ `node_events`（原始时间线）；
      - `permission`：本次任务的权限档位与"过了几次确认闸门"。

    `permission_mode` 默认 `HEADLESS_PERMISSION_MODE`（阶段 6 = 「需确认」）；
    `approver` 默认一个 `AutoApprover`（逐条留痕的自动批准器，见 permissions.py）。
    **对抗性题**要传 `AutoApprover(deny_high_risk=True)` 或 `permission_mode="readonly"`。

    `tools`：**已加载好的原始工具列表**（不含包装层）。评估 runner 一轮跑 60 趟，
    只是把 `_load_all_tools()` 的结果传进来复用，就能省掉每趟 8~16 秒的 server 启动；
    传 `None`（默认）时本函数自己加载。
    """
    conversation: list[dict] = [{"role": "user", "content": task_prompt[:2000]}]
    started = time.perf_counter()
    events: list[tuple[float, dict]] = []

    def _collect(event: dict) -> None:
        events.append((time.perf_counter(), event))

    if approver is None:
        approver = AutoApprover()
    perm_mode = normalize_mode(permission_mode or HEADLESS_PERMISSION_MODE)
    permission = Session(
        mode=perm_mode,
        scope=f"eval-{thread_id}",
        approver=approver,
        timeout=timeout,
        approver_label=getattr(approver, "label", "eval_auto"),
    )
    result: dict = {}
    try:
        # ── 加载 MCP 工具（`tools` 传进来就复用，省掉每题 8~16 秒的 server 启动）──
        raw_tools = tools if tools is not None else await _load_all_tools()
        # 阶段 4：包装「结果外置 + 只读结果缓存」。
        # ⚠️ 每个任务一个**独立的缓存作用域** —— 跨任务复用缓存会让
        #    "上一个任务改过的文件"污染"下一个任务的读取"（缓存命中越准，错得越隐蔽）。
        cache = ToolCache(scope=f"eval-{thread_id}-{uuid4().hex[:8]}")
        wrapped = wrap_tools(list(raw_tools), cache)

        executor_agent = build_executor_agent(wrapped, mode=mode)
        verifier_agent = build_verifier_agent(wrapped)
        verifier_agent = build_verifier_agent(tools)

        # ── 阶段 5：无人值守入口**必须显式指定权限档位**；阶段 6 起不再绕开闸门 ──
        # 这里是 `HEADLESS_PERMISSION_MODE`（=「需确认」）+ `AutoApprover`：
        # 每次写操作**照旧逐次过确认闸门**，只是答话的一方是程序，且每条答复都进审计
        # （`allowed_by_eval_auto`）。见 permissions.py 里 `AutoApprover` 的对照表。
        with bind_sink(_collect), bind_session(permission):
            result = await run_multi_agent(
                task_prompt,
                tools,
                executor_agent=executor_agent,
                verifier_agent=verifier_agent,
                thread_id=thread_id,
                mode=mode,
                # ⚠️ 评估必须关掉「自动沉淀」：否则评测过程产生的临时经验会写进知识库，
                #    从而改变后续题目的检索结果（同一批数据前后不可比）。
                auto_deposit=False,
            )
        await cache.aclose()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # 异常时不抛：保留已收集的对话，返回结构化错误（供 evals 定位/记录）
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "response": "",
            "tool_trace": [],
            "conversation": conversation,
            "step_count": 0,
            "token_usage": 0,
            "mode": mode,
            "route": "",
            "verdict": "",
            "verdict_passed": None,
            "retry_count": 0,
            "budget_exceeded": False,
            "pruned_messages": 0,
            "knowledge_injected": [],
            "thread_id": thread_id,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "node_events": [{"t_ms": round((ts - started) * 1000, 1), **ev} for ts, ev in events],
            "node_timings": _summarize_node_events(events),
            "permission": _permission_stats(permission, approver),
        }
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

    verdict_text = str(result.get("verdict") or "")
    parsed_verdict = _verdict_of(verdict_text)
    return {
        "ok": True,
        "error": "",
        "response": result["final_response"],
        "tool_trace": tool_trace,
        "conversation": conversation,
        "step_count": result["step_count"],
        "token_usage": result["token_usage"],
        "mode": result.get("mode", mode),
        "route": result.get("route", ""),  # auto 模式的路由结论（single/multi 模式为空）
        # ── 阶段 6 新增：验收结论不再被丢掉（对抗题"假成功诱导应判 FAIL"要靠它）──
        "verdict": verdict_text,
        "verdict_passed": parsed_verdict,  # True / False / None（single 模式没有验收）
        "retry_count": result.get("retry_count", 0),
        "budget_exceeded": bool(result.get("budget_exceeded")),
        "pruned_messages": result.get("pruned_messages", 0),
        "knowledge_injected": result.get("knowledge_injected", []),
        "thread_id": thread_id,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "node_events": [{"t_ms": round((ts - started) * 1000, 1), **ev} for ts, ev in events],
        "node_timings": _summarize_node_events(events),
        "permission": _permission_stats(permission, approver),
    }


if __name__ == "__main__":
    main()
