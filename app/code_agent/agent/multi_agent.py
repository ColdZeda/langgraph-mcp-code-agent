"""多 Agent 架构 — Planner → Executor → Verifier 三阶段协作图（D 路径）。

将原单 Agent 的 Plan→Execute→Verify 从 Prompt 软约束升级为 StateGraph 硬流程：
- Planner：纯 LLM（无工具），产出结构化计划 JSON（goal + steps）
  ⚠️ 曾经还让它声明 `verify_tools`（"建议验收员用哪些只读工具"），但**全仓库没有任何代码去读它**
  —— Planner 每次都在花 token 产出一个被丢掉字段。2026-09-24 已从提示词里删掉。
  想真正接上（按题收缩 Verifier 工具集）要改架构：Verifier 现在是**图跑之前**构建的，
  而计划是**图里面**才产出的。已登记进候选池。
- Executor：复用 create_react_agent（全量工具），按计划执行
- Verifier：只读工具白名单（按计划动态挂载子集），对照「需求 + 计划 + 执行轨迹」验收
- 打回机制：Verifier FAIL 时带原因打回 Executor 重做，最多 2 轮
"""

from __future__ import annotations

import asyncio
import copy
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

from app.code_agent.agent.cancel import (
    REASON_USER,
    REASON_WALL_CLOCK,
    TaskCancelled,
    current_token,
)
from app.code_agent.agent.context import (
    compact_history,
    estimate_messages_tokens,
    over_task_budget,
    prune_messages,
)
from app.code_agent.agent.events import emit
from app.code_agent.agent.memory import inject_relevant_knowledge, maybe_deposit_knowledge
from app.code_agent.agent.prompts import (
    EXECUTOR_PLAN_PROMPT,
    SYSTEM_PROMPT_TEMPLATE,
    prompt_context,
)
from app.code_agent.config import (
    CHECKPOINT_DB,
    RAG_AUTO_DEPOSIT,
    RAG_AUTO_INJECT,
    token_budgets,
)
from app.code_agent.model.llm import get_llm, invoke_with_fallback, registry, with_fallback
from app.code_agent.utils.placeholder_guard import (
    clarify_reply,
    find_placeholders,
    is_explicitly_literal,
)

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
    planner_model: str  # Planner 这次实际用的模型名（服务端回报；结果卡片显示用）
    # ── 阶段 4：上下文工程 ──
    knowledge: str  # 任务开始时自动注入的相关经验（T4.4 ②；空串=没注入）
    budget_exceeded: bool  # 任务级 token 预算是否已击穿（T4.3）
    pruned_messages: int  # 节点级剪枝砍掉的消息条数（T4.3，用于留证据）
    # ── 阶段 8 · P0：停止（协作式取消 / 墙钟）──
    cancelled: bool  # 本次任务是否被停止（用户点停止 / 墙钟到点）
    cancel_reason: str  # 停止原因码："user" / "wall_clock"（空串=没停）
    cancel_stage: str  # 在哪个节点停下的："planner" / "executor" / "verifier"
    # ── 跨轮记忆（唯一的会话通道）──
    # ⚠️ 必须带 `add_messages` reducer：没 reducer 的通道是"新值覆盖旧值"，
    #    那样即使接了 checkpointer，按 thread_id 也恢复不出对话。
    #    每轮由 run_multi_agent 在图跑完后追加一对 (任务, 最终回复)。
    messages: Annotated[list[AnyMessage], add_messages]


# ═══════════════════════════════════════════════════════════════════
# 阶段 8 · 实测修复：**每轮通道必须复位**（否则上一轮的值串到本轮）
# ═══════════════════════════════════════════════════════════════════
#
# 🔴 为什么：LangGraph 的语义是"**没传进 `ainvoke` 的通道保留上一轮 checkpoint 的值**"。
#    用户第 3 轮实测（2026-10-07 晚）撞到的现场：
#      · `plan` 没复位 ⇒ `auto` 判成 `simple`（跳过 Planner）时，Executor 拿到的是**上一轮的计划**，
#        对着旧计划干活（一句「你好」白跑 13 次工具调用 6 分钟、整段英文）；用户可见回复里
#        还混进了上一轮计划的章节；
#      · `verdict` 没复位 ⇒ 新一轮的**第一次** Executor 执行会被 `_is_retry_round` 当成"重跑轮"
#        （`retry_count` 白加一次、提示词里多一段"被打回"的话）；
#      · `executor_trace_list` 没复位 ⇒ 轨迹跨轮累加（实测 28 条 = 两轮混在一起）。
#    ⇒ 教训与 F2（`step_count`）**同一个**：**新增任何"每轮"通道，必须同时进这张表**。
#    机械守卫：`tests/test_per_turn_state.py` 会拿 `AgentState` 的**全部通道**对这张表 +
#    `PER_TURN_ASSIGNED` + `CROSS_TURN_CHANNELS` 做覆盖校验 —— 漏一个就红。
PER_TURN_RESET: dict[str, Any] = {
    "plan": "",
    "executor_result": "",
    "executor_trace": "",
    "verdict": "",  # ⚠️ 不复位会让新一轮被误判成"重跑轮"
    "retry_count": 0,
    "token_usage": 0,
    "executor_trace_list": [],
    "verifier_trace_list": [],
    "executor_messages": [],
    "verifier_messages": [],
    "step_count": 0,
    "route": "",
    "planner_model": "",
    "budget_exceeded": False,
    "pruned_messages": 0,
    "cancelled": False,
    "cancel_reason": "",
    "cancel_stage": "",
}

#: 每轮由 `run_multi_agent` **显式赋值**（不是复位成固定值）的通道。
PER_TURN_ASSIGNED: tuple[str, ...] = ("user_input", "knowledge")

