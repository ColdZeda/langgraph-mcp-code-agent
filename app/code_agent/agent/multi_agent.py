"""多 Agent 架构 — Planner → Executor → Verifier 三阶段协作图（D 路径）。

将原单 Agent 的 Plan→Execute→Verify 从 Prompt 软约束升级为 StateGraph 硬流程：
- Planner：纯 LLM（无工具），产出结构化计划 JSON（含 verify_tools 声明）
- Executor：复用 create_react_agent（全量工具），按计划执行
- Verifier：只读工具白名单（按计划动态挂载子集），对照「需求 + 计划 + 执行轨迹」验收
- 打回机制：Verifier FAIL 时带原因打回 Executor 重做，最多 2 轮
"""

from __future__ import annotations

import json
import logging
import re
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.code_agent.agent.context import (
    compact_history,
    estimate_messages_tokens,
    over_task_budget,
    prune_messages,
)
from app.code_agent.agent.memory import inject_relevant_knowledge, maybe_deposit_knowledge
from app.code_agent.agent.prompts import (
    EXECUTOR_PLAN_PROMPT,
    PROMPT_CONTEXT,
    SYSTEM_PROMPT_TEMPLATE,
)
from app.code_agent.config import (
    CHECKPOINT_DB,
    COMPACT_THRESHOLD_TOKENS,
    NODE_TOKEN_BUDGET,
    RAG_AUTO_DEPOSIT,
    RAG_AUTO_INJECT,
    TASK_TOKEN_BUDGET,
)
from app.code_agent.model.llm import get_llm, invoke_with_fallback, registry, with_fallback

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# Verifier 只读工具白名单（代码级强制：不在名单内的工具绝不会挂载给 Verifier）
# ═══════════════════════════════════════════════════════════════════

READONLY_TOOL_NAMES: set[str] = {
    # code_tools（MCP）只读
    "read_file_range",
    "generate_diff",
    "analyze_ast",
    "list_project_structure",
    # MySQL 只读（查询/结构，无写权限）
    "mysql_list_databases",
    "mysql_list_tables",
    "mysql_describe_tables",
    "mysql_execute_query",
    # RAG 查询
    "query_rag",
    # FileManagementToolkit 读/查看类
    "read_file",
    "list_directory",
    "file_search",
}

MAX_RETRY = 2  # Verifier 打回上限


# ═══════════════════════════════════════════════════════════════════
# 图状态
# ═══════════════════════════════════════════════════════════════════


