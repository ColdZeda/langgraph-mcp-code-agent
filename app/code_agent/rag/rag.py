"""RAG MCP Server（阶段 4 · T4.4 改造后）。

**这里只剩一层薄薄的工具壳**：真正的分块 / 索引 / 检索 / 精排逻辑都在
`app/code_agent/rag/store.py`，因为 Agent 进程也要用同一套（自动注入 / 自动沉淀），
而走 MCP 工具每次调用都要新起 python 子进程，太慢。

改造前后对比：
  | | 改造前 | 改造后 |
  |---|---|---|
  | 索引进度 | 整篇文档一个向量（内容一长就被"平均"掉） | **按语义块**建索引，每块一个向量 |
  | 检索 | 向量 top-3 | 向量粗召回 top-10 → **CrossEncoder 精排** → top-3 |
  | collection | `terminal_knowledge` | `terminal_knowledge_v2`（两种粒度不能混） |
"""

import logging
import sys
from typing import Annotated

# ⚠️⚠️ **阶段 5 订正 #27（真因）—— 这一行 import 是修 bug 用的，别当冗余删掉！**
#
# 症状：`query_rag` / `save_knowledge`（凡是要向量化的工具）从这个 MCP server 调，
#       **永远不返回**；而 `delete_knowledge` 里"知识不存在"那种早返回路径 0.2 秒就回。
#       调用方看到的是"卡死"，但**副作用其实已经发生**（文件与向量都写好了）——
#       所以那一轮对话"转圈 + 不烧 token"，最难查的一类症状。
#
# 实测定位（`faulthandler` 打线程栈）：
#       `store.get_embed_model()` → `from sentence_transformers import ...`
#       → sklearn → **scipy.special 的扩展模块 `create_module`（Windows DLL 加载）卡死**。
#       同一句 import 放在**事件循环启动之前**则完全正常 ——
#       也就是说：**在 `mcp.run()` 之后（anyio 已起工作线程）才首次加载这些原生扩展会死锁**
#       （DLL 的 `DllMain` 与 GIL / 加载器锁互等）。
#
# 修法：在**模块 import 阶段**（= `mcp.run()` 之前）就把这些库导进来，让 DLL 先加载完。
#       **不预热模型** —— 模型仍然在第一次用到时惰性实例化（实测那样已经不卡了）。
#       代价为 0：这些 import 本来就要付，只是从"第一次工具调用时"挪到"进程启动时"。
#       `store.py` **保持全懒加载不变**：Agent 进程与单测 import 它仍然不会 load torch。
import sentence_transformers  # noqa: F401  —— 见上面的订正说明
from mcp.server.fastmcp import FastMCP  # noqa: E402
from pydantic import Field  # noqa: E402

from app.code_agent.rag import store  # noqa: E402

# 抑制 ModelScope 下载进度条污染 MCP stdio 协议
logging.getLogger("modelscope").setLevel(logging.WARNING)

mcp = FastMCP()

# 启动时灌一次库（增量：mtime 没变的文件会跳过）
store.ensure_seeded()


def _log(msg: str) -> None:
    # MCP stdio 协议：stdout 是 JSON-RPC 通道，任何 print 都会污染协议，所以用 stderr 打日志
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


# ════════════════ 工具 1：查询知识（自学习闭环的读端） ════════════════


@mcp.tool(name="query_rag", description="查询本地知识库（ChromaDB 分块检索 + CrossEncoder 精排）")
def query_rag_from_local(
    query: Annotated[
        str,
        Field(description="访问知识库查询的内容", examples=["终端的操作规范"]),
    ] = "",
) -> str:
    results = store.search_knowledge(query)
    result = store.format_results(results)

    _log("-" * 60)
    _log(f"[ChromaDB RAG] query: {query}")
    for it in results:
        score = it.get("rerank_score")
        score_text = f" rerank={score:.3f}" if score is not None else ""
        _log(f"  · {it['id']} (dist={it['distance']:.3f}{score_text})")
    _log(result)
    _log("-" * 60)

    return result


# ════════════════ 工具 2：保存知识（自学习核心） ════════════════


@mcp.tool(
    name="save_knowledge",
    description="将学到的知识保存到本地知识库。写入 knowledge/ 文件夹并立即分块向量化入库，无需等待重启。",
)
def save_knowledge(
    title: Annotated[
        str,
        Field(
            description="知识标题，同时用作文件名（不需要加 .txt 后缀）",
            examples=["Vue项目创建规范"],
        ),
    ],
    content: Annotated[
        str,
        Field(
            description="知识的完整内容",
            examples=["创建Vue3项目：cd 目标目录; vue create 项目名 --default"],
        ),
    ],
) -> str:
    source, chunks = store.save_document(title, content)
    _log(f"[save_knowledge] 已保存: {source} ({len(content)} 字符 / {chunks} 块)")
    return f"知识 '{source}' 已保存到知识库，共 {len(content)} 字符（{chunks} 块）"


# ════════════════ 工具 3：删除知识 ════════════════


@mcp.tool(
    name="delete_knowledge",
    description="从知识库中删除指定知识。同时删除 knowledge/ 文件和它的全部向量块。",
)
def delete_knowledge(
    title: Annotated[
        str,
        Field(description="要删除的知识标题（不需要加 .txt 后缀）", examples=["Vue项目创建规范"]),
    ],
) -> str:
    deleted_file, deleted_vector = store.delete_document(title)
    safe_title = store.safe_title(title)

    if deleted_file or deleted_vector:
        _log(
            f"[delete_knowledge] 已删除: {safe_title} (文件={deleted_file}, 向量={deleted_vector})"
        )
        return f"知识 '{safe_title}' 已从知识库删除"
    return f"知识 '{safe_title}' 不存在，无需删除"


# ════════════════ 工具 4：更新知识 ════════════════


@mcp.tool(name="update_knowledge", description="更新知识库中的已有知识。等同于先删除再保存。")
def update_knowledge(
    title: Annotated[str, Field(description="要更新的知识标题", examples=["Vue项目创建规范"])],
    content: Annotated[
        str,
        Field(
            description="更新后的完整内容", examples=["创建Vue3项目：npm create vue@latest 项目名"]
        ),
    ],
) -> str:
    if not store.document_exists(title):
        return f"知识 '{store.safe_title(title)}' 不存在，请先用 save_knowledge 创建"

    source, chunks = store.save_document(title, content)
    _log(f"[update_knowledge] 已更新: {source} ({len(content)} 字符 / {chunks} 块)")
    return f"知识 '{source}' 已更新，新内容共 {len(content)} 字符（{chunks} 块）"


if __name__ == "__main__":
    mcp.run(transport="stdio")