#: 跨轮累积的通道（只有它一个：带 `add_messages` reducer）。
CROSS_TURN_CHANNELS: tuple[str, ...] = ("messages",)


def per_turn_reset() -> dict[str, Any]:
    """给 `ainvoke` 用的复位字典（**每轮一份新对象**，避免共享可变默认值）。"""
    return copy.deepcopy(PER_TURN_RESET)


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
  "steps": ["步骤1：...", "步骤2：...", "步骤3：..."]
}}

注意：
- steps 要具体到可执行动作（读哪个文件、创建什么、查什么库），不要空泛。
- 简单查询/搜索/查库类任务（搜索关键词、查表、读文件后汇报）：直接调用对应工具并汇报结果即可，
  【不要创建中间文件、不要写解析脚本、不要执行多余命令】，除非用户明确要求保存/处理。"""

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


def _msg_model(msg: Any) -> str:
    """取服务端**真实回报**的模型名（`response_metadata.model_name`）。

    为什么不用配置里的名字：配置说的是"我让谁答"，这里是"**真的谁答的**"
    —— 两者在"官方把旧模型名路由到新模型"这种情况下会不一样（本机实测过：
    发 `deepseek-v4-flash`，服务端回报 `deepseek-flash`）。结果卡片与阶段 6 的
    评估报告都用这个字段做"模型口径"。
    """
    meta = getattr(msg, "response_metadata", None) or {}
    return str(meta.get("model_name") or meta.get("model") or "")


def _models_of(messages: Any) -> list[str]:
    """一组消息里出现过的模型名（去重排序，空值丢掉）。"""
    return sorted({name for name in (_msg_model(m) for m in (messages or [])) if name})


def _plan_to_text(plan: str) -> str:
    obj = _extract_json(plan)
    if obj and obj.get("steps"):
        return "\n".join(f"{i + 1}. {s}" for i, s in enumerate(obj["steps"]))
    return plan or "（无计划）"


# ═══════════════════════════════════════════════════════════════════
# 阶段 8 · P0：停止（协作式取消 + 墙钟）与任务状态条
# ═══════════════════════════════════════════════════════════════════


#: 两种终止（停止 / 预算）共用的两句话。
#: ⚠️ **谁都不许再写"见上方工具调用轨迹"**（账本 R6）：历史回放里**只有最终回复**、
#: 没有轨迹，那句话会把用户引到一个不存在的地方。轨迹在 Web 的结果卡片里、不在对话历史里。
_TERMINATION_TAIL = "如需继续，请把范围缩小后重试（例如只读需要的文件片段，或把任务拆成几次）。"
_SIDE_EFFECTS_KEPT = (
    "⚠️ 已经发生的副作用（写好的文件 / 建好的目录 / 库里的数据）一律保留 —— "
    "终止只保证「后面不再继续」，不做回滚。"
)


def _budget_message(
    *, total_tokens: int, step_count: int, tool_calls: int, limit: int, last: str = ""
) -> str:
    """预算击穿后的**固定中文说明**（结构与 `_cancel_message` 对齐）。

    ⚠️ 与"停止"分开写：**预算终止 ≠ 任务失败**。第 2 题现场是「因预算终止」被拼成
    "验收未通过"+ 一句英文错误串（账本 R5），用户完全看不懂发生了什么 ——
    预算只是**成本保险丝**，产物与已经拿到的结论都还在。

    `limit`：本次实际生效的额度。**必须由调用方传进来**（阶段 8 · P2）：
    它现在是按"当前模型的窗口"运行期算出来的，不再是一个写死的常量。
    """
    where = (
        f"任务在「第 {step_count} 步之后」停下" if step_count > 0 else "任务在「开始执行前」就停了"
    )
    return (
        f"【已终止·预算】本次任务累计消耗 {total_tokens} token，"
        f"达到上限 {limit}（CODE_AGENT_TASK_TOKEN_BUDGET 或按窗口自动算的值）—— {where}。\n"
        "预算终止是「成本保险丝」：不代表任务做错了，也不是「验收未通过」。\n"
        f"{_SIDE_EFFECTS_KEPT}\n"
        f"已执行 {tool_calls} 次工具调用。\n"
        f"停下前拿到的最后一段结论：{last[:500] or '（无）'}\n"
        f"{_TERMINATION_TAIL}"
    )


def _cancel_reason_text(token: Any) -> str:
    """停止原因 → 给用户看的中文（原因码本身在 `cancel.py` 里，是结构化的）。"""
    reason = getattr(token, "reason", "") or REASON_USER
    if reason != REASON_WALL_CLOCK:
        return "你点了「停止」"
    limit = float(getattr(token, "wall_clock", 0) or 0)
    minutes = f"{limit / 60:.0f} 分钟" if limit >= 60 else f"{limit:.0f} 秒"
    return f"达到单任务最长执行时间（{minutes}，CODE_AGENT_TASK_WALL_CLOCK）"


def _cancel_where_text(stage: str, step_count: int, tool_calls: int) -> str:
    """停在哪 —— **按真实发生的事说**，不能出现"一步都没跑"却已经跑过工具的情况。"""
    if stage == "planner":
        return "任务在「规划阶段」就停了（还没开始执行）"
    if stage == "verifier":
        return "产物已经产出，「验收环节」被停止"
    if step_count > 0:
        return f"任务在「第 {step_count} 步之后」停下"
    if tool_calls > 0:
        return "任务在「执行中途」停下（那一步没走完）"
    return "任务在「开始执行前」就停了（一步都没跑）"


def _cancel_message(
    token: Any, *, stage: str, step_count: int, total_tokens: int, tool_calls: int, last: str = ""
) -> str:
    """停止后的**固定中文说明**。

    为什么要"固定"：第 2 题现场是「因预算终止」却被拼成"验收未通过"+ 一句英文错误串，
    用户根本看不出发生了什么（账本 R5）。停止也一样，必须一眼看懂：
    **谁停的 / 停在哪 / 已经发生了什么 / 还能怎么办**。
    """
    elapsed = float(getattr(token, "elapsed", lambda: 0.0)())
    minutes, seconds = divmod(int(elapsed), 60)
    return (
        f"【已停止】{_cancel_reason_text(token)} —— "
        f"{_cancel_where_text(stage, step_count, tool_calls)}。\n"
        f"已用 {minutes} 分 {seconds} 秒（不含人工确认的等待）· 累计 {total_tokens} token · "
        f"已执行 {tool_calls} 次工具调用。\n"
        f"{_SIDE_EFFECTS_KEPT}\n"
        f"停下前拿到的最后一段结论：{last[:500] or '（无）'}\n"
        f"{_TERMINATION_TAIL}"
    )


def _stopped() -> Any:
    """当前任务的停止开关是否已生效；返回 token（没绑定时 None）。"""
    token = current_token()
    return token if token is not None and token.cancelled else None


async def _emit_status() -> None:
    """推一条任务状态条事件（时长 / 步数 / 用量）。

    ⚠️ **没有绑定停止开关时一个事件都不发** —— evals、单测、任何不关心界面状态的入口
    零改动（与 `events.emit` 的"没绑 sink 就跳过"同一个设计）。
    """
    token = current_token()
    if token is None:
        return
    await emit({"type": "status", **token.snapshot()})


def _cancel_stop_result(
    state: AgentState,
    token: Any,
    *,
    stage: str,
    step_count: int = 0,
    tokens_spent: int = 0,
    trace: list[dict] | None = None,
    messages: list | None = None,
    last_content: str = "",
    pruned: int = 0,
    is_retry: bool = False,
) -> dict:
    """停止命中时的**统一返回**（executor 用；planner / verifier 见各自节点）。

    与"预算击穿"那条路**刻意分开**：预算击穿是"钱烧完了"，停止是"人让它停/时间到了"，
    两种终止的文案、`cancel_reason`、以及"要不要再去验收"都不一样。
    """
    trace = trace or []
    prior_trace = list(state.get("executor_trace_list") or [])
    total = state.get("token_usage", 0) + tokens_spent
    # 阶段 8 · P0（真机验收 F2）：文案与卡片必须报**同一个步数**。
    # 本地 `step_count` 只是"本次节点调用"的块数；一轮内被打回重跑过的话，
    # 卡片的 `stepCount`（= 本轮累计）会更大 ⇒ 同一张卡片上出现"第 11 步"与"步数 51"两个数字。
    total_steps = state.get("step_count", 0) + step_count
    return {
        "executor_result": _cancel_message(
            token,
            stage=stage,
            step_count=total_steps,
            total_tokens=total,
            tool_calls=len(prior_trace) + len(trace),
            last=last_content,
        ),
        "cancelled": True,
        "cancel_reason": getattr(token, "reason", "") or REASON_USER,
        "cancel_stage": stage,
        # trace 沿用"跨轮累积"的规矩（修 D2）：被打回重跑过的话，前几轮真干的活也在
        "executor_trace": _trace_to_text(prior_trace + trace),
        "executor_trace_list": prior_trace + trace,
        "executor_messages": messages or [],
        "step_count": total_steps,
        # 阶段 8 · P1（F3）：被停止的这一轮若本来就是"被打回后发起的"，同样要计一次打回
        # （否则卡片上会写「打回 0 次」，而实际上明明被打回过）
        "retry_count": state.get("retry_count", 0) + (1 if is_retry else 0),
        "token_usage": total,
        "pruned_messages": state.get("pruned_messages", 0) + pruned,
    }


# ═══════════════════════════════════════════════════════════════════
# 节点实现
# ═══════════════════════════════════════════════════════════════════


async def planner_node(state: AgentState) -> dict:
    """纯 LLM 规划，产出结构化计划。"""
    token = current_token()
    if token is not None:
        token.stage = "planner"
    await emit({"type": "node", "node": "planner", "status": "start"})
    # ── 阶段 8 · P0：节点入口的停止检查（"还没开始的节点"一步都不该跑）──
    stopped = _stopped()
    if stopped is not None:
        logger.info("任务已停止（%s）：跳过 Planner", stopped.reason)
        await emit(
            {
                "type": "node",
                "node": "planner",
                "status": "end",
                "cancelled": True,
                "cancelReason": stopped.reason,
            }
        )
        # 只打标记：真正的终止文案由 executor 节点统一产出（planner 后面必然是 executor）
        return {
            "cancelled": True,
            "cancel_reason": stopped.reason,
            "cancel_stage": "planner",
        }
    await _emit_status()
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
    planner_tokens = _msg_tokens(resp)
    if token is not None:
        token.tokens = state.get("token_usage", 0) + planner_tokens
    await _emit_status()
    await emit(
        {
            "type": "node",
            "node": "planner",
            "status": "end",
            "steps": len((parsed or {}).get("steps") or []),
            # 阶段 6：token 带进事件 —— 评估报告要按节点拆"哪一段最费 token"
            "tokens": planner_tokens,
        }
    )
    return {
        "plan": plan_text,
        "token_usage": state.get("token_usage", 0) + planner_tokens,
        # 结果卡片要显示"本轮实际用了哪个模型"（服务端回报的那个名字）
        "planner_model": _msg_model(resp),
    }


def _trace_to_text(trace: list[dict]) -> str:
    lines = []
    for t in trace[-15:]:
        args = json.dumps(t["args"], ensure_ascii=False)[:120]
        lines.append(f"- {t['name']}({args})")
    return "\n".join(lines) if lines else "（无工具调用）"


def _verdict_passed(verdict_text: str) -> bool:
    """裁定是否**通过** —— 这是全流程**唯一**的判据（2026-09-24 修 D1，别改回字符串猜法）。

    ⚠️ **为什么不能用 `"FAIL" in verdict.upper()`**：Verifier 的输出**不保证是合法 JSON**。
    上游抽风时会直接返回 `Sorry, need more steps to process this request.` 这类错误串（实测踩到），
    那时字符串里既没有 `PASS` 也没有 `FAIL` ⇒ 旧写法判 `is_retry=False` ⇒
    `retry_count` **永不增长** ⇒ `decide_after_verify` 里那条「最多打回 MAX_RETRY 次」**永不生效**
    ⇒ 只能等 token 预算被烧穿（实测 E007：Executor 跑 3 次 / 232,700 token / 击穿 200k 预算，
    而且因为 `is_retry=False`，**重跑时连"上一轮为什么没通过"都没告诉它** ⇒ 盲重试）。

    语义：**只有明确解析出 `{"verdict": "PASS"}` 才算通过**；其余（含空、含错误串）一律算未通过。
    """
    parsed = _extract_json(verdict_text or "")
    return str((parsed or {}).get("verdict", "")).upper() == "PASS"


def _is_retry_round(prev_verdict: str) -> bool:
    """本次 Executor 是不是**重跑**（上一轮裁定存在且未通过）。

    ⚠️ 它就是 `retry_count` 的自增判据，而 `retry_count` 是「最多打回 MAX_RETRY 次」**唯一**的终止依据
    —— 所以这里**绝不能**用 `"FAIL" in prev_verdict` 那种字符串猜法（修 D1，见 `_verdict_passed`）。
    """
    text = str(prev_verdict or "")
    return bool(text.strip()) and not _verdict_passed(text)


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
      - T4.3 节点级：进模型前按 `NODE_TOKEN_BUDGET` 剪枝（先砍最老的工具结果）；
      - T4.3 任务级：累计 token 已击穿 `TASK_TOKEN_BUDGET` → **直接终止并报告**。
    ⚠️ **阶段 8 · P2 起，这两个额度是运行期按模型窗口算的**（`config.token_budgets()`），
    不再是 import 期常量 —— 换模型后不用重启就生效。

    **阶段 8 · P0 在这里加了停止**（协作式取消，三道检查点里的两道）：
      - **节点入口**：进来先看停止开关（"还没开始执行"就该一步都不跑）；
      - **ReAct 每一步**：`astream` 循环开头 —— 停止后**下一步不再开始**；
      - 第三道在工具调用层（`utils/tool_wrap.py::_process`），兜住"这一步里的下一次调用"。
    ⚠️ **正在飞的那一次模型调用不会被打断**（协作式的固有代价）：它返回后我们才发现已停止。
    """
    # 阶段 8 · P2：额度**运行期**解析（按当前 executor 模型的窗口；env 显式设了就用 env）
    budgets = token_budgets()
    # ── 阶段 8 · P0：① 节点入口的停止检查（与"预算击穿"分开：停是"人/时间"，击穿是"钱"）──
    token = current_token()
    if token is not None:
        token.stage = "executor"
        token.usage_limit = budgets.task
        token.tokens = state.get("token_usage", 0)
    stopped = _stopped()
    if stopped is not None:
        # 停止可能是在**上一个节点**就被发现的（例如 Planner 入口）—— 沿用那个 stage，
        # 文案才准确（"在规划阶段就停了" vs "开始执行前就停了"）。
        stage = str(state.get("cancel_stage") or "executor")
        logger.info("任务已停止（%s）：跳过 Executor 本次执行", stopped.reason)
        # 事件仍然成对（start → end），前端能正常收起"执行中"
        await emit({"type": "node", "node": "executor", "status": "start"})
        await emit(
            {
                "type": "node",
                "node": "executor",
                "status": "end",
                "steps": 0,
                "cancelled": True,
                "cancelReason": stopped.reason,
            }
        )
        return _cancel_stop_result(state, stopped, stage=stage)

    # ⚠️ 阶段 8 · P1（F3）：`is_retry` **要在所有终止分支之前算好**。
    #    `retry_count` 的语义是「已经打回了多少次」—— 只要这一轮**是**被打回后发起的，
    #    无论它是跑完了、被预算掐断、还是被停止，都得计一次；
    #    否则卡片上会写「打回 0 次」，而实际上明明被打回过（现场：重跑轮撞预算 ⇒ 显示 0 次）。
    prev_verdict = str(state.get("verdict") or "")
    is_retry = _is_retry_round(prev_verdict)
    retry_now = state.get("retry_count", 0) + (1 if is_retry else 0)

    tokens_used = state.get("token_usage", 0)
    if over_task_budget(tokens_used, budgets.task):
        logger.warning(
            "任务级 token 预算击穿：已用 %d / 上限 %d，主动终止", tokens_used, budgets.task
        )
        prior_trace = list(state.get("executor_trace_list") or [])
        return {
            "executor_result": _budget_message(
                total_tokens=tokens_used,
                step_count=0,
                tool_calls=len(prior_trace),
                limit=budgets.task,
            ),
            "budget_exceeded": True,
            "retry_count": retry_now,
            "executor_trace": _trace_to_text(prior_trace),
            "executor_trace_list": prior_trace,
            "executor_messages": [],
            "step_count": state.get("step_count", 0),
        }

    plan_steps = _plan_to_text(state.get("plan", ""))
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
    # ⚠️ **本轮之前已经发生过的执行轨迹**（修 D2）：被打回重跑时 state 里已经有上一轮的 trace，
    #    这里先取出来，最后和本轮合并 —— 否则记录里只剩最后一次（见 D2 注释）。
    prior_trace = list(state.get("executor_trace_list") or [])
    tokens = 0
    messages: list = []
    step_count = 0
    # 跨轮记忆来自 state["messages"]（checkpointer 按 thread_id 恢复，add_messages 负责累积）
    prior_turns = list(state.get("messages") or [])
    raw_input = [*prior_turns, HumanMessage(content=user_msg)]
    input_messages = prune_messages(raw_input, budgets.node)
    pruned = len(raw_input) - len(input_messages)
    if pruned:
        logger.info(
            "节点级剪枝：prompt 估算 %d token 超过预算 %d，砍掉 %d 条最老消息",
            estimate_messages_tokens(raw_input),
            budgets.node,
            pruned,
        )
    budget_hit = False
    cancelled_hit = False
    spent_before = state.get("token_usage", 0)
    await emit({"type": "node", "node": "executor", "status": "start", "retry": is_retry})
    try:
        async for chunk in executor_agent.astream(
            {"messages": input_messages}, config={"recursion_limit": 100}
        ):
            # ── 阶段 8 · P0：② **ReAct 每一步**的停止检查 ──
            # 放在最前面：这一块（本步的模型输出 + 它要调的工具）**一个都不执行**。
            # 停止是协作式的 ⇒ 正在飞的那次模型调用拦不住，但它之后的这一步一定不会开始。
            if token is not None and token.cancelled:
                cancelled_hit = True
                logger.info(
                    "任务已停止（%s）：在第 %d 步后停下（本步未执行）", token.reason, step_count
                )
                break
            step_count += 1
            step_tools: list[str] = []
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
                            step_tools.append(tc.get("name", "?"))
            if token is not None:
                token.steps = state.get("step_count", 0) + step_count
                token.tokens = spent_before + tokens
            # T5.6：每步推一条进度（带本步调了哪些工具），界面上就能看到"正在读哪个文件/查哪张表"
            await emit(
                {
                    "type": "node",
                    "node": "executor",
                    "status": "step",
                    "step": step_count,
                    "tools": step_tools,
                }
            )
            # 阶段 8 · P0：任务状态条（时长 / 步数 / 用量；没绑停止开关时不发）
            await _emit_status()
            # ⚠️ 任务级预算**必须在这里也要判**：单个 executor 节点内部的 ReAct 循环
            #    是不经过图节点边界的，只在节点入口判的话，一次"读大文件 + 反复重读"
            #    就能在**一次**节点调用里烧掉十几万 token 而永远不触发上限（实测 127,071）。
            if over_task_budget(spent_before + tokens, budgets.task):
                budget_hit = True
                logger.warning(
                    "任务级 token 预算击穿：本节点已用 %d（累计 %d / 上限 %d），终止 ReAct 循环",
                    tokens,
                    spent_before + tokens,
                    budgets.task,
                )
                break
    except TaskCancelled as exc:
        # ③ 第三道检查点在工具调用层（`tool_wrap._process`）：那一句抛出的 `TaskCancelled`
        #    会穿过 LangGraph 的 ToolNode（它 `except Exception` 抓不到 BaseException）
        #    直接解栈到这里 —— 于是"这一步剩下的工具调用"一个都不会执行。
        cancelled_hit = True
        logger.info("任务已停止（%s）：第 %d 步的工具调用被拦下", exc.reason, step_count)

    if cancelled_hit:
        await emit(
            {
                "type": "node",
                "node": "executor",
                "status": "end",
                "steps": step_count,
                "cancelled": True,
                "cancelReason": token.reason if token is not None else "",
                "tokens": tokens,
            }
        )
        return _cancel_stop_result(
            state,
            token,
            stage="executor",
            step_count=step_count,
            tokens_spent=tokens,
            trace=trace,
            messages=messages,
            last_content=last_content,
            pruned=pruned,
            # 阶段 8 · P1（F3）：被停止的这一轮若是重跑，同样要计一次打回
            is_retry=is_retry,
        )

    if budget_hit:
        await emit(
            {
                "type": "node",
                "node": "executor",
                "status": "end",
                "steps": step_count,
                "budgetExceeded": True,
                "tokens": tokens,
            }
        )
        return {
            "executor_result": _budget_message(
                total_tokens=spent_before + tokens,
                # 口径与停止一致：报**本轮累计**步数（不是本次节点调用的局部计数）
                step_count=state.get("step_count", 0) + step_count,
                tool_calls=len(prior_trace) + len(trace),
                limit=budgets.task,
                last=last_content,
            ),
            "budget_exceeded": True,
            "retry_count": retry_now,
            # ⚠️ **跨轮累积**（修 D2）：重跑被预算掐断时，若只返回本轮 trace，
            #    前几轮真正干活的轨迹就被**覆盖**了 —— 实测 E007 因此"表建好了、3 行数据也插了，
            #    但轨迹断言只看见 mysql_create_database 一条" ⇒ 计分假阴性。
            "executor_trace": _trace_to_text(prior_trace + trace),
            "executor_trace_list": prior_trace + trace,
            "executor_messages": messages,
            "step_count": state.get("step_count", 0) + step_count,
            "token_usage": spent_before + tokens,
            "pruned_messages": state.get("pruned_messages", 0) + pruned,
        }

    await emit(
        {"type": "node", "node": "executor", "status": "end", "steps": step_count, "tokens": tokens}
    )
    return {
        "executor_result": last_content or "（Executor 未产出最终回复）",
        # ⚠️ **跨轮累积**（修 D2）：同一次任务的多次执行（被打回重跑）应当**合并**轨迹，
        #    否则记录里只剩最后一次 —— 会漏掉前面几轮真正完成的动作。
        "executor_trace": _trace_to_text(prior_trace + trace),
        "executor_trace_list": prior_trace + trace,
        "executor_messages": messages,
        "step_count": state.get("step_count", 0) + step_count,
        "retry_count": retry_now,
        "token_usage": spent_before + tokens,
        "pruned_messages": state.get("pruned_messages", 0) + pruned,
    }


