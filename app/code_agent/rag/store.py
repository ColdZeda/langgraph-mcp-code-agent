"""分层记忆的「语义记忆」层：ChromaDB 知识库（分块 + 精排）。

阶段 4 · T4.4 把这里从「整篇文档一个向量」改成「按语义块建索引 + CrossEncoder 精排」。

**为什么要有这个模块（而不是继续写在 rag.py 里）**：
`rag.py` 是 MCP stdio 子进程（只提供工具）；而「任务开始时自动注入知识」「任务成功后自动沉淀」
需要在 **Agent 进程内**直接检索 —— 走 MCP 工具的话，每次调用都要新起一个 python 子进程
重新 import chromadb + torch（实测工具调用是"每次调用自建会话"），
延迟从毫秒级变成秒级。所以：**检索/索引逻辑放这里，两个进程共用一份**，
`rag.py` 只剩一层薄薄的工具壳。

⚠️ **全部懒加载**：import 本模块**不**加载 embedding 模型、不连 chromadb、不灌库。
（否则 `import` 一下就要几秒 + 吃内存，测试也没法只测纯逻辑。）

⚠️ 分块会打穿原来的 id / 增量 / CRUD 逻辑（第六版没提）：
  | 原来（整篇） | 现在（分块） |
  |---|---|
  | doc_id = 文件相对路径 | `f"{source}#{块序号}"`（否则后块覆盖前块） |
  | metadata 只有 mtime | `{source, chunk, mtime}` |
  | 按 doc_id 删旧 | 按 `where={"source": …}` 删旧（否则改文件后旧块留库 → 检索到过期内容） |
  | CRUD 拿 title 当 id | 保持「整篇」语义：写文件 → 重建该 source 的全部块 |
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from app.code_agent.config import (
    CHROMA_DIR,
    EMBEDDING_MODEL_CACHE_DIR,
    EMBEDDING_MODEL_PATH,
    KNOWLEDGE_DIR,
    RAG_CHUNK_MAX_CHARS,
    RAG_CHUNK_OVERLAP_RATIO,
    RAG_COLLECTION,
    RAG_RECALL_K,
    RAG_TOP_K,
    RERANK_ENABLED,
    RERANKER_PATH,
)
from app.code_agent.rag.chunking import split_into_chunks

__all__ = [
    "delete_document",
    "document_exists",
    "ensure_seeded",
    "format_results",
    "get_chunk_count",
    "get_collection",
    "get_embed_model",
    "get_reranker",
    "index_source",
    "save_document",
    "search_knowledge",
    "seed_knowledge_base",
    "source_of",
    "split_into_chunks",
]


def _log(msg: str) -> None:
    # MCP stdio 协议：stdout 是 JSON-RPC 通道，任何 print 都会污染协议，所以一律走 stderr。
    # （Agent 进程里写 stderr 同样无害，统一走一个出口省事。）
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


# ═══════════════════════════════════════════════════════════════════
# 懒加载的全局单例（embedding 模型 / 向量库 / reranker）
# ═══════════════════════════════════════════════════════════════════

_embed_model: Any = None
_collection: Any = None
_reranker: Any = None
_reranker_resolved = False
_seeded = False


def _load_embedding_model():
    from sentence_transformers import SentenceTransformer

    # 核心文件 model.safetensors 存在 → 直接从本地加载，不走网络
    if os.path.exists(EMBEDDING_MODEL_PATH / "model.safetensors"):
        return SentenceTransformer(str(EMBEDDING_MODEL_PATH))

    # 首次：从 ModelScope 下载（只下载 PyTorch 格式）
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
                    "*/onnx/*",
                    "*/openvino/*",
                    "*.h5",
                    "*.ot",
                    "pytorch_model.bin",
                ],
            )
        finally:
            sys.stdout.close()
            sys.stdout = saved_stdout
    return SentenceTransformer(model_dir)


def get_embed_model():
    global _embed_model
    if _embed_model is None:
        _embed_model = _load_embedding_model()
    return _embed_model


def get_collection():
    global _collection
    if _collection is None:
        import chromadb

        KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
        CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        # ⚠️ 换 collection 名：旧的「整篇一个向量」与新的「每块一个向量」混在一个 collection 里，
        #    两种粒度互相干扰，检索质量会**更差**，所以必须分家。
        _collection = client.get_or_create_collection(name=RAG_COLLECTION)
    return _collection


def get_chunk_count() -> int:
    return get_collection().count()


def _load_reranker():
    """从**本地路径**加载 CrossEncoder。

    ⚠️ 离线优先 + **优雅降级**：
      - 路径不存在（没预下载）→ 返回 None，检索退化为纯向量召回，**不联网、不崩**；
      - 加载失败（权重损坏等）→ 同上。
    理由：rerank 只是"锦上添花"，不该成为硬依赖；而本项目 RAG 跑在 stdio 子进程里，
    多一个联网下载源就多一份污染 JSON-RPC 通道的风险。
    """
    if not RERANK_ENABLED:
        _log("[RAG] rerank 已被 CODE_AGENT_RERANK=0 关闭，使用纯向量召回")
        return None
    if not (Path(RERANKER_PATH) / "config.json").exists():
        _log(f"[RAG] 未找到本地 reranker（{RERANKER_PATH}）→ 降级为纯向量召回")
        return None
    try:
        from sentence_transformers import CrossEncoder

        return CrossEncoder(str(RERANKER_PATH))
    except Exception as exc:  # noqa: BLE001 —— 降级而不是崩
        _log(f"[RAG] reranker 加载失败（{type(exc).__name__}: {exc}）→ 降级为纯向量召回")
        return None


def get_reranker():
    """懒加载 reranker（进程内只解析一次；返回 None 表示已降级）。"""
    global _reranker, _reranker_resolved
    if not _reranker_resolved:
        _reranker = _load_reranker()
        _reranker_resolved = True
    return _reranker


# ═══════════════════════════════════════════════════════════════════
# 索引
# ═══════════════════════════════════════════════════════════════════


def _embed(texts: list[str]) -> list[list[float]]:
    """批量编码（单条也走这个，保证与入库时同一条代码路径）。"""
    vectors = get_embed_model().encode(texts)
    return [v.tolist() for v in vectors]


def _delete_source(source: str) -> None:
    """按 source 删除该文件的**全部块**（分块后不能再按 doc_id 删）。"""
    try:
        get_collection().delete(where={"source": source})
    except Exception as exc:  # noqa: BLE001 —— 空 collection 等情况不算错
        _log(f"[RAG] 删除 source={source} 的旧块时出错（忽略）：{type(exc).__name__}: {exc}")


def index_source(source: str, text: str, mtime: float) -> int:
    """（重）建一个 source 的全部块。返回块数。"""
    chunks = split_into_chunks(
        text, max_chars=RAG_CHUNK_MAX_CHARS, overlap_ratio=RAG_CHUNK_OVERLAP_RATIO
    )
    _delete_source(source)
    if not chunks:
        return 0
    get_collection().add(
        ids=[f"{source}#{i}" for i in range(len(chunks))],
        documents=chunks,
        embeddings=_embed(chunks),
        metadatas=[{"source": source, "chunk": i, "mtime": mtime} for i in range(len(chunks))],
    )
    return len(chunks)


def source_of(filepath: Path) -> str:
    return filepath.relative_to(KNOWLEDGE_DIR).as_posix()


def seed_knowledge_base() -> None:
    """扫描 knowledge/（含子目录）增量灌库。

    - 新文件 → 建块入库
    - mtime 变了 → **按 source 删掉全部旧块**再重建（分块后不能按 doc_id 删）
    - mtime 没变 → 跳过
    - **文件已被删除 → 连它的块一起清掉**（否则会检索到早已删掉的内容）
    """
    files = list(KNOWLEDGE_DIR.rglob("*.txt")) + list(KNOWLEDGE_DIR.rglob("*.md"))
    files = [f for f in files if "__pycache__" not in str(f)]

    collection = get_collection()
    existing = collection.get(include=["metadatas"])
    source_mtimes: dict[str, float] = {}
    for meta in existing["metadatas"] or []:
        src = (meta or {}).get("source")
        if not src:
            continue
        source_mtimes[src] = max(source_mtimes.get(src, 0.0), float(meta.get("mtime") or 0))

    if not files and not source_mtimes:
        _log("[ChromaDB] knowledge/ 文件夹为空，跳过导入")
        return

    new_sources, updated = [], []
    for filepath in files:
        source = source_of(filepath)
        mtime = filepath.stat().st_mtime
        if source in source_mtimes and source_mtimes[source] == mtime:
            continue
        text = filepath.read_text(encoding="utf-8")
        if not text.strip():
            continue
        n = index_source(source, text, mtime)
        (updated if source in source_mtimes else new_sources).append(f"{source}({n}块)")

    # 清理"文件已删但块还在"的 source
    alive = {source_of(f) for f in files}
    purged = [s for s in source_mtimes if s not in alive]
    for src in purged:
        _delete_source(src)

    parts = []
    if new_sources:
        parts.append(f"{len(new_sources)} 篇新增")
    if updated:
        parts.append(f"{len(updated)} 篇更新")
    if purged:
        parts.append(f"{len(purged)} 篇已删清理")
    if parts:
        _log(f"[ChromaDB] {', '.join(parts)}；共 {collection.count()} 块")
    else:
        _log(f"[ChromaDB] 知识无变化（{len(source_mtimes)} 篇 / {collection.count()} 块）")


def ensure_seeded() -> None:
    """进程内只灌一次库（Agent 进程内检索前调用；MCP server 启动时也会调）。"""
    global _seeded
    if not _seeded:
        seed_knowledge_base()
        _seeded = True


# ═══════════════════════════════════════════════════════════════════
# 检索（粗召回 → 精排）
# ═══════════════════════════════════════════════════════════════════


def search_knowledge(
    query: str,
    *,
    top_k: int = RAG_TOP_K,
    recall_k: int = RAG_RECALL_K,
) -> list[dict]:
    """检索知识：向量粗召回 `recall_k` 块 → CrossEncoder 精排 → 取 `top_k`。

    reranker 不可用时**自动退化为纯向量召回**（结果照常返回，只是不重排）。
    返回 `[{id, text, source, chunk, distance, rerank_score?}, …]`。
    """
    collection = get_collection()
    total = collection.count()
    if total == 0:
        _log("[RAG] 知识库为空（还没有灌库？）→ 返回空结果")
        return []
    n = max(1, min(max(recall_k, top_k), total))

    res = collection.query(
        query_embeddings=[_embed([query])[0]],
        n_results=n,
        include=["documents", "metadatas", "distances"],
    )
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    ids = (res.get("ids") or [[]])[0]

    items = [
        {
            "id": ids[i] if i < len(ids) else "",
            "text": doc,
            "source": (metas[i] or {}).get("source", "") if i < len(metas) else "",
            "chunk": (metas[i] or {}).get("chunk") if i < len(metas) else None,
            "distance": float(dists[i]) if i < len(dists) else None,
        }
        for i, doc in enumerate(docs)
    ]

    reranker = get_reranker()
    if reranker is not None and len(items) > 1:
        scores = reranker.predict([(query, it["text"]) for it in items])
        for it, s in zip(items, scores, strict=True):
            it["rerank_score"] = float(s)
        items.sort(key=lambda x: x["rerank_score"], reverse=True)

    return items[:top_k]


def format_results(items: list[dict]) -> str:
    """把检索结果拼成给模型/工具看的文本。"""
    if not items:
        return "知识库中未找到相关内容"
    lines = []
    for it in items:
        tag = f"（来源：{it['source']}"
        if it.get("chunk") is not None:
            tag += f" 第{it['chunk']}块"
        tag += "）"
        lines.append(f"{it['text']}\n{tag}")
    return "\n    ---\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 整篇 CRUD（对外的三个工具用；分块对使用者透明）
# ═══════════════════════════════════════════════════════════════════


def safe_title(title: str) -> str:
    return title.replace("/", "_").replace("\\", "_")


def save_document(title: str, content: str) -> tuple[str, int]:
    """整篇保存：写文件 + 重建该 source 的全部块。返回 (source, 块数)。"""
    name = safe_title(title)
    filepath = KNOWLEDGE_DIR / f"{name}.txt"
    filepath.write_text(content, encoding="utf-8")
    n = index_source(f"{name}.txt", content, filepath.stat().st_mtime)
    return f"{name}.txt", n


def delete_document(title: str) -> tuple[bool, bool]:
    """整篇删除：删文件 + 按 source 删掉全部块。返回 (删了文件, 删了向量)。"""
    name = safe_title(title)
    filepath = KNOWLEDGE_DIR / f"{name}.txt"
    deleted_file = False
    if filepath.exists():
        filepath.unlink()
        deleted_file = True

    collection = get_collection()
    before = collection.count()
    _delete_source(f"{name}.txt")
    deleted_vector = collection.count() < before
    if not deleted_vector:
        # 兼容历史数据：改造前"整篇"入库时 id 就是标题（没有 .txt、没有 #序号）
        try:
            collection.delete(ids=[name])
            deleted_vector = collection.count() < before
        except Exception:  # noqa: BLE001
            pass
    return deleted_file, deleted_vector


def document_exists(title: str) -> bool:
    return (KNOWLEDGE_DIR / f"{safe_title(title)}.txt").exists()
