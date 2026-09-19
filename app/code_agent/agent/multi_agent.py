"""多 Agent 架构 — Planner → Executor → Verifier 三阶段协作图（D 路径）。

将原单 Agent 的 Plan→Execute→Verify 从 Prompt 软约束升级为 StateGraph 硬流程：
- Planner：纯 LLM（无工具），产出结构化计划 JSON（含 verify_tools 声明）
- Executor：复用 create_react_agent（全量工具），按计划执行
- Verifier：只读工具白名单（按计划动态挂载子集），对照「需求 + 计划 + 执行轨迹」验收
- 打回机制：Verifier FAIL 时带原因打回 Executor 重做，最多 2 轮
"""
from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.code_agent.agent.prompts import PROMPT_CONTEXT, SYSTEM_PROMPT_TEMPLATE
from app.code_agent.config import CHECKPOINT_DB
from app.code_agent.model.llm import get_llm

# ═══════════════════════════════════════════════════════════════════
# Verifier 只读工具白名单（代码级强制：不在名单内的工具绝不会挂载给 Verifier）
# ═══════════════════════════════════════════════════════════════════

READONLY_TOOL_NAMES: set[str] = {
    # code_tools（MCP）只读
    "read_file_range", "generate_diff", "analyze_ast", "list_project_structure",
    # MySQL 只读（查询/结构，无写权限）
    "mysql_list_databases", "mysql_list_tables", "mysql_describe_tables", "mysql_execute_query",
    # RAG 查询
    "query_rag",
    # FileManagementToolkit 读/查看类
    "read_file", "list_directory", "file_search",
}

MAX_RETRY = 2  # Verifier 打回上限


# ═══════════════════════════════════════════════════════════════════
# 图状态
# ═══════════════════════════════════════════════════════════════════

class AgentState(TypedDict):
    user_input: str          # 原始用户需求
    plan: str                # Planner 产出（JSON 字符串）
    executor_result: str     # Executor 最终回复
    executor_trace: str      # Executor 工具调用摘要（供 Verifier 参考）
    verdict: str             # Verifier 判定（JSON：verdict/reason）
    retry_count: int         # 已打回次数
    token_usage: int         # 全流程 token 累计
    # evals 存档用（结构化输出）
    executor_trace_list: list[dict]   # Executor 工具调用 trace [{name, args}]
    verifier_trace_list: list[dict]   # Verifier 工具调用 trace
    executor_messages: list           # Executor 消息流（组装对话存档用）
    verifier_messages: list           # Verifier 消息流
    step_count: int                   # Executor 执行步数（近似原单 Agent 步数）
    # ── 跨轮记忆（唯一的会话通道）──
    # ⚠️ 必须带 `add_messages` reducer：没 reducer 的通道是"新值覆盖旧值"，
    #    那样即使接了 checkpointer，按 thread_id 也恢复不出对话。
    #    每轮由 run_multi_agent 在图跑完后追加一对 (任务, 最终回复)。
    messages: Annotated[list[AnyMessage], add_messages]


# ═══════════════════════════════════════════════════════════════════
# Planner / Verifier 提示词
# ═══════════════════════════════════════════════════════════════════

PLANNER_PROMPT = """你是任务规划员（Planner）。你的职责是理解用户需求并制定清晰、可执行的步骤计划。
你【不执行任何操作】，只输出计划。

用户需求：
{user_input}

{retry_context}

输出要求（严格 JSON，不要输出任何其他内容）：
{{
  "goal": "任务目标（一句话概括）",
  "steps": ["步骤1：...", "步骤2：...", "步骤3：..."],
  "verify_tools": ["建议验收员使用的只读工具名，如 read_file_range / mysql_list_tables / generate_diff / query_rag"]
}}

注意：
- steps 要具体到可执行动作（读哪个文件、创建什么、查什么库），不要空泛。
- 简单查询/搜索/查库类任务（搜索关键词、查表、读文件后汇报）：直接调用对应工具并汇报结果即可，
  【不要创建中间文件、不要写解析脚本、不要执行多余命令】，除非用户明确要求保存/处理。
- verify_tools 只填只读工具，按任务类型从以下候选里选 1-5 个：
  read_file_range, generate_diff, analyze_ast, list_project_structure,
  mysql_list_databases, mysql_list_tables, mysql_describe_tables, mysql_execute_query,
  query_rag, read_file, list_directory, file_search"""