async def verifier_node(state: AgentState, verifier_agent: Any) -> dict:
    """只读验收：对照需求+计划+执行轨迹，输出 PASS/FAIL + 原因。"""
    token = current_token()
    if token is not None:
        token.stage = "verifier"
    await emit({"type": "node", "node": "verifier", "status": "start"})
    # ── 阶段 8 · P0：节点入口的停止检查 ──
    stopped = _stopped()
    if stopped is not None:
        logger.info("任务已停止（%s）：跳过 Verifier 验收", stopped.reason)
        await emit(
            {
                "type": "node",
                "node": "verifier",
                "status": "end",
                "cancelled": True,
                "cancelReason": stopped.reason,
                "tokens": 0,
            }
        )
        # ⚠️ **不动 `executor_result`**：产物已经产出了，停止只是"不再验收"，
        #    把 Executor 的结论丢掉反而会让用户以为白跑了。
        return {
            "cancelled": True,
            "cancel_reason": stopped.reason,
            "cancel_stage": "verifier",
            "token_usage": state.get("token_usage", 0),
        }
    await _emit_status()
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
    cancelled_hit = False
    try:
        async for chunk in verifier_agent.astream(
            {"messages": [HumanMessage(content=prompt)]}, config={"recursion_limit": 20}
        ):
            # 阶段 8 · P0：验收内部的每一步同样受停止约束
            if token is not None and token.cancelled:
                cancelled_hit = True
                break
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
    except TaskCancelled as exc:
        # 工具层抛出的停止（理由同 executor_node 里的同一段注释）
        cancelled_hit = True
        logger.info("任务已停止（%s）：Verifier 的工具调用被拦下", exc.reason)

    if token is not None:
        token.tokens = state.get("token_usage", 0) + tokens
    if cancelled_hit:
        await emit(
            {
                "type": "node",
                "node": "verifier",
                "status": "end",
                "cancelled": True,
                "cancelReason": token.reason if token is not None else "",
                "tokens": tokens,
            }
        )
        return {
            "cancelled": True,
            "cancel_reason": token.reason if token is not None else "",
            "cancel_stage": "verifier",
            "verifier_trace_list": trace,
            "verifier_messages": messages,
            "token_usage": state.get("token_usage", 0) + tokens,
        }
    await _emit_status()
    parsed = _extract_json(verdict_text)
    if parsed and parsed.get("verdict"):
        verdict_text = json.dumps(parsed, ensure_ascii=False)
    await emit(
        {
            "type": "node",
            "node": "verifier",
            "status": "end",
            "passed": str((parsed or {}).get("verdict", "")).upper() == "PASS",
            "tokens": tokens,
        }
    )
    return {
        "verdict": verdict_text,
        "verifier_trace_list": trace,
        "verifier_messages": messages,
        "token_usage": state.get("token_usage", 0) + tokens,
    }


