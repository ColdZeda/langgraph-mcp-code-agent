"""评估的**语料与向量库隔离**（订正 #36）。

## 为什么需要它

`data/knowledge/` 原来是"产品知识库 + 测试语料"混在一起的一个目录，里面放着 **7 篇测试语料**
（4 篇写对的 + **3 篇故意写错的干扰项**）。后果是：**跑完评估之后，产品会话会被灌进错误知识** ——
用户实测过一次：他问模型"你是什么模型"，回答里夹着三条"相关经验"（硬编码数据库连接、
报错就删文件重试、"所有 Web 项目都该用 PHP"）—— 那正是 `distractors/` 里的原文，
被 `agent/memory.py` 的**自动注入**塞进提示词的。

所以现在分家：

| 谁 | 知识库 | 向量库 |
|---|---|---|
| **产品**（CLI / Web / MCP 子进程） | `data/knowledge/`（**默认空**，靠使用慢慢积累） | `runtime/chroma_db/` |
| **评估**（`evals/*.py` 入口） | `runtime/eval_knowledge/`（每次从夹具复制） | `runtime/chroma_db_eval/` |

夹具（只读、进版本控制）在 `evals/fixtures/knowledge/`；
评估开跑时把它**复制到 `runtime/` 下再用** —— 这样评估期间模型自己写的知识、
每题开跑前的复位，**全发生在 runtime 里，绝不会往仓库里写东西**。

## 怎么生效的（**两种机制，别只做一半**）

1. **本进程**：直接改 `rag/store.py` 的模块级路径（它是在函数里读这两个全局量的），
   并把懒加载单例重置 —— 这样**不依赖 import 顺序**。
   （踩过：把 `use_eval_corpus()` 放在模块顶层调用，会让任何 import 它的测试都被改掉环境变量，
   pytest 里 `KNOWLEDGE_DIR` 被指到评估目录、一条测试假失败。**import 不该有副作用**。）
2. **MCP 子进程**：额外设 `CODE_AGENT_KNOWLEDGE_DIR` / `CODE_AGENT_CHROMA_DIR` 环境变量 ——
   `utils/mcp.py` 起子进程时用 `dict(os.environ)`，所以 RAG server 也只用评估那份语料，
   判定器与 Agent 的 `query_rag` 看到的是**同一份**。

⚠️ 因此：**评估侧的代码不要直接 import `config.KNOWLEDGE_DIR`**（那是产品的路径），
一律走本模块的 `knowledge_dir()` / `chroma_dir()`。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

__all__ = [
    "EVAL_CHROMA_DIR",
    "EVAL_KNOWLEDGE_DIR",
    "FIXTURE_KNOWLEDGE_DIR",
    "chroma_dir",
    "knowledge_dir",
    "use_eval_corpus",
]

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 只读夹具：评估用的 7 篇测试语料（4 篇正解 + 3 篇干扰），**进版本控制**
FIXTURE_KNOWLEDGE_DIR = Path(__file__).resolve().parent / "fixtures" / "knowledge"

#: 评估自己那份工作副本与向量库（都在 gitignore 的 runtime/ 下）
#: ⚠️ 这里不 import `config`（保持本模块 import 安全），所以 runtime 的默认值照 config 的公式写一遍
_RUNTIME_DIR = Path(os.getenv("CODE_AGENT_RUNTIME_DIR") or (REPO_ROOT / "runtime"))
EVAL_KNOWLEDGE_DIR = _RUNTIME_DIR / "eval_knowledge"
EVAL_CHROMA_DIR = _RUNTIME_DIR / "chroma_db_eval"

#: 夹具里必须有的东西（缺了就说明目录被挪走了/没拉全，早点报错比跑到一半发现好）
_REQUIRED_SUBDIRS = ("real_knowledge", "distractors")


def knowledge_dir() -> Path:
    """**评估**用的知识库目录（≠ 产品的 `data/knowledge/`）。"""
    return EVAL_KNOWLEDGE_DIR


def chroma_dir() -> Path:
    """**评估**用的向量库目录（≠ 产品的 `runtime/chroma_db/`）。"""
    return EVAL_CHROMA_DIR


def _materialize_fixtures() -> None:
    """把夹具复制到 runtime/ 下的工作副本（每次调用都重建）。"""
    if not FIXTURE_KNOWLEDGE_DIR.is_dir():
        raise RuntimeError(
            f"找不到评估语料夹具：{FIXTURE_KNOWLEDGE_DIR}\n"
            "（它应该在仓库里；如果你刚 clone，确认 evals/fixtures/knowledge/ 拉下来了）"
        )
    missing = [d for d in _REQUIRED_SUBDIRS if not (FIXTURE_KNOWLEDGE_DIR / d).is_dir()]
    if missing:
        raise RuntimeError(f"评估语料夹具不完整，缺：{missing}（在 {FIXTURE_KNOWLEDGE_DIR}）")

    EVAL_KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    for name in _REQUIRED_SUBDIRS:
        src, dst = FIXTURE_KNOWLEDGE_DIR / name, EVAL_KNOWLEDGE_DIR / name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    # 根目录散文件（上一轮模型自己写的）也清掉 —— 每题复位是第二道保险，这是第一道
    for stray in EVAL_KNOWLEDGE_DIR.iterdir():
        if stray.is_file() and stray.name != ".gitkeep":
            stray.unlink(missing_ok=True)
    EVAL_CHROMA_DIR.mkdir(parents=True, exist_ok=True)


def _point_store_at_eval_dirs() -> None:
    """把 `rag/store.py` 指到评估专用路径，并重置它的懒加载单例。"""
    from app.code_agent.rag import store

    store.KNOWLEDGE_DIR = EVAL_KNOWLEDGE_DIR
    store.CHROMA_DIR = EVAL_CHROMA_DIR
    # 单例必须重置：否则会继续用产品那个 collection / 跳过灌库
    store._collection = None
    store._seeded = False


def use_eval_corpus(*, quiet: bool = False) -> dict:
    """启用评估专用语料与向量库（**在入口的 `main()` 开头调用**）。

    可以重复调用（每次都把工作副本重建 = 评估起点恒定）。
    """
    _materialize_fixtures()
    os.environ["CODE_AGENT_KNOWLEDGE_DIR"] = str(EVAL_KNOWLEDGE_DIR)
    os.environ["CODE_AGENT_CHROMA_DIR"] = str(EVAL_CHROMA_DIR)
    _point_store_at_eval_dirs()

    snapshot = {
        "fixtures": str(FIXTURE_KNOWLEDGE_DIR),
        "knowledge_dir": str(EVAL_KNOWLEDGE_DIR),
        "chroma_dir": str(EVAL_CHROMA_DIR),
    }
    if not quiet:
        print(
            f"[evals] 语料隔离：夹具 {snapshot['fixtures']} → 工作副本 {snapshot['knowledge_dir']}"
        )
        print(
            f"[evals] 评估专用向量库：{snapshot['chroma_dir']}（产品的 runtime/chroma_db 不受影响）"
        )
    return snapshot