VERIFIER_PROMPT = """你是任务验收员（Verifier）。你的职责是对照「用户需求」和「执行计划」，检查「执行结果」是否真正满足要求。
你可以使用只读工具核实（如读取文件确认修改、查库确认数据），但【绝不能修改任何内容】。

用户需求：
{user_input}

执行计划：
{plan}

执行结果（Executor 的最终回复）：
{executor_result}

执行过程中的工具调用轨迹：
{executor_trace}

验收要点：
1. 用户要求的事情是否真的做完了（不是只走了流程）？
2. 结果是否正确（可以读文件/查库核实）？
3. 有没有遗漏步骤、或做了计划外不该做的事？
4. 简单查询/搜索/查库类任务：执行结果已包含明确答案时，直接判断通过，
   【不要调用工具重复核实】（除非结果缺失、明显可疑或涉及关键产物验证）。

最后必须输出严格 JSON（不要输出其他内容）：
{{"verdict": "PASS" 或 "FAIL", "reason": "结论依据；FAIL 时必须给出具体、可执行的修正意见"}}"""


# ═══════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════

def _extract_json(text: str) -> dict | None:
    """从 LLM 输出中提取 JSON 对象（容错：支持 ```json 包裹 / 前后杂文本）。"""
    if not text:
        return None
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _msg_tokens(msg: Any) -> int:
    usage = getattr(msg, "usage_metadata", None)
    return int(usage.get("total_tokens") or 0) if usage else 0


def _plan_to_text(plan: str) -> str:
    obj = _extract_json(plan)
    if obj and obj.get("steps"):
        return "\n".join(f"{i+1}. {s}" for i, s in enumerate(obj["steps"]))
    return plan or "（无计划）"


# ═══════════════════════════════════════════════════════════════════
# 节点实现
# ═══════════════════════════════════════════════════════════════════

async def planner_node(state: AgentState) -> dict:
    """纯 LLM 规划，产出结构化计划。"""
    retry_context = ""
    if state["retry_count"] > 0:
        retry_context = (
            "（注意：上一轮执行未通过验收，验收意见如下，请重新规划修正方案：\n"
            f"{state['verdict']}\n）"
        )
    prompt = PLANNER_PROMPT.format(user_input=state["user_input"], retry_context=retry_context)
    resp = await get_llm().ainvoke([SystemMessage(content="你是规划员。"), HumanMessage(content=prompt)])
    plan_text = resp.content if isinstance(resp.content, str) else str(resp.content)
    parsed = _extract_json(plan_text)
    if parsed and parsed.get("steps"):
        plan_text = json.dumps(parsed, ensure_ascii=False, indent=2)
    return {"plan": plan_text, "token_usage": state.get("token_usage", 0) + _msg_tokens(resp)}


def _trace_to_text(trace: list[dict]) -> str:
    lines = []
    for t in trace[-15:]:
        args = json.dumps(t["args"], ensure_ascii=False)[:120]
        lines.append(f"- {t['name']}({args})")
    return "\n".join(lines) if lines else "（无工具调用）"


