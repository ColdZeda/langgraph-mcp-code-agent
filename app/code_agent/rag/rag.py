import os
from pathlib import Path
from typing import Annotated

import chromadb
from pydantic import Field

import logging

from sentence_transformers import SentenceTransformer
from mcp.server.fastmcp import FastMCP
from app.code_agent.config import (
    CHROMA_DIR,
    EMBEDDING_MODEL_CACHE_DIR,
    EMBEDDING_MODEL_PATH,
    KNOWLEDGE_DIR,
)

# 抑制 ModelScope 下载进度条污染 MCP stdio 协议
logging.getLogger("modelscope").setLevel(logging.WARNING)

mcp = FastMCP()

# MCP stdio 协议：stdout 是 JSON-RPC 通道，任何 print 都会污染协议，所以用 stderr 打日志
import sys as _sys
def _log(msg: str) -> None:
    _sys.stderr.write(msg + "\n")
    _sys.stderr.flush()

# ── 加载 Embedding 模型 ──
# 核心文件 model.safetensors 存在 → 直接从本地加载，不走网络
if os.path.exists(EMBEDDING_MODEL_PATH / "model.safetensors"):
    embed_model = SentenceTransformer(str(EMBEDDING_MODEL_PATH))
else:
    # 首次：从 ModelScope 下载（只下载 PyTorch 格式）
    import sys
    from contextlib import redirect_stderr
    from modelscope import snapshot_download

    # 抑制下载进度条（否则会通过 stdio 污染 MCP 协议）
    with redirect_stderr(None):
        saved_stdout = sys.stdout
        sys.stdout = open(os.devnull, "w")
        try:
            model_dir = snapshot_download(
                "sentence-transformers/all-MiniLM-L6-v2",
                cache_dir=str(EMBEDDING_MODEL_CACHE_DIR),
                ignore_file_pattern=[
                    "*/onnx/*", "*/openvino/*",
                    "*.h5", "*.ot", "pytorch_model.bin",
                ],
            )
        finally:
            sys.stdout.close()
            sys.stdout = saved_stdout
    embed_model = SentenceTransformer(model_dir)

# ── ChromaDB 初始化 ──
KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
CHROMA_DIR.mkdir(parents=True, exist_ok=True)

chroma_client = chromadb.PersistentClient(path=str(CHROMA_DIR))
collection = chroma_client.get_or_create_collection(name="terminal_knowledge")


def seed_knowledge_base():
    """扫描 knowledge/ 文件夹（含子目录），把所有 .txt/.md 文件灌入 ChromaDB
    - 新文件 → 直接导入
    - 旧文件但 mtime 变了 → 删除旧记录，重新导入
    - 旧文件 mtime 没变 → 跳过
    - doc_id 用相对路径（如 real_knowledge/python_best_practices），避免子目录同名冲突
    """
    files = list(KNOWLEDGE_DIR.rglob("*.txt")) + list(KNOWLEDGE_DIR.rglob("*.md"))
    files = [f for f in files if "__pycache__" not in str(f)]
    if not files:
        _log("[ChromaDB] knowledge/ 文件夹为空，跳过导入")
        return

    # 从 ChromaDB 拉出现有数据：{id: 上次的 mtime}
    existing = collection.get()
    existing_mtimes = {}
    for doc_id, meta in zip(existing["ids"], existing["metadatas"]):
        existing_mtimes[doc_id] = (meta or {}).get("mtime", 0)

    new_ids, new_docs, new_embeddings, new_metadatas = [], [], [], []
    delete_ids = []
    updated_count = 0

    for filepath in files:
        # 用相对路径做 ID：real_knowledge/xxx 或 distractors/xxx
        doc_id = filepath.relative_to(KNOWLEDGE_DIR).with_suffix("").as_posix()
        current_mtime = filepath.stat().st_mtime

        if doc_id in existing_mtimes:
            if current_mtime == existing_mtimes[doc_id]:
                continue  # 没变，跳过
            # mtime 变了 → 先删旧，再导入
            delete_ids.append(doc_id)
            updated_count += 1

        text = filepath.read_text(encoding="utf-8")
        if not text.strip():
            continue
        new_ids.append(doc_id)
        new_docs.append(text)
        new_embeddings.append(embed_model.encode(text).tolist())
        new_metadatas.append({"mtime": current_mtime})

    # 批量删除旧记录
    if delete_ids:
        collection.delete(ids=delete_ids)

    # 批量导入新/更新记录
    if new_ids:
        collection.add(
            documents=new_docs,
            embeddings=new_embeddings,
            metadatas=new_metadatas,
            ids=new_ids,
        )
        new_only = len(new_ids) - updated_count
        parts = []
        if new_only > 0:
            parts.append(f"{new_only} 篇新增")
        if updated_count > 0:
            parts.append(f"{updated_count} 篇更新")
        _log(f"[ChromaDB] {', '.join(parts)}: {new_ids}")
    else:
        _log(f"[ChromaDB] 知识无变化（共 {len(existing_mtimes)} 篇）")


