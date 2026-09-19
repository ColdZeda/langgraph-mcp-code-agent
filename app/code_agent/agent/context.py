"""上下文管理（阶段 4 · T4.1 工具结果外置 / T4.2 对话压实 / T4.3 token 预算）。

为什么需要这个模块：

1. **上下文爆炸** —— 一次任务读 20 个文件，长工具输出会**永久占着** context 直到任务结束；
2. **token 随步数平方增长** —— LLM API 无状态，每次调用都要重发全部历史，
   N 步时累计输入 ≈ O(N²)（实测单 Agent 单题 token 中位 23,678 / max 150,826，max 是中位的 6.4 倍）；
3. **跨轮记忆**在阶段 1 接了 checkpointer，但**恢复的是完整历史** —— 不管的话 context 只会越来越长。

本模块只做**纯逻辑**（不碰 registry / 工具加载），便于单测：
LLM 通过参数传入（`chain`），不外呼。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage

from app.code_agent.config import (
    COMPACT_KEEP_MESSAGES,
    COMPACT_THRESHOLD_TOKENS,
    EXTERNALIZE_EXEMPT_TOOLS,
    EXTERNALIZE_MAX_LINES,
    EXTERNALIZE_PREVIEW_CHARS,
    EXTERNALIZE_THRESHOLD,
    RUNTIME_DIR,
    TOOL_RESULTS_DIR,
)
from app.code_agent.model.llm import invoke_with_fallback

# ═══════════════════════════════════════════════════════════════════
# T4.1 · 工具结果外置
# ═══════════════════════════════════════════════════════════════════


def _content_of(msg: Any) -> str:
    """把消息内容统一取成字符串（content 可能是 str，也可能是 content blocks 列表）。"""
    if isinstance(msg, dict):
        msg = msg.get("content", "")
    else:
        msg = getattr(msg, "content", msg)
    if isinstance(msg, str):
        return msg
    return str(msg)


def should_externalize(content: str, tool_name: str = "") -> bool:
    """是否该外置：字符数超阈值，**或**行数超兜底上限。

    行数兜底的理由：有些输出单行很短、总字符数不超标，但行数极多（长清单），
    token 消耗同样可观。

    ⚠️ **豁免"取内容"类工具**（默认 `read_file_range` / `read_file`）：
    它们的输出就是模型点名要的那份内容，藏起来只会逼模型改用"分段读"绕过去。
    实测同一道"读全文并总结"的题：不外置 17,361 token；外置后 **127,071 token（7.3 倍）**，
    因为模型为了拿到内容读了 12 次、跑了 25 步。
    详见 `config.EXTERNALIZE_EXEMPT_TOOLS` 的注释。
    """
    if tool_name in EXTERNALIZE_EXEMPT_TOOLS:
        return False
    if len(content) > EXTERNALIZE_THRESHOLD:
        return True
    return content.count("\n") + 1 > EXTERNALIZE_MAX_LINES


def externalize_tool_result(tool_name: str, content: str) -> str:
    """超长工具结果外置到文件，返回给 context 用的瘦身版本。

    不改内容的语义：完整原文原样落盘，**路径可以用 `read_file_range` 读回来**。
    """
    if not should_externalize(content, tool_name):
        return content

    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", tool_name)
    path = TOOL_RESULTS_DIR / f"{safe_name}-{digest}.txt"
    # 同名同内容 → 幂等（同一份内容重复外置不产生新文件）
    if not path.exists():
        path.write_text(content, encoding="utf-8")

    # ⚠️ 返回**相对项目根**的路径：实测 read_file_range 接受相对路径、按 Path.cwd() 解析，
    #    而 CLI/Web/evals 三种入口的 cwd 都是项目根。
    rel = path.relative_to(RUNTIME_DIR.parent).as_posix()
    line_count = content.count("\n") + 1
    preview = content[:EXTERNALIZE_PREVIEW_CHARS]
    return (
        f"[工具结果已外置] {tool_name} 返回 {len(content)} 字符 / {line_count} 行，"
        f"完整内容已存到 {rel}（需要时可再用 read_file_range 读取）\n"
        f"--- 前 {EXTERNALIZE_PREVIEW_CHARS} 字符预览 ---\n{preview}\n--- 预览结束 ---"
    )


# ═══════════════════════════════════════════════════════════════════
# T4.3 · token 估算与预算
# ═══════════════════════════════════════════════════════════════════

# 中日韩统一表意文字（本项目用户可见字符串是中文，中文大致 1 字 ≈ 1 token）
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]")


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（**不引第三方 tokenizer**，只要量级对）。

    口径：CJK 字符按 1 token/字；其余字符按 4 字符/token（英文经验值）。
    实测校准见 tests/test_context.py 的用例（对已知长度文本断言量级）。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def estimate_messages_tokens(messages: list) -> int:
    """估算整个消息列表的 token（消息本身有约 4 token 的角色/分隔开销）。"""
    return sum(estimate_tokens(_content_of(m)) + 4 for m in messages)


def over_task_budget(token_usage: int, budget: int) -> bool:
    """任务级硬上限判定。budget <= 0 视为不限制。"""
    return budget > 0 and token_usage >= budget


def prune_messages(messages: list, budget: int, *, keep_last: int = 4) -> list:
    """节点级剪枝：估算超预算时砍消息，直到达标。

    策略（按"信息密度最低"优先）：
      1. 先砍**最老的 ToolMessage**（工具原始输出最容易重新获取，且通常最长）；
      2. 没有可砍的工具消息了，再从最老的开始砍；
      3. **最近 `keep_last` 条永远保留**（当前任务与最新工具结果不能丢）。
    """
    if budget <= 0:
        return list(messages)

    msgs = list(messages)
    while len(msgs) > keep_last and estimate_messages_tokens(msgs) > budget:
        cut = next((i for i, m in enumerate(msgs[:-keep_last]) if isinstance(m, ToolMessage)), None)
        if cut is None:
            cut = 0
        msgs.pop(cut)
    return msgs


# ═══════════════════════════════════════════════════════════════════
# T4.2 · 对话压实（四段式摘要）
# ═══════════════════════════════════════════════════════════════════

SUMMARY_PROMPT = """把下面的对话历史压缩成四段式摘要，保留所有关键信息：