async def executor_node(state: AgentState, executor_agent: Any) -> dict:
    """复用 create_react_agent 按计划执行。

    ⚠️ **重试计数在这里自增**（不是在 verifier_node 里）：
    `retry_count` 的语义是「**已经打回了多少次**」——只有真正发起一次重跑时才 +1。
    这样 `MAX_RETRY = 2` 恰好等于"最多打回 2 轮"
    = Executor 一共执行 `MAX_RETRY + 1` 次（首次 + 2 次重跑），
    且 `run_multi_agent` 里"已重试 N 次"的文案也准确。

    （若改成在 verifier 里自增，第 1 次 FAIL 就会被当成"已打回 1 次"，
    实际只会有 1 次重跑，与「最多打回 2 轮」不符。）
    """
    plan_steps = _plan_to_text(state.get("plan", ""))
    prev_verdict = str(state.get("verdict") or "")
    is_retry = "FAIL" in prev_verdict.upper()      # 上一轮验收失败 → 本次是重跑
    if is_retry:
        user_msg = (
            f"【任务】{state['user_input']}\n"
            f"【执行计划】\n{plan_steps}\n"
            f"【注意】上一轮执行未通过验收，验收意见：{prev_verdict}\n"
            "请针对验收意见修正后重做，完成后再总结结果。"
        )
    else:
        user_msg = f"【任务】{state['user_input']}\n【执行计划】\n{plan_steps}\n请按计划执行并完成任务。"

    last_content = ""
    trace: list[dict] = []
    tokens = 0
    messages: list = []
    step_count = 0
    # 跨轮记忆来自 state["messages"]（checkpointer 按 thread_id 恢复，add_messages 负责累积）
    prior_turns = list(state.get("messages") or [])
    input_messages = [*prior_turns, HumanMessage(content=user_msg)]
    async for chunk in executor_agent.astream(
        {"messages": input_messages}, config={"recursion_limit": 100}
    ):
        step_count += 1
        for _node, output in chunk.items():
            if "messages" not in output:
                continue
            for msg in output["messages"]:
                messages.append(msg)
                tokens += _msg_tokens(msg)
                if isinstance(msg, AIMessage):
                    if msg.content:
                        last_content = msg.content if isinstance(msg.content, str) else str(msg.content)
                    for tc in getattr(msg, "tool_calls", None) or []:
                        trace.append({"name": tc.get("name", "?"), "args": tc.get("args", {})})
    return {
        "executor_result": last_content or "（Executor 未产出最终回复）",
        "executor_trace": _trace_to_text(trace),
        "executor_trace_list": trace,
        "executor_messages": messages,
        "step_count": step_count,
        "retry_count": state.get("retry_count", 0) + (1 if is_retry else 0),
        "token_usage": state.get("token_usage", 0) + tokens,
    }

async def verifier_node(state: AgentState, verifier_agent: Any) -> dict:
    """只读验收：对照需求+计划+执行轨迹，输出 PASS/FAIL + 原因。"""
    prompt = VERIFIER_PROMPT.format(
        user_input=state["user_input"],
        plan=_plan_to_text(state["plan"]),
        executor_result=state["executor_result"],
        executor_trace=state["executor_trace"],
    )
    verdict_text = ""
    tokens = 0
    messages: list = []
    trace: list[dict] = []
    async for chunk in verifier_agent.astream(
        {"messages": [HumanMessage(content=prompt)]}, config={"recursion_limit": 20}
    ):
        for _node, output in chunk.items():
            if "messages" not in output:
                continue
            for msg in output["messages"]:
                messages.append(msg)
                tokens += _msg_tokens(msg)
                if isinstance(msg, AIMessage):
                    if msg.content:
                        verdict_text = msg.content if isinstance(msg.content, str) else str(msg.content)
                    for tc in getattr(msg, "tool_calls", None) or []:
                        trace.append({"name": tc.get("name", "?"), "args": tc.get("args", {})})
    parsed = _extract_json(verdict_text)
    if parsed and parsed.get("verdict"):
        verdict_text = json.dumps(parsed, ensure_ascii=False)
    return {
        "verdict": verdict_text,
        "verifier_trace_list": trace,
        "verifier_messages": messages,
        "token_usage": state.get("token_usage", 0) + tokens,
    }


def decide_after_verify(state: AgentState) -> Literal["executor", "end"]:
    """条件边：PASS → 结束；FAIL 且未超限 → 打回 Executor；超限 → 结束。"""
    parsed = _extract_json(state["verdict"])
    verdict = (parsed or {}).get("verdict", "")
    if str(verdict).upper() == "PASS":
        return "end"
    if state["retry_count"] >= MAX_RETRY:
        return "end"
    return "executor"


# ═══════════════════════════════════════════════════════════════════
# Agent 构造
# ═══════════════════════════════════════════════════════════════════

def build_executor_agent(tools: list) -> Any:
    """构造 Executor：现有 create_react_agent，全量工具 + 现有系统 Prompt。"""
    from langchain_core.prompts import PromptTemplate
    from langgraph.prebuilt import create_react_agent

    prompt = PromptTemplate.from_template(template=SYSTEM_PROMPT_TEMPLATE)
    return create_react_agent(
        model=get_llm(),
        tools=tools,
        debug=False,
        prompt=prompt.format(**PROMPT_CONTEXT),
    )