seed_knowledge_base()


@mcp.tool(name="query_rag", description="查询本地知识库（ChromaDB + Embedding）")
def query_rag_from_local(
    query: Annotated[
        str,
        Field(description="访问知识库查询的内容", examples=["终端的操作规范"]),
    ] = ""
) -> str:
    query_embedding = embed_model.encode(query).tolist()
    results = collection.query(query_embeddings=[query_embedding], n_results=3)

    if results["documents"] and results["documents"][0]:
        result = "\n    ---\n".join(results["documents"][0])
    else:
        result = "知识库中未找到相关内容"

    _log("-" * 60)
    _log(f"[ChromaDB RAG] query: {query}")
    _log(result)
    _log("-" * 60)

    return result


# ════════════════ 工具 2：保存知识（自学习核心） ════════════════

@mcp.tool(name="save_knowledge", description="将学到的知识保存到本地知识库。写入 knowledge/ 文件夹并立即向量化入库，无需等待重启。")
def save_knowledge(
    title: Annotated[str, Field(description="知识标题，同时用作文件名（不需要加 .txt 后缀）", examples=["Vue项目创建规范"])],
    content: Annotated[str, Field(description="知识的完整内容", examples=["创建Vue3项目：cd 目标目录; vue create 项目名 --default"])],
) -> str:
    safe_title = title.replace("/", "_").replace("\\", "_")
    filepath = KNOWLEDGE_DIR / f"{safe_title}.txt"
    filepath.write_text(content, encoding="utf-8")
    mtime = filepath.stat().st_mtime

    embedding = embed_model.encode(content).tolist()
    collection.upsert(
        documents=[content],
        embeddings=[embedding],
        metadatas=[{"mtime": mtime}],
        ids=[safe_title],
    )

    _log(f"[save_knowledge] 已保存: {safe_title} ({len(content)} 字符)")
    return f"知识 '{safe_title}' 已保存到知识库，共 {len(content)} 字符"


# ════════════════ 工具 3：删除知识 ════════════════

@mcp.tool(name="delete_knowledge", description="从知识库中删除指定知识。同时删除 knowledge/ 文件和 ChromaDB 向量。")
def delete_knowledge(
    title: Annotated[str, Field(description="要删除的知识标题（不需要加 .txt 后缀）", examples=["Vue项目创建规范"])],
) -> str:
    safe_title = title.replace("/", "_").replace("\\", "_")
    filepath = KNOWLEDGE_DIR / f"{safe_title}.txt"

    deleted_file = False
    if filepath.exists():
        filepath.unlink()
        deleted_file = True

    # 从 ChromaDB 删除（即使文件不存在也清理向量残留）
    try:
        collection.delete(ids=[safe_title])
        deleted_vector = True
    except Exception:
        deleted_vector = False

    if deleted_file or deleted_vector:
        _log(f"[delete_knowledge] 已删除: {safe_title} (文件={deleted_file}, 向量={deleted_vector})")
        return f"知识 '{safe_title}' 已从知识库删除"
    return f"知识 '{safe_title}' 不存在，无需删除"


# ════════════════ 工具 4：更新知识 ════════════════

@mcp.tool(name="update_knowledge", description="更新知识库中的已有知识。等同于先删除再保存。")
def update_knowledge(
    title: Annotated[str, Field(description="要更新的知识标题", examples=["Vue项目创建规范"])],
    content: Annotated[str, Field(description="更新后的完整内容", examples=["创建Vue3项目：npm create vue@latest 项目名"])],
) -> str:
    safe_title = title.replace("/", "_").replace("\\", "_")
    filepath = KNOWLEDGE_DIR / f"{safe_title}.txt"

    if not filepath.exists():
        return f"知识 '{safe_title}' 不存在，请先用 save_knowledge 创建"

    # 覆盖文件
    filepath.write_text(content, encoding="utf-8")
    mtime = filepath.stat().st_mtime

    # 更新 ChromaDB 向量
    embedding = embed_model.encode(content).tolist()
    collection.upsert(
        documents=[content],
        embeddings=[embedding],
        metadatas=[{"mtime": mtime}],
        ids=[safe_title],
    )

    _log(f"[update_knowledge] 已更新: {safe_title} ({len(content)} 字符)")
    return f"知识 '{safe_title}' 已更新，新内容共 {len(content)} 字符"


if __name__ == "__main__":
    mcp.run(transport="stdio")