class AgentState(TypedDict):
    user_input: str  # 原始用户需求
    plan: str  # Planner 产出（JSON 字符串）
    executor_result: str  # Executor 最终回复
    executor_trace: str  # Executor 工具调用摘要（供 Verifier 参考）
    verdict: str  # Verifier 判定（JSON：verdict/reason）
    retry_count: int  # 已打回次数
    token_usage: int  # 全流程 token 累计
    # evals 存档用（结构化输出）
    executor_trace_list: list[dict]  # Executor 工具调用 trace [{name, args}]
    verifier_trace_list: list[dict]  # Verifier 工具调用 trace
    executor_messages: list  # Executor 消息流（组装对话存档用）
    verifier_messages: list  # Verifier 消息流
    step_count: int  # Executor 执行步数（近似原单 Agent 步数）
    route: str  # auto 模式的路由结论："simple" / "complex"（由 route_node 写入）
    # ── 阶段 4：上下文工程 ──
    knowledge: str  # 任务开始时自动注入的相关经验（T4.4 ②；空串=没注入）
    budget_exceeded: bool  # 任务级 token 预算是否已击穿（T4.3）
    pruned_messages: int  # 节点级剪枝砍掉的消息条数（T4.3，用于留证据）
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
        return "\n".join(f"{i + 1}. {s}" for i, s in enumerate(obj["steps"]))
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
    # 走 planner 角色的降级链（模型报错/超时会自动换下一个）
    resp = await invoke_with_fallback(
        registry.chain("planner"),
        [SystemMessage(content="你是规划员。"), HumanMessage(content=prompt)],
    )
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

    **阶段 4 在这里加了三个上下文工程动作**（都只是"改喂给模型的 prompt"，
    不动 checkpoint 里的完整历史）：
      - T4.4 ②：把任务开始时检索到的相关经验拼在【任务】前面；
      - T4.3 节点级：进模型前按 NODE_TOKEN_BUDGET 剪枝（先砍最老的工具结果）；
      - T4.3 任务级：累计 token 已击穿 TASK_TOKEN_BUDGET → **直接终止并报告**。
    """
    tokens_used = state.get("token_usage", 0)
    if over_task_budget(tokens_used, TASK_TOKEN_BUDGET):
        logger.warning(
            "任务级 token 预算击穿：已用 %d / 上限 %d，主动终止", tokens_used, TASK_TOKEN_BUDGET
        )
        return {
            "executor_result": (
                f"【已终止】本次任务累计消耗 {tokens_used} token，"
                f"达到上限 {TASK_TOKEN_BUDGET}（CODE_AGENT_TASK_TOKEN_BUDGET）。\n"
                "为避免继续消耗，已主动停止执行。已完成的部分见上方工具调用轨迹；"
                "如需继续，请缩小任务范围后重试。"
            ),
            "budget_exceeded": True,
            "executor_trace": _trace_to_text(state.get("executor_trace_list") or []),
            "executor_trace_list": state.get("executor_trace_list") or [],
            "executor_messages": [],
            "step_count": state.get("step_count", 0),
        }

    plan_steps = _plan_to_text(state.get("plan", ""))
    prev_verdict = str(state.get("verdict") or "")
    is_retry = "FAIL" in prev_verdict.upper()  # 上一轮验收失败 → 本次是重跑
    # 自动注入的知识（T4.4 ②）：只在任务语义上拼一次，重跑时也保留
    knowledge = str(state.get("knowledge") or "")
    knowledge_prefix = f"{knowledge}\n\n" if knowledge else ""
    if is_retry:
        user_msg = (
            f"{knowledge_prefix}"
            f"【任务】{state['user_input']}\n"
            f"【执行计划】\n{plan_steps}\n"
            f"【注意】上一轮执行未通过验收，验收意见：{prev_verdict}\n"
            "请针对验收意见修正后重做，完成后再总结结果。"
        )
    else:
        user_msg = (
            f"{knowledge_prefix}"
            f"【任务】{state['user_input']}\n【执行计划】\n{plan_steps}\n请按计划执行并完成任务。"
        )

    last_content = ""
    trace: list[dict] = []
    tokens = 0
    messages: list = []
    step_count = 0
    # 跨轮记忆来自 state["messages"]（checkpointer 按 thread_id 恢复，add_messages 负责累积）
    prior_turns = list(state.get("messages") or [])
    raw_input = [*prior_turns, HumanMessage(content=user_msg)]
    input_messages = prune_messages(raw_input, NODE_TOKEN_BUDGET)
    pruned = len(raw_input) - len(input_messages)
    if pruned:
        logger.info(
            "节点级剪枝：prompt 估算 %d token 超过预算 %d，砍掉 %d 条最老消息",
            estimate_messages_tokens(raw_input),
            NODE_TOKEN_BUDGET,
            pruned,
        )
    budget_hit = False
    spent_before = state.get("token_usage", 0)
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
                        last_content = (
                            msg.content if isinstance(msg.content, str) else str(msg.content)
                        )
                    for tc in getattr(msg, "tool_calls", None) or []:
                        trace.append({"name": tc.get("name", "?"), "args": tc.get("args", {})})
        # ⚠️ 任务级预算**必须在这里也要判**：单个 executor 节点内部的 ReAct 循环
        #    是不经过图节点边界的，只在节点入口判的话，一次"读大文件 + 反复重读"
        #    就能在**一次**节点调用里烧掉十几万 token 而永远不触发上限（实测 127,071）。
        if over_task_budget(spent_before + tokens, TASK_TOKEN_BUDGET):
            budget_hit = True
            logger.warning(
                "任务级 token 预算击穿：本节点已用 %d（累计 %d / 上限 %d），提前终止 ReAct 循环",
                tokens,
                spent_before + tokens,
                TASK_TOKEN_BUDGET,
            )
            break

    if budget_hit:
        return {
            "executor_result": (
                f"【已终止】本次任务累计消耗 {spent_before + tokens} token，"
                f"达到上限 {TASK_TOKEN_BUDGET}（CODE_AGENT_TASK_TOKEN_BUDGET），"
                f"在第 {step_count} 步提前停止。\n"
                f"已执行的工具调用：{_trace_to_text(trace)}\n"
                f"终止前拿到的最后一段结论：{last_content[:500] or '（无）'}\n"
                "如需继续，请缩小任务范围（例如只读需要的文件片段）后重试。"
            ),
            "budget_exceeded": True,
            "executor_trace": _trace_to_text(trace),
            "executor_trace_list": trace,
            "executor_messages": messages,
            "step_count": step_count,
            "token_usage": spent_before + tokens,
            "pruned_messages": state.get("pruned_messages", 0) + pruned,
        }

    return {
        "executor_result": last_content or "（Executor 未产出最终回复）",
        "executor_trace": _trace_to_text(trace),
        "executor_trace_list": trace,
        "executor_messages": messages,
        "step_count": step_count,
        "retry_count": state.get("retry_count", 0) + (1 if is_retry else 0),
        "token_usage": spent_before + tokens,
        "pruned_messages": state.get("pruned_messages", 0) + pruned,
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
                        verdict_text = (
                            msg.content if isinstance(msg.content, str) else str(msg.content)
                        )
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
    """条件边：PASS → 结束；FAIL 且未超限 → 打回 Executor；超限 → 结束。

    另外两条**必须先判**的终止条件：
      - 任务级 token 预算击穿（T4.3）→ 直接结束，别再打回重跑烧 token；
      - 击穿过一次之后同样直接结束。
    """
    if state.get("budget_exceeded"):
        return "end"
    parsed = _extract_json(state["verdict"])
    verdict = (parsed or {}).get("verdict", "")
    if str(verdict).upper() == "PASS":
        return "end"
    if state["retry_count"] >= MAX_RETRY:
        return "end"
    return "executor"


# ═══════════════════════════════════════════════════════════════════
# 执行模式路由（single / multi / auto）
# ═══════════════════════════════════════════════════════════════════

# 规则兜底：明显复杂的任务直接判 complex（漏判的代价只是多花 token，可恢复）
COMPLEX_KEYWORDS = ("删除", "重构", "部署", "批量", "所有文件", "整个项目", "批量修改")

ROUTER_PROMPT = (
    "判断下面这个任务需要「单步执行」还是「多步规划+验证」。\n"
    "单步 = 查看/读取/查询/搜索类，调 1-2 次工具就能答完。\n"
    "多步 = 需要改多个文件、需要规划步骤、有明确产物要验收。\n\n"
    "任务：{task}\n\n"
    "只输出一个词：simple 或 complex"
)


def route_task(task: str | dict) -> Literal["simple", "complex"]:
    """判断任务复杂度（规则兜底 + LLM 分类）。

    入参既接受任务文本，也接受 state 字典（取其中的 user_input）。
    """
    text = task.get("user_input", "") if isinstance(task, dict) else str(task)
    if any(k in text for k in COMPLEX_KEYWORDS):
        return "complex"
    return _llm_classify_complexity(text)


def _llm_classify_complexity(task_text: str) -> Literal["simple", "complex"]:
    """用一次轻量 LLM 调用判断复杂度；**失败时保守走 complex**（宁可多花 token 也别漏验证）。"""
    try:
        resp = get_llm("router").invoke(ROUTER_PROMPT.format(task=task_text))
        verdict = str(resp.content).strip().lower()
        return "complex" if "complex" in verdict else "simple"
    except Exception:
        return "complex"


async def route_node(state: AgentState) -> dict:
    """真正做判断的**节点**：把结论写进 state。

    ⚠️ 为什么必须是节点而不是直接挂在条件边上：LangGraph 的条件边函数
    只负责"选边"，**返回值不会写进 state** —— 那样 `executor_node` 就无从知道
    自己来自 simple 还是 complex 分支。
    """
    return {"route": route_task(state)}


def _route_decide(state: AgentState) -> Literal["simple", "complex"]:
    """纯函数：只读 route_node 写下的结论来选边（缺省保守走 complex）。"""
    return state.get("route", "complex")  # type: ignore[return-value]


def after_executor(state: AgentState) -> Literal["verifier", "end"]:
    """auto 模式下 Executor 的收尾：simple 路径直接结束，complex 路径去验收。

    预算击穿（T4.3）必须先结束 —— 击穿时 Executor 根本没执行，
    再去跑 Verifier 只是白花一次模型调用。
    """
    if state.get("budget_exceeded"):
        return "end"
    return "end" if state.get("route") == "simple" else "verifier"


# ═══════════════════════════════════════════════════════════════════
# Agent 构造
# ═══════════════════════════════════════════════════════════════════


def build_executor_agent(tools: list, *, mode: str = "multi", llm: Any | None = None) -> Any:
    """构造 Executor：create_react_agent + 全量工具。

    **按模式选 Prompt**（这是 T3.1 的关键之一）：
      - `single`：用 SYSTEM_PROMPT_TEMPLATE（自带 Plan→Execute→Verify 三步法，
        Executor 需要自己规划 —— 这正是改造前单 Agent 的路子）；
      - `multi` / `auto`：用 EXECUTOR_PLAN_PROMPT（"按给定计划执行"），
        避免和 Planner 的指令打架；auto 模式下如果计划为空，它也会直接完成简单任务。
    """
    from langchain_core.prompts import PromptTemplate
    from langgraph.prebuilt import create_react_agent

    template = SYSTEM_PROMPT_TEMPLATE if mode == "single" else EXECUTOR_PLAN_PROMPT
    prompt = PromptTemplate.from_template(template=template)
    # 走 executor 角色的降级链：主力失败时 LangChain 会自动切到备用模型
    model = llm or with_fallback(registry.chain("executor"))
    return create_react_agent(
        model=model,
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
        model=with_fallback(registry.chain("verifier")),
        tools=verifier_tools,
        debug=False,
        prompt=verifier_prompt,
    )


def build_graph(
    executor_agent: Any,
    verifier_agent: Any,
    checkpointer: Any = None,
    mode: str = "auto",
):
    """组装 StateGraph。executor_agent / verifier_agent 为已构造的 agent 实例。

    `mode`：
      - `single`：START → executor → END（单 Agent，跳过规划与验收）
      - `multi` ：START → planner → executor → verifier（→ FAIL 打回 executor）
      - `auto`  ：START → route →（simple: executor / complex: planner→executor→verifier）

    ⚠️ `checkpointer` 是刻意保留的接线，**不要删**（跨轮记忆靠它，曾在上一次重构中被误删）。
    """
    graph = StateGraph(AgentState)

    async def _executor_wrapper(state):
        return await executor_node(state, executor_agent)

    async def _verifier_wrapper(state):
        return await verifier_node(state, verifier_agent)

    graph.add_node("route", route_node)
    graph.add_node("planner", planner_node)
    graph.add_node("executor", _executor_wrapper)
    graph.add_node("verifier", _verifier_wrapper)

    if mode == "single":
        graph.add_edge(START, "executor")
        graph.add_edge("executor", END)
    elif mode == "multi":
        graph.add_edge(START, "planner")
        graph.add_edge("planner", "executor")
        graph.add_edge("executor", "verifier")
        graph.add_conditional_edges(
            "verifier",
            decide_after_verify,
            {"executor": "executor", "end": END},
        )
    else:  # auto
        graph.add_edge(START, "route")
        graph.add_conditional_edges(
            "route", _route_decide, {"simple": "executor", "complex": "planner"}
        )
        graph.add_edge("planner", "executor")
        # Executor 的出边要看来源：simple 直接结束，complex 才去验收
        graph.add_conditional_edges(
            "executor", after_executor, {"verifier": "verifier", "end": END}
        )
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
    mode: str = "auto",
    auto_inject: bool | None = None,
    auto_deposit: bool | None = None,
) -> dict:
    """执行单次多 Agent 任务。

    跨轮记忆由 checkpointer 按 `thread_id` 持久化（SQLite），进程重启后仍在。
    调用方**只需传增量输入**（当前任务），历史由 state["messages"] 自动恢复。

    `mode`: single / multi / auto（见 build_graph）。执行模式与路由结论都会出现在返回值里，
    供 evals 统计"多少题走了哪条路径"。

    `auto_inject` / `auto_deposit`（T4.4）：默认跟随配置；评估侧**必须**显式关掉沉淀
    （否则评测过程会改写知识库，后续题目不可比）。

    返回: {plan, executor_result, executor_trace, verdict, retry_count, token_usage,
           final_response, mode, route, knowledge_injected, compacted, budget_exceeded,
           pruned_messages, deposited}
    """
    if executor_agent is None:
        executor_agent = build_executor_agent(all_tools, mode=mode)
    if verifier_agent is None:
        verifier_agent = build_verifier_agent(all_tools)

    # ── T4.4 ②：任务开始时自动检索一次相关知识（不走 MCP，见 memory.py 的说明）──
    inject_enabled = RAG_AUTO_INJECT if auto_inject is None else auto_inject
    knowledge_text, injected_items = ("", [])
    if inject_enabled:
        knowledge_text, injected_items = inject_relevant_knowledge(user_input)

    CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINT_DB)) as saver:
        app = build_graph(executor_agent, verifier_agent, checkpointer=saver, mode=mode)
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}

        # ── T4.2：先把过长的历史压实，再进图 ──
        # 只在**确实超阈值**时才去构造模型链（否则每轮都白建一次 LLM 对象）
        compacted = False
        snapshot = await app.aget_state(config)
        prior = list((snapshot.values or {}).get("messages") or [])
        if prior and estimate_messages_tokens(prior) > COMPACT_THRESHOLD_TOKENS:
            new_history, compacted = await compact_history(prior, registry.chain("executor"))
            if compacted:
                logger.info(
                    "对话压实：%d 条历史（估算 %d token）→ 摘要 + 最近 %d 条",
                    len(prior),
                    estimate_messages_tokens(prior),
                    len(new_history) - 1,
                )
                # 用 RemoveMessage 清掉旧消息，再把「摘要 + 最近若干条」写回 —— 结果列表正好是后者。
                # 摘要**同时被持久化**：下一轮不用重复摘要（重复摘要等于每轮多烧一次模型调用）。
                await app.aupdate_state(
                    config,
                    {
                        "messages": [
                            RemoveMessage(id=m.id)
                            for m in prior
                            if getattr(m, "id", None) is not None
                        ]
                        + new_history
                    },
                )

        state = await app.ainvoke(
            # 只传增量：历史在 checkpoint 里，靠 messages 的 add_messages reducer 累积
            {
                "user_input": user_input,
                "retry_count": 0,
                "token_usage": 0,
                "knowledge": knowledge_text,
                "budget_exceeded": False,
                "pruned_messages": 0,
            },
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

    # ── T4.4 ③：任务成功后判断要不要把经验沉淀进知识库 ──
    # 只记成功经验（失败的多半是环境问题，存进去就是噪音）
    deposited: list[dict] = []
    succeeded = "FAIL" not in str(state.get("verdict", "")).upper() and not state.get(
        "budget_exceeded"
    )
    if succeeded:
        save_tool = next((t for t in all_tools if getattr(t, "name", "") == "save_knowledge"), None)
        deposited = await maybe_deposit_knowledge(
            user_input,
            final_response,
            chain=registry.chain("executor"),
            save_tool=save_tool,
            enabled=RAG_AUTO_DEPOSIT if auto_deposit is None else auto_deposit,
        )

    return {
        # single 模式没有 planner / verifier → 用 .get 兜底，不要 KeyError
        "plan": state.get("plan", ""),
        "executor_result": state["executor_result"],
        "executor_trace": state.get("executor_trace", ""),
        "verdict": state.get("verdict", ""),
        "retry_count": state.get("retry_count", 0),
        "mode": mode,
        "route": state.get("route", ""),
        "token_usage": state.get("token_usage", 0),
        "executor_trace_list": state.get("executor_trace_list", []),
        "verifier_trace_list": state.get("verifier_trace_list", []),
        "executor_messages": state.get("executor_messages", []),
        "verifier_messages": state.get("verifier_messages", []),
        "step_count": state.get("step_count", 0),
        "final_response": final_response,
        # ── 阶段 4 的证据字段（供 evals / 汇报引用）──
        "knowledge_injected": [it["id"] for it in injected_items],
        "compacted": compacted,
        "budget_exceeded": bool(state.get("budget_exceeded")),
        "pruned_messages": state.get("pruned_messages", 0),
        "deposited": deposited,
    }
