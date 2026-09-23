"""RAG 工具性能基准测试 — 查询延迟 / 检索准确率 / 召回率 / 相关性排序。

用法: uv run python evals/rag_bench.py
输出: 打印指标 + 保存 JSON 到 runtime/runs/rag_bench_{timestamp}.json

说明:
  - 需预置知识在 data/knowledge/（real_knowledge 真知识 + distractors 干扰项）
  - 直接调 RAG 的检索层，不经过 Agent（测工具层性能）
  - ⚠️ 阶段 4 起改为调用 **`store.search_knowledge`**（与 `query_rag` 工具同一条检索路径：
    向量粗召回 → CrossEncoder 精排）。
    改造前这一层等价于 `collection.query(n_results=3)`（整篇文档一个向量），
    所以口径没变、数字可比。
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.config import KNOWLEDGE_DIR, RAG_RECALL_K  # noqa: E402
from app.code_agent.rag import store  # noqa: E402

# ── 测试查询集：每个问题对应真实答案的关键词 ──
QUERIES = [
    # (问题, 期望命中的内容关键词, 主题)
    ("Python 字符串格式化推荐用哪种方式", "f-string", "python"),
    ("Python 如何安全地打开文件", "with", "python"),
    ("MySQL 表名拼接 SQL 时要注意什么", "反引号", "mysql"),
    ("MySQL 查询参数怎么传", "参数化", "mysql"),
    ("MCP server 调试日志输出到哪里", "stderr", "mcp"),
    ("MCP 工具函数怎么注册", "mcp.tool", "mcp"),
    ("WSL 命令怎么防止死循环", "超时", "wsl"),
    ("WSL 部署文件传到哪个目录", "nginx/uploads", "wsl"),
    ("Python 列表推导式有什么好处", "列表推导", "python"),
    ("MySQL 事务操作要注意什么", "BEGIN", "mysql"),
]

_QUERY_FOR_ORDERING = "Python 字符串格式化"

#: 每条查询**应该**命中的来源文件（人工核对 `data/knowledge/real_knowledge/` 原文写下的，
#: 2026-09-22 逐条核过）。`rag_ablation.py` 用它算主指标 `correct_source_top1`
#: —— 光看关键词命中会**高估**质量：比如"Python 如何安全地打开文件"的关键词是 `with`，
#: 而 `with` 在别的文件里也可能出现；再比如"推荐 f-string"和"别用 f-string"都能命中关键词。
EXPECTED_SOURCE = {
    "Python 字符串格式化推荐用哪种方式": "real_knowledge/python_best_practices.txt",
    "Python 如何安全地打开文件": "real_knowledge/python_best_practices.txt",
    "MySQL 表名拼接 SQL 时要注意什么": "real_knowledge/mysql_safety.txt",
    "MySQL 查询参数怎么传": "real_knowledge/mysql_safety.txt",
    "MCP server 调试日志输出到哪里": "real_knowledge/mcp_protocol.txt",
    "MCP 工具函数怎么注册": "real_knowledge/mcp_protocol.txt",
    "WSL 命令怎么防止死循环": "real_knowledge/wsl_safety.txt",
    "WSL 部署文件传到哪个目录": "real_knowledge/wsl_safety.txt",
    "Python 列表推导式有什么好处": "real_knowledge/python_best_practices.txt",
    "MySQL 事务操作要注意什么": "real_knowledge/mysql_safety.txt",
}


def measure_latency(n: int = 5) -> tuple[float, float, dict]:
    """查询延迟：跑 n 次取平均（毫秒）。走**与 query_rag 工具完全相同的检索路径**。

    ⚠️ **分开报「冷启动」与「稳态」**：第一次调用包含 reranker 的懒加载（只发生一次），
    把它混进平均值会把稳态延迟报高一大截（实测 111ms vs 82ms）——
    那种数字既不准也没意义（真实运行里只冷一次）。
    同时给出**分段耗时**（embed / chroma / rerank），只看总延迟看不出瓶颈在哪。
    """
    # 冷启动：包含 embedding 模型 + CrossEncoder 的懒加载
    t_cold = time.perf_counter()
    store.search_knowledge(_QUERY_FOR_ORDERING, top_k=3)
    cold_ms = round((time.perf_counter() - t_cold) * 1000, 1)

    times = []
    for _ in range(n):
        start = time.perf_counter()
        store.search_knowledge(_QUERY_FOR_ORDERING, top_k=3)
        times.append((time.perf_counter() - start) * 1000)

    # 分段：已预热，逐段计时
    t0 = time.perf_counter()
    vector = store.get_embed_model().encode([_QUERY_FOR_ORDERING])
    t1 = time.perf_counter()
    res = store.get_collection().query(
        query_embeddings=[vector[0].tolist()],
        n_results=RAG_RECALL_K,
        include=["documents", "metadatas", "distances"],
    )
    t2 = time.perf_counter()
    reranker = store.get_reranker()
    docs = res["documents"][0]
    if reranker is not None and len(docs) > 1:
        reranker.predict([(_QUERY_FOR_ORDERING, d) for d in docs])
    t3 = time.perf_counter()

    breakdown = {
        "embed_ms": round((t1 - t0) * 1000, 1),
        "vector_search_ms": round((t2 - t1) * 1000, 1),
        "rerank_ms": round((t3 - t2) * 1000, 1) if reranker is not None else 0.0,
        "rerank_pairs": len(docs) if reranker is not None else 0,
    }
    return round(sum(times) / len(times), 1), cold_ms, breakdown


_FILE_TEXT_CACHE: dict[str, str] = {}


def _source_full_text(source: str) -> str:
    """读来源文件的**全文**（小写），用于复现"改造前的文件粒度口径"。

    改造前的检索单位就是"一整个文件"（整篇一个向量），所以那时的
    "top-1 命中"问的是「排第一的**文件**里有没有这个关键词」——
    要公平对比就必须同样问"整个文件"，而不是问"召回到的那个块"。
    """
    if source not in _FILE_TEXT_CACHE:
        path = Path(KNOWLEDGE_DIR) / source
        try:
            _FILE_TEXT_CACHE[source] = path.read_text(encoding="utf-8").lower()
        except OSError:
            _FILE_TEXT_CACHE[source] = ""
    return _FILE_TEXT_CACHE[source]


def measure_accuracy() -> tuple[dict, list[dict]]:
    """检索准确率。

    ⚠️ **两种粒度都给**，否则改造前后不可比：

    - **块粒度（chunk）**：top-1/top-3 返回的**块**里有没有期望关键词
      —— 这是系统现在的真实行为（`query_rag` 返回的就是 3 个块），口径更严。
    - **文件粒度（source）**：把命中块归到来源文件、按最好排名取前 1/3 个文件，
      再看**整个文件**里有没有关键词
      —— **这一档才与改造前口径完全一致**（改造前"一个文档"就是一整个文件，
      5 条知识捆在一起，关键词自然更容易命中；拿它跟"3 个块"直接比会冤枉新实现）。
    - **正解来源（real_source）**：top-1/top-3 是不是来自 `real_knowledge/`。
      为什么要单列：关键词命中**分不清**「推荐 f-string」和「别用 f-string，用 % 更好」——
      后者正是 `distractors/` 里故意写错的干扰项，只报关键词命中会**高估**检索质量。
    """
    chunk_top1 = chunk_top3 = 0
    file_top1 = file_top3 = 0
    real_top1 = real_top3 = 0
    distractor_top1 = 0
    details = []

    for query, expected, _topic in QUERIES:
        items = store.search_knowledge(query, top_k=RAG_RECALL_K)
        if not items:
            details.append(
                {"query": query, "hit_top1": False, "hit_top3": False, "top_results": []}
            )
            continue

        needle = expected.lower()
        flags = [needle in it["text"].lower() for it in items]

        # ── 块粒度 ──
        c1 = flags[0]
        c3 = any(flags[:3])
        chunk_top1 += int(c1)
        chunk_top3 += int(c3)

        # ── 文件粒度（按最好排名取不同的 source，再看整个文件）──
        ranked_sources: list[str] = []
        for it in items:
            if it["source"] not in ranked_sources:
                ranked_sources.append(it["source"])
        file_hits = [needle in _source_full_text(s) for s in ranked_sources[:3]]
        f1 = file_hits[0] if file_hits else False
        f3 = any(file_hits)
        file_top1 += int(f1)
        file_top3 += int(f3)

        # ── 正解来源口径 ──
        is_real = [s.startswith("real_knowledge/") for s in ranked_sources]
        real_top1 += int(is_real[0]) if is_real else 0
        real_top3 += int(any(is_real[:3]))
        distractor_top1 += (
            int(ranked_sources[0].startswith("distractors/")) if ranked_sources else 0
        )

        details.append(
            {
                "query": query,
                "hit_top1": c1,
                "hit_top3": c3,
                "hit_top1_source": f1,
                "hit_top3_source": f3,
                "top1_from_real_knowledge": bool(is_real[0]) if is_real else False,
                "expected": expected,
                "top1_source": ranked_sources[0] if ranked_sources else "",
                "top_results": [
                    {
                        "id": it["id"],
                        "source": it["source"],
                        "hit": flags[i],
                        "distance": round(it["distance"], 4)
                        if it["distance"] is not None
                        else None,
                        "rerank_score": round(it["rerank_score"], 4)
                        if it.get("rerank_score") is not None
                        else None,
                    }
                    for i, it in enumerate(items[:3])
                ],
            }
        )

    n = len(QUERIES)
    return (
        {
            "accuracy_top1": round(chunk_top1 / n, 3),
            "accuracy_top3": round(chunk_top3 / n, 3),
            "accuracy_top1_source": round(file_top1 / n, 3),
            "accuracy_top3_source": round(file_top3 / n, 3),
            "real_source_top1": round(real_top1 / n, 3),
            "real_source_top3": round(real_top3 / n, 3),
            "distractor_top1": round(distractor_top1 / n, 3),
        },
        details,
    )


def measure_recall() -> float:
    """召回率：查 python 主题，看 5 条 python 知识能召回几条（按**块**计，同一块不重复）。"""
    items = store.search_knowledge("Python 编程规范", top_k=10)
    if not items:
        return 0.0

    # python 知识文件里有哪些关键词可识别
    python_hits = sum(1 for it in items if "python" in it["text"].lower())
    # 预置 python 知识 5 条
    total_python = 5
    return round(min(python_hits, total_python) / total_python, 3)


def measure_ordering() -> tuple[str, str]:
    """相关性排序：检查**当前排序依据**是否单调。

    改造前排序依据是向量距离（升序）；改造后开了 rerank 就是 rerank 分数（降序）。
    两者都合法 —— 所以这里同时报告"用的哪个信号"，避免把"rerank 重排导致距离不再单调"
    误读成排序退化。
    """
    items = store.search_knowledge(_QUERY_FOR_ORDERING, top_k=5)
    if not items:
        return "N/A", "none"

    if all(it.get("rerank_score") is not None for it in items):
        scores = [it["rerank_score"] for it in items]
        ok = all(scores[i] >= scores[i + 1] for i in range(len(scores) - 1))
        return ("sorted" if ok else "unsorted"), "rerank_score"

    dists = [it["distance"] for it in items if it["distance"] is not None]
    ok = all(dists[i] <= dists[i + 1] for i in range(len(dists) - 1))
    return ("sorted" if ok else "unsorted"), "distance"


def main():
    print("=" * 55)
    print("RAG 工具性能基准测试")
    print("=" * 55)

    # store 是懒加载的：这里显式灌一次库（增量，mtime 没变会跳过）
    store.ensure_seeded()

    reranker = store.get_reranker()
    print(
        f"\n[0/4] 索引状态: {store.get_chunk_count()} 块；"
        f"reranker = {'已启用（本地 CrossEncoder）' if reranker is not None else '未启用（纯向量召回）'}"
    )

    print("\n[1/4] 查询延迟...")
    latency, cold_ms, breakdown = measure_latency()
    print(f"  稳态延迟: {latency} ms (预热后 5 次平均)")
    print(f"  冷启动  : {cold_ms} ms（含 embedding + reranker 懒加载，只发生一次）")
    print(
        f"  分段: embed {breakdown['embed_ms']}ms + 向量检索 {breakdown['vector_search_ms']}ms"
        f" + 精排 {breakdown['rerank_ms']}ms（{breakdown['rerank_pairs']} 对）"
    )

    print("\n[2/4] 检索准确率...")
    acc, details = measure_accuracy()
    print(f"  top-1 命中【块粒度】  : {acc['accuracy_top1']} ({int(acc['accuracy_top1'] * 10)}/10)")
    print(f"  top-3 命中【块粒度】  : {acc['accuracy_top3']} ({int(acc['accuracy_top3'] * 10)}/10)")
    print(
        f"  top-1 命中【文件粒度】: {acc['accuracy_top1_source']} "
        f"({int(acc['accuracy_top1_source'] * 10)}/10)  ← 与改造前同口径"
    )
    print(
        f"  top-3 命中【文件粒度】: {acc['accuracy_top3_source']} "
        f"({int(acc['accuracy_top3_source'] * 10)}/10)  ← 与改造前同口径"
    )
    print(
        f"  top-1 来自正解文件    : {acc['real_source_top1']} "
        f"({int(acc['real_source_top1'] * 10)}/10)  ← 比[关键词命中]严：干扰项也会提关键词"
    )
    print(
        f"  top-1 落在干扰项      : {acc['distractor_top1']} "
        f"({int(acc['distractor_top1'] * 10)}/10)  ← 越低越好（distractors/ 是故意写错的）"
    )

    print("\n[3/4] 召回率...")
    recall = measure_recall()
    print(f"  python 主题召回: {recall} (5条中)")

    print("\n[4/4] 相关性排序...")
    ordering, signal = measure_ordering()
    print(f"  排序质量: {ordering} (依据: {signal})")

    # 保存结果
    result = {
        "run_id": f"rag_bench_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "latency_ms_avg": latency,
        "latency_cold_ms": cold_ms,
        "latency_breakdown": breakdown,
        **acc,
        "recall": recall,
        "ordering": ordering,
        "ordering_signal": signal,
        "rerank_enabled": reranker is not None,
        "chunks_total": store.get_chunk_count(),
        "query_details": details,
    }
    out_path = (
        Path(__file__).resolve().parents[1]
        / "runtime"
        / "runs"
        / f"rag_bench_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\n结果已保存: {out_path}")


if __name__ == "__main__":
    main()