【目标】用户最终要实现什么
【约束】用户提过的限制（技术栈、不许做什么、偏好）
【已完成】做过哪些操作、产生了什么产物
【未决】还没解决的问题

对话历史：
{history}

只输出这四段，不要其他内容。"""

SUMMARY_MARKER = "【历史摘要】"
# 摘要前每条消息最多喂多少字符（避免"为了省 token 先把 token 烧光"）
_SUMMARIZE_MSG_CHARS = 2000


def _history_to_text(messages: list) -> str:
    lines = []
    for m in messages:
        role = getattr(m, "type", None) or (m.get("role", "?") if isinstance(m, dict) else "?")
        lines.append(f"[{role}] {_content_of(m)[:_SUMMARIZE_MSG_CHARS]}")
    return "\n".join(lines)


def _summary_message(summary: str) -> BaseMessage:
    """摘要用 HumanMessage 承载：任何 provider 都支持，且不会被当成 system 指令覆盖。"""
    return HumanMessage(content=f"{SUMMARY_MARKER}\n{summary}")


async def compact_history(messages: list, chain: list) -> tuple[list, bool]:
    """超阈值时压实历史。

    返回 `(新消息列表, 是否真的压实了)`：
      - 未超阈值 / 消息太少 / 摘要失败 → 原样返回，第二项为 False
        （**摘要失败必须原样返回**，绝不能因为省 token 把历史弄丢）；
      - 成功 → `[摘要消息] + 最近 COMPACT_KEEP_MESSAGES 条`，第二项为 True。

    压缩的是**最老的那一段**，最近的消息逐字保留 —— 越近的信息越不能糊。
    """
    if not messages:
        return list(messages), False
    if estimate_messages_tokens(messages) <= COMPACT_THRESHOLD_TOKENS:
        return list(messages), False
    if len(messages) <= COMPACT_KEEP_MESSAGES:
        return list(messages), False

    old = list(messages[:-COMPACT_KEEP_MESSAGES])
    recent = list(messages[-COMPACT_KEEP_MESSAGES:])
    # 上一轮的摘要会被一起再摘要（它就在 old 里），信息不会丢
    prompt = SUMMARY_PROMPT.format(history=_history_to_text(old))

    try:
        resp = await invoke_with_fallback(
            chain,
            [HumanMessage(content=prompt)],
        )
    except Exception:
        return list(messages), False

    summary = _content_of(resp).strip()
    if not summary:
        return list(messages), False
    return [_summary_message(summary), *recent], True


__all__ = [
    "compact_history",
    "estimate_messages_tokens",
    "estimate_tokens",
    "externalize_tool_result",
    "over_task_budget",
    "prune_messages",
    "should_externalize",
]