def decide_after_verify(state: AgentState) -> Literal["executor", "end"]:
    """条件边：PASS → 结束；FAIL 且未超限 → 打回 Executor；超限 → 结束。

    另外三条**必须先判**的终止条件：
      - 任务被**停止**（阶段 8 · P0：用户点停止 / 墙钟到点）→ 结束，绝不打回重跑；
      - 任务级 token 预算击穿（T4.3）→ 直接结束，别再打回重跑烧 token；
      - 击穿过一次之后同样直接结束。
    """
    if state.get("cancelled"):
        return "end"
    if state.get("budget_exceeded"):
        return "end"
    if _verdict_passed(state.get("verdict", "")):
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
    await emit({"type": "node", "node": "route", "status": "start"})
    route = route_task(state)
    await emit({"type": "node", "node": "route", "status": "end", "route": route})
    return {"route": route}


def _route_decide(state: AgentState) -> Literal["simple", "complex"]:
    """纯函数：只读 route_node 写下的结论来选边（缺省保守走 complex）。"""
    return state.get("route", "complex")  # type: ignore[return-value]


def after_executor(state: AgentState) -> Literal["verifier", "end"]:
    """Executor 的收尾：simple 路径直接结束，complex 路径去验收。

    两条**必须先结束**的情形（阶段 8 · P0 把这条判据统一给 multi 模式也用上了）：
      - 任务被**停止**：停止的语义就是"后面不再继续"，再去跑验收等于没停；
      - **预算击穿**（T4.3）：击穿时 Executor 根本没执行完，再去跑 Verifier 只是白花一次模型调用。
         ⚠️ 这条以前只在 `auto` 模式下成立 —— `multi` 模式的 `executor → verifier` 是**无条件边**，
         于是限额版评估里 E015 被掐断后照样调了一次 Verifier（阶段 6 的教训），P1 的账也是这一条。
    """
    if state.get("cancelled") or state.get("budget_exceeded"):
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
        # ⚠️ 走 prompt_context()：model_name 要**运行期**取（订正 #37），
        #   直接用静态 PROMPT_CONTEXT 会 KeyError —— 故意让它响亮地失败
        prompt=prompt.format(**prompt_context("executor")),
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
        # ⚠️ 阶段 8 · P0：这里以前是**无条件边** `executor → verifier` ⇒
        #    被"停止"或被"预算"终止之后**照样会跑一次验收**（白花一次模型调用，
        #    而且会把"停止"包装成"验收未通过"）。现在与 auto 模式统一走 `after_executor`。
        graph.add_conditional_edges(
            "executor", after_executor, {"verifier": "verifier", "end": END}
        )
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


