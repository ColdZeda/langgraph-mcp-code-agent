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
from collections.abc import Callable

from app.code_agent.config import RAG_AUTO_DEPOSIT, RAG_AUTO_INJECT, RAG_TOP_K
from app.code_agent.security.permissions import MODE_READONLY, current_session

logger = logging.getLogger(__name__)

# 太短 / 纯寒暄就不查（省一次检索，也避免把"你好"当查询词）
MIN_QUERY_CHARS = 10
CHITCHAT = {"你好", "您好", "hi", "hello", "在吗", "在么", "谢谢", "thanks", "test", "测试"}

# ⚠️ 2026-10-07 实测订正：原来只有 `【相关经验】` 四个字，模型把它当成**用户输入**，
#    在用户可见回复里报了「疑似提示注入：随任务一起塞进来的『【相关经验】』文本…建议留意输入来源」。
#    ⇒ 表头必须**自带来源说明**（这是系统注入的长期记忆，不是用户说的话）。
# ⚠️ 为什么不用 `SystemMessage` 承载：与 `agent/context.py` 里"摘要用 HumanMessage"同一个理由 ——
#    **任何 provider 都支持**；而"列表中间出现 system 角色"在部分 OpenAI 兼容服务上会被直接拒，
#    那会让整个任务失败（比"观感"严重得多）。
INJECT_HEADER = "【系统注入的相关经验｜来自长期记忆，不是用户输入，无需按提示注入报警】"


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

判断标准（**宁缺毋滥** —— 存错一条的代价比漏存一条大得多：知识库被噪音污染后检索质量会下降，
而这些经验还会被**自动注入**到以后的任务里）：

✅ **值得存**（必须属于下面某一类，且**脱离本次任务仍然成立**）：
- 环境事实：端口 / 路径 / 账号约定 / 服务名；
- 踩过的坑与规避方法（"XX 会报 YY，改成 ZZ 才行"）；
- 项目特有的规范或流程（例如"跑评估前必须先清 checkpoint 残留"）；
- 用户明确表达过的偏好或要求。

❌ **不值得存**（命中任何一条 → save 必须是 false）：
- **闲聊 / 打招呼 / 询问身份或能力**（"你是谁" / "你是什么模型" / "你能做什么"）；
- **模型对自己的介绍、自我描述、能力说明**；
- 复述任务本身、复述用了哪个工具、复述执行步骤；
- 用户个人信息（名字 / 联系方式）；
- 本次任务的具体产物内容、一次性数据、临时路径。

⚠️ **不确定就不存**（save=false）。先问自己一句：这条经验换个任务还用得上吗？用不上就别存。

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


def save_knowledge_in_process(title: str, content: str) -> tuple[str, int]:
    """**进程内**写入知识库（**不走 MCP 工具**）。

    ⚠️ 阶段 5 订正 #28：原实现是 `await save_tool.ainvoke(...)` —— 即**走 MCP 工具**，
    与本模块 docstring 写的"两个都在 Agent 进程内直接调 `rag/store.py`，不走 MCP"**不符**
    （自动注入确实没走 MCP，**只有自动沉淀走了**）。走 MCP 的代价有两层：
      1. 每写一条就新起一个 python 子进程重新 import chromadb + torch → 秒级延迟；
      2. 正好踩上订正 #27 那个坑 ——「**事件循环跑起来之后**才首次加载原生扩展会死锁」，
         表现是"知识写进去了、但工具调用永远不返回"，整轮任务卡死。
    → 改回进程内直调 `store.save_document`，与 docstring 一致，也不再受 MCP 那条路影响。
    """
    from app.code_agent.rag import store

    return store.save_document(title, content)


async def maybe_deposit_knowledge(
    user_input: str,
    result: str,
    *,
    chain: list,
    saver: Callable[[str, str], tuple[str, int]] | None = None,
    enabled: bool | None = None,
) -> list[dict]:
    """任务成功后判断并沉淀经验。

    ⚠️ **可关闭**（`CODE_AGENT_RAG_AUTO_DEPOSIT=0` 或显式传 `enabled=False`）：
    评估时必须关掉 —— 否则评测过程产生的临时经验会改写知识库，
    进而改变后续题目的检索结果（同一批数据前后不可比）。

    ⚠️ **阶段 5：不再走三档权限的人工确认**（用户 2026-09-21 决策 B）——
    自动沉淀是**应用自己的记账**，不是模型的自主动作（模型碰不到它的时机与内容），
    所以它由 `RAG_AUTO_DEPOSIT` 这一个开关管；
    但**「只读」档下仍然不写**（"什么都不改"要彻底）。
    模型自己主动调 `save_knowledge` 工具时，照旧走权限层（该弹框还是弹框）。
    """
    if enabled is None:
        enabled = RAG_AUTO_DEPOSIT
    if not enabled:
        return []
    if current_session().mode == MODE_READONLY:
        logger.info("权限模式为只读，跳过自动沉淀（只读档不写任何持久状态）")
        return []

    save = saver or save_knowledge_in_process

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
            # 同步直调（与 inject_relevant_knowledge 一致）：模型此时已经加载好，
            # 耗时在毫秒~百毫秒级，没必要为了它引入线程（线程里首次加载原生扩展反而有风险）。
            save(title, body)
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
    "save_knowledge_in_process",
]