def build_verifier_agent(all_tools: list, verify_tool_names: set[str] | None = None) -> Any:
    """构造 Verifier：只挂只读白名单工具（verify_tool_names 可动态收缩子集）。"""
    from langgraph.prebuilt import create_react_agent

    if verify_tool_names is None:
        verify_tool_names = READONLY_TOOL_NAMES
    verifier_tools = [t for t in all_tools if t.name in verify_tool_names]
    verifier_prompt = (
        "你是任务验收员（Verifier）。你只负责检查与验收，使用只读工具核实结果，"
        "【绝不修改、创建、删除任何文件或数据】。"
    )
    return create_react_agent(
        model=get_llm(),
        tools=verifier_tools,
        debug=False,
        prompt=verifier_prompt,
    )


def build_graph(executor_agent: Any, verifier_agent: Any, checkpointer: Any = None):
    """组装 StateGraph。executor_agent / verifier_agent 为已构造的 agent 实例。

    ⚠️ `checkpointer` 是刻意保留的接线，**不要删**（跨轮记忆靠它，曾在上一次重构中被误删）。
    """
    graph = StateGraph(AgentState)

    async def _executor_wrapper(state):
        return await executor_node(state, executor_agent)

    async def _verifier_wrapper(state):
        return await verifier_node(state, verifier_agent)

    graph.add_node("planner", planner_node)
    graph.add_node("executor", _executor_wrapper)
    graph.add_node("verifier", _verifier_wrapper)

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "executor")
    graph.add_edge("executor", "verifier")
    graph.add_conditional_edges(
        "verifier",
        decide_after_verify,
        {"executor": "executor", "end": END},
    )
    return graph.compile(checkpointer=checkpointer)


# ═══════════════════════════════════════════════════════════════════
# 异步入口（供 evals / REPL 使用）
# ═══════════════════════════════════════════════════════════════════

async def run_multi_agent(
    user_input: str,
    all_tools: list,
    *,
    executor_agent: Any | None = None,
    verifier_agent: Any | None = None,
    thread_id: str = "default",
) -> dict:
    """执行单次多 Agent 任务。

    跨轮记忆由 checkpointer 按 `thread_id` 持久化（SQLite），进程重启后仍在。
    调用方**只需传增量输入**（当前任务），历史由 state["messages"] 自动恢复。

    返回: {plan, executor_result, executor_trace, verdict, retry_count,
           token_usage, final_response}
    """
    if executor_agent is None:
        executor_agent = build_executor_agent(all_tools)
    if verifier_agent is None:
        verifier_agent = build_verifier_agent(all_tools)

    CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINT_DB)) as saver:
        app = build_graph(executor_agent, verifier_agent, checkpointer=saver)
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
        state = await app.ainvoke(
            # 只传增量：历史在 checkpoint 里，靠 messages 的 add_messages reducer 累积
            {"user_input": user_input, "retry_count": 0, "token_usage": 0},
            config=config,
        )

        final_response = state["executor_result"]
        if "FAIL" in str(state.get("verdict", "")).upper():
            parsed = _extract_json(state["verdict"])
            reason = (parsed or {}).get("reason", state["verdict"])
            final_response = (
                f"任务执行完成，但验收未通过（已重试 {state['retry_count']} 次）：\n"
                f"验收意见：{reason}\n\n执行结果：\n{state['executor_result']}"
            )

        # 把本轮 (任务, 最终回复) 写回线程记忆。
        # ⚠️ 只在图跑完后写一次 —— 若让 executor_node 每次返回都写，
        #    重试轮次会把中间结果也塞进记忆。
        await app.aupdate_state(
            config,
            {
                "messages": [
                    HumanMessage(content=user_input),
                    AIMessage(content=final_response),
                ]
            },
        )

    return {
        "plan": state["plan"],
        "executor_result": state["executor_result"],
        "executor_trace": state["executor_trace"],
        "verdict": state["verdict"],
        "retry_count": state["retry_count"],
        "token_usage": state.get("token_usage", 0),
        "executor_trace_list": state.get("executor_trace_list", []),
        "verifier_trace_list": state.get("verifier_trace_list", []),
        "executor_messages": state.get("executor_messages", []),
        "verifier_messages": state.get("verifier_messages", []),
        "step_count": state.get("step_count", 0),
        "final_response": final_response,
    }