def _interrupt_reason(exc: BaseException) -> str:
    """把"为什么中断"翻译成给人看的一句中文。"""
    if isinstance(exc, asyncio.CancelledError):
        return "连接断开或任务被取消（例如页面被关闭 / 刷新）"
    return f"任务被中断（{type(exc).__name__}: {exc}）"


def _schedule_interruption_record(thread_id: str, user_input: str, reason: str) -> None:
    """fire-and-forget：把"被中断的那一轮"补进跨轮记忆（见 `_record_interrupted_turn`）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover —— 没有事件循环（纯同步调用）时静默跳过
        return
    loop.create_task(_record_interrupted_turn(thread_id, user_input, reason))


async def _record_interrupted_turn(thread_id: str, user_input: str, reason: str) -> None:
    """往线程记忆里补一对 `(用户说了什么, 本轮被中断)`。

    ⚠️ 为什么要补：跨轮记忆的追加原本只在"图跑完之后"发生 ⇒ **没跑完 = 整轮消失**，
    下一轮的模型既看不到用户说过什么，也不知道上一轮没做完（会接着上一轮的旧状态乱猜）。
    """
    if not thread_id or not user_input:
        return
    note = (
        "【本轮被中断】" + reason + "，这个任务**没有跑完**。\n"
        "已经发生的副作用（写好的文件 / 建好的目录 / 库里的数据）一律保留，不做回滚。\n"
        "如需继续，请重新发一次任务（或把范围缩小后再发）。"
    )
    try:
        CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINT_DB)) as saver:
            app = build_graph(None, None, checkpointer=saver, mode="single")
            config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
            await app.aupdate_state(
                config,
                {"messages": [HumanMessage(content=user_input), AIMessage(content=note)]},
            )
    except Exception as exc:  # noqa: BLE001 —— 补记失败不该影响任何主流程
        logger.warning("中断补记失败（忽略）：%s: %s", type(exc).__name__, exc)


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

    **阶段 8 · P0 —— 停止怎么接**：本函数**不自己造停止开关**；调用方在**外面**用
    `with bind_cancel(CancelToken(wall_clock=...))` 绑一次（Web 的每条消息、CLI 的每一轮各绑一次）。
    没绑 ⇒ 这个入口不参与停止（evals / 单测零改动）。停止命中时返回值里
    `cancelled=True` / `cancel_reason` / `cancel_stage` 会说清"谁停的、停在哪"。

    返回: {plan, executor_result, executor_trace, verdict, retry_count, token_usage,
           final_response, mode, route, knowledge_injected, compacted, budget_exceeded,
           pruned_messages, deposited, cancelled, cancel_reason, cancel_stage, status,
           needs_clarification, placeholders}
    """
    if executor_agent is None:
        executor_agent = build_executor_agent(all_tools, mode=mode)
    if verifier_agent is None:
        verifier_agent = build_verifier_agent(all_tools)

    # ── 阶段 8 · P1.5（候选池 §十五B）：入口的"模板未渲染"检测 ──
    # 任务里出现没替换的占位符（`<你的WSL用户名>` / `${YOUR_PATH}` / `你的API密钥` …）
    # ⇒ **只回问、不进图**：0 次模型调用、0 次工具调用。
    # 判据只认"形状 + 占位词"，**单花括号一律不管**（JSON / 字典 / f-string 太常见）——
    # 细节与那两道会被误伤的评估题（E016 / E029）见 `utils/placeholder_guard.py`。
    placeholders = find_placeholders(user_input)
    if placeholders and not is_explicitly_literal(user_input):
        reply = clarify_reply(placeholders)
        logger.info("任务里有未替换的模板占位符 %s：只回问、不进图", placeholders)
        # ⚠️ 仍然把这一轮 (任务, 回问) 写回线程记忆 —— 否则下一轮的历史里看不到"我问过什么"
        CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINT_DB)) as saver:
            # 图只用来写记忆：两个 agent 传 None（永远不会被调用到）
            app = build_graph(None, None, checkpointer=saver, mode="single")
            config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
            await app.aupdate_state(
                config,
                {"messages": [HumanMessage(content=user_input), AIMessage(content=reply)]},
            )
        return {
            "plan": "",
            "executor_result": reply,
            "executor_trace": "",
            "verdict": "",
            "retry_count": 0,
            "mode": mode,
            "route": "",
            "token_usage": 0,
            "executor_trace_list": [],
            "verifier_trace_list": [],
            "executor_messages": [],
            "verifier_messages": [],
            "step_count": 0,
            "final_response": reply,
            "knowledge_injected": [],
            "compacted": False,
            "budget_exceeded": False,
            "pruned_messages": 0,
            "deposited": [],
            "cancelled": False,
            "cancel_reason": "",
            "cancel_stage": "",
            "status": None,
            # P1.5：前端据此显示「需要你确认」，而不是「未验收」
            "needs_clarification": True,
            "placeholders": placeholders,
            "models_used": {"planner": [], "executor": [], "verifier": []},
        }

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
        # 阶段 8 · P2：压实阈值也**运行期**解析（按当前模型的窗口）
        budgets = token_budgets()
        snapshot = await app.aget_state(config)
        prior = list((snapshot.values or {}).get("messages") or [])
        if prior and estimate_messages_tokens(prior) > budgets.compact:
            new_history, compacted = await compact_history(
                prior, registry.chain("executor"), threshold=budgets.compact
            )
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

        try:
            state = await app.ainvoke(
                # 只传增量：历史在 checkpoint 里，靠 messages 的 add_messages reducer 累积。
                # ⚠️ **每轮通道全部复位**（见 `PER_TURN_RESET` 的说明）：不复位 = 上一轮的值串到本轮
                #    （实测撞过：旧计划被当成本轮计划、轨迹跨轮累加、新一轮被误判成重跑轮）。
                {
                    **per_turn_reset(),
                    "user_input": user_input,
                    "knowledge": knowledge_text,
                },
                config=config,
            )
        except TaskCancelled as exc:
            # ── 阶段 8 · P0 的**兜底**（正常路径用不到）──
            # 三道检查点里，工具层那道抛出的 `TaskCancelled` 要穿过**整张图**才能到节点里；
            # 各节点都接了，但万一将来新增节点/子图没接住，绝不能让它变成一次"未处理异常"
            # —— 那对 Web 端就是"任务永远不回包"（比停止失败更糟）。
            # 这里把最后一次 checkpoint 的值捞回来，按同一条停止路径收尾。
            logger.warning("停止信号穿出了整张图（%s）：按停止路径收尾", exc.reason)
            snapshot = await app.aget_state(config)
            values = dict(snapshot.values or {})
            state = {
                **values,
                **_cancel_stop_result(
                    values,
                    current_token(),
                    stage=str(values.get("cancel_stage") or "executor"),
                    tokens_spent=0,
                ),
            }
        except BaseException as exc:  # noqa: BLE001
            # ── 阶段 8 实测修复（B5）：**被硬中断的一轮也必须留档** ──
            # 现场：用户在执行中关掉页面 ⇒ WS 断开 ⇒ 这个任务被 `cancel()` ⇒ 图没跑完 ⇒
            # 原来那句"只在图跑完后写一次"的记忆追加**根本没执行** ⇒ 记忆里连"他说过什么"都没有。
            # ⚠️ 这里**只补一条记录，然后把异常原样抛出去**（绝不吞）：Web 端要靠 `CancelledError`
            #    正常收尾，吞掉它会让连接永远挂着。
            # ⚠️ 补记用**另起一个 task**（fire-and-forget）：当前任务已在取消流程里，
            #    任何 `await` 都会被立刻再取消一次；主循环还活着，所以新 task 能跑完。
            _schedule_interruption_record(thread_id, user_input, _interrupt_reason(exc))
            raise

        cancelled = bool(state.get("cancelled"))
        cancel_reason = str(state.get("cancel_reason") or "")
        cancel_stage = str(state.get("cancel_stage") or "")
        final_response = state["executor_result"]
        verdict_text = str(state.get("verdict") or "")
        if cancelled:
            # 阶段 8 · P0：**停止的文案必须是"已停止"**，绝不能拼成"验收未通过"
            # （第 2 题现场就是因预算终止却显示"验收未通过 + 一句英文" —— 账本 R5）。
            # 停在哪一段，说法不一样：
            #   · executor 停 ⇒ 那段中文说明由 `_cancel_stop_result` 产出，直接用；
            #   · planner 停  ⇒ 一步都没跑，同样由 executor 的入口检查产出（planner 后面必然是它）；
            #   · verifier 停 ⇒ 产物已经产出了，**保留 Executor 的结论**，只在前面加一句"没验收"。
            if cancel_stage == "verifier":
                final_response = (
                    "【验收已停止】任务在验收环节被停止，因此这一轮**没有验收结论**。\n"
                    "以下是 Executor 已经产出的结果（副作用均已保留）：\n\n"
                    f"{state['executor_result']}"
                )
        elif state.get("budget_exceeded"):
            # 阶段 8 · P1（F3 / 账本 R5 的真实形状）：**被预算掐断的那一轮不该被包装成验收失败**。
            # 触发路径：第 1 轮跑完 → Verifier 判 FAIL（`verdict` 留在 state 里）→ 打回重跑 →
            # 第 2 轮撞上预算 ⇒ 若走下面那条 `elif`，最终回复会变成
            # 「任务执行完成，但验收未通过（已重试 1 次）：验收意见：… 执行结果：【已终止·预算】…」
            # —— 正是第 2 题现场那句"看不懂的混合文案"。
            # 语义上也说不通：那一轮**根本没验收**，谈不上"验收未通过"。
            final_response = state["executor_result"]
        elif verdict_text.strip() and not _verdict_passed(verdict_text):
            # ⚠️ 判据走 `_verdict_passed`（修 D1）：裁定**无法解析**时也算"未通过"，
            #    否则上游返回错误串时会被当成"没失败"，回复里既不提示、也照样算成功。
            parsed = _extract_json(verdict_text)
            reason = (parsed or {}).get("reason", verdict_text)
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
    # ⚠️ 阶段 5（订正 #28）：**不再传 save_tool** —— 沉淀改回进程内直调 `store.save_document`，
    #    不走 MCP 工具（一是对齐 memory.py 的 docstring，二是绕开订正 #27 那个死锁）。
    #    它也不再走三档权限的人工确认（用户决策 B），但仍受"只读档不写"约束。
    deposited: list[dict] = []
    # ⚠️ 判据走 `_verdict_passed`（修 D1）：**没有裁定**（single / auto-simple 路径）算成功（保持原行为），
    #    **有裁定就必须是 PASS** 才算成功 —— 裁定无法解析（上游错误串）时**不该沉淀经验**。
    # 阶段 8 · P0：被**停止**的任务同样不沉淀（半途而废的"经验"存进去就是噪音）。
    verdict_text = str(state.get("verdict") or "")
    succeeded = (
        (not verdict_text.strip() or _verdict_passed(verdict_text))
        and not state.get("budget_exceeded")
        and not cancelled
    )
    if succeeded:
        deposited = await maybe_deposit_knowledge(
            user_input,
            final_response,
            chain=registry.chain("executor"),
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
        # ── 阶段 8 · P0：停止（谁停的 / 停在哪 / 到这一刻的状态）──
        "cancelled": cancelled,
        "cancel_reason": cancel_reason,
        "cancel_stage": cancel_stage,
        # 状态条口径（时长**不含人工确认等待**）：没有停止开关时是 None
        "status": (current_token().snapshot() if current_token() is not None else None),
        # ── 阶段 6：**本轮实际用了哪些模型**（服务端回报的名字，不是配置里的）──
        "models_used": {
            "planner": [m for m in [state.get("planner_model") or ""] if m],
            "executor": _models_of(state.get("executor_messages")),
            "verifier": _models_of(state.get("verifier_messages")),
        },
    }
