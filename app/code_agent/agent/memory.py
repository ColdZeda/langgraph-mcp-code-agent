"""T4.4 ② 自动注入 + ③ 自动沉淀（分层记忆的读写两端）。

- **自动注入**：任务开始时自动检索一次相关知识，拼进 Executor 的输入 ——
  现在模型得**主动调** `query_rag` 才知道有相关知识，它经常忘。
- **自动沉淀**：任务成功后让 LLM 判断"这次有没有值得长期记住的经验"，
  有就写进知识库。**不是什么都存** —— 噪音会拉低检索质量。

⚠️ 两个都在**Agent 进程内**直接调 `rag/store.py`，不走 MCP 工具：
MCP 工具每次调用都会新起 python 子进程（重新 import chromadb + torch），
延迟从毫秒级变秒级；而这里要求的是"任务开始前顺手查一下"。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.code_agent.config import RAG_AUTO_DEPOSIT, RAG_AUTO_INJECT, RAG_TOP_K

logger = logging.getLogger(__name__)

# 太短 / 纯寒暄就不查（省一次检索，也避免把"你好"当查询词）
MIN_QUERY_CHARS = 10
CHITCHAT = {"你好", "您好", "hi", "hello", "在吗", "在么", "谢谢", "thanks", "test", "测试"}

INJECT_HEADER = "【相关经验】"


# ═══════════════════════════════════════════════════════════════════
# ② 自动注入
# ═══════════════════════════════════════════════════════════════════


def is_worth_searching(user_input: str) -> bool:
    """该不该为这次输入检索知识库。"""
    text = (user_input or "").strip()
    if len(text) < MIN_QUERY_CHARS:
        return False
    return text.lower() not in CHITCHAT


def inject_relevant_knowledge(user_input: str, *, top_k: int = RAG_TOP_K) -> tuple[str, list[dict]]:
    """任务开始时自动检索相关知识。

    返回 `(要注入的文本, 命中的条目)`；文本为空字符串表示不注入。
    **任何失败都只降级为"不注入"**（知识检索不该让任务跑不起来）。
    """
    if not RAG_AUTO_INJECT:
        return "", []
    if not is_worth_searching(user_input):
        return "", []

    try:
        from app.code_agent.rag import store

        store.ensure_seeded()
        items = store.search_knowledge(user_input.strip(), top_k=top_k)
    except Exception as exc:  # noqa: BLE001
        logger.warning("自动注入失败（忽略，继续任务）：%s: %s", type(exc).__name__, exc)
        return "", []

    if not items:
        return "", []

    lines = [f"- {it['text']}" for it in items]
    text = f"{INJECT_HEADER}\n" + "\n".join(lines)
    logger.info("注入了 %d 条相关经验：%s", len(items), [it["id"] for it in items])
    return text, items


# ═══════════════════════════════════════════════════════════════════
# ③ 自动沉淀
# ═══════════════════════════════════════════════════════════════════

DEPOSIT_PROMPT = """判断下面这次任务里，有没有**值得长期记住的经验**（下次遇到同类任务能省事的知识）。

任务：
{user_input}

执行结果：
{result}

判断标准（**宁缺毋滥**，知识库被噪音污染后检索质量会下降）：
- 值得存：环境事实（端口 / 路径 / 账号约定）、踩过的坑与规避方法、
  项目特有的规范或流程（例如"跑评估前必须先清 checkpoint 残留"）。
- 不值得存：闲聊、用户个人信息（名字 / 偏好）、本次任务的具体产物内容、
  一次性数据、复述任务本身。

只输出严格 JSON，不要其他内容：
{{"save": true 或 false, "items": [{{"title": "简短标题（用作文件名）", "content": "经验正文"}}]}}

最多 2 条；save 为 false 时 items 必须是空数组。"""


def _extract_json(text: str) -> dict | None:
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


async def maybe_deposit_knowledge(
    user_input: str,
    result: str,
    *,
    chain: list,
    save_tool: Any | None = None,
    enabled: bool | None = None,
) -> list[dict]:
    """任务成功后判断并沉淀经验。

    ⚠️ **可关闭**（`CODE_AGENT_RAG_AUTO_DEPOSIT=0` 或显式传 `enabled=False`）：
    评估时必须关掉 —— 否则评测过程产生的临时经验会改写知识库，
    进而改变后续题目的检索结果（同一批数据前后不可比）。
    """
    if enabled is None:
        enabled = RAG_AUTO_DEPOSIT
    if not enabled:
        return []
    if save_tool is None:
        logger.debug("没有 save_knowledge 工具，跳过自动沉淀")
        return []

    from langchain_core.messages import HumanMessage

    from app.code_agent.model.llm import invoke_with_fallback

    prompt = DEPOSIT_PROMPT.format(user_input=user_input[:2000], result=(result or "")[:4000])
    try:
        resp = await invoke_with_fallback(chain, [HumanMessage(content=prompt)])
    except Exception as exc:  # noqa: BLE001
        logger.warning("自动沉淀判断失败（忽略）：%s: %s", type(exc).__name__, exc)
        return []

    content = resp.content if isinstance(resp.content, str) else str(resp.content)
    parsed = _extract_json(content) or {}
    if not parsed.get("save"):
        logger.info("自动沉淀：本次没有值得长期记住的经验")
        return []

    saved: list[dict] = []
    for item in (parsed.get("items") or [])[:2]:
        title = str(item.get("title") or "").strip()
        body = str(item.get("content") or "").strip()
        if not title or not body:
            continue
        try:
            await save_tool.ainvoke({"title": title, "content": body})
        except Exception as exc:  # noqa: BLE001
            logger.warning("自动沉淀写入失败（%s）：%s: %s", title, type(exc).__name__, exc)
            continue
        saved.append({"title": title, "content": body})

    if saved:
        logger.info("自动沉淀：写入 %d 条经验 %s", len(saved), [s["title"] for s in saved])
    return saved


__all__ = [
    "INJECT_HEADER",
    "inject_relevant_knowledge",
    "is_worth_searching",
    "maybe_deposit_knowledge",
]
