"""RAG 工具性能基准测试 — 查询延迟 / 检索准确率 / 召回率 / 相关性排序。

用法: uv run python evals/rag_bench.py
输出: 打印指标 + 保存 JSON 到 runtime/runs/rag_bench_{timestamp}.json

说明:
  - 需预置知识在 data/knowledge/（real_knowledge 真知识 + distractors 干扰项）
  - 直接调 rag.py 的 query_rag，不经过 Agent（测工具层性能）
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.mcp_servers import code_tools  # noqa: F401 确保路径
from app.code_agent.rag.rag import collection, query_rag_from_local

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


def measure_latency(n: int = 5) -> float:
    """查询延迟：跑 n 次取平均（毫秒）。"""
    times = []
    for _ in range(n):
        start = time.perf_counter()
        query_rag_from_local("Python 字符串格式化")
        times.append((time.perf_counter() - start) * 1000)
    return round(sum(times) / len(times), 1)


def measure_accuracy() -> tuple[float, list[dict]]:
    """检索准确率：每个问题看 top-3 结果是否命中预期关键词（top-1 命中 = 准确）。"""
    hits_top1 = 0
    hits_top3 = 0
    details = []
    for query, expected, _topic in QUERIES:
        emb = None
        # 用与 query_rag 相同的查询逻辑
        from app.code_agent.rag.rag import embed_model

        emb = embed_model.encode(query).tolist()
        results = collection.query(query_embeddings=[emb], n_results=3)

        if not results["documents"] or not results["documents"][0]:
            details.append(
                {"query": query, "hit_top1": False, "hit_top3": False, "top_results": []}
            )
            continue

        docs = results["documents"][0]
        ids = results["ids"][0] if results.get("ids") else []
        distances = results["distances"][0] if results.get("distances") else []

        # 检查每个返回结果是否命中期望关键词
        hit_flags = [expected.lower() in doc.lower() for doc in docs]
        top1_hit = hit_flags[0] if hit_flags else False
        top3_hit = any(hit_flags)

        if top1_hit:
            hits_top1 += 1
        if top3_hit:
            hits_top3 += 1

        details.append(
            {
                "query": query,
                "hit_top1": top1_hit,
                "hit_top3": top3_hit,
                "top_results": [
                    {
                        "id": ids[i] if i < len(ids) else "?",
                        "distance": distances[i] if i < len(distances) else None,
                    }
                    for i in range(len(docs))
                ],
            }
        )

    return round(hits_top1 / len(QUERIES), 3), round(hits_top3 / len(QUERIES), 3), details


def measure_recall() -> float:
    """召回率：查 python 主题，看 5 条 python 知识能召回几条。"""
    from app.code_agent.rag.rag import embed_model

    emb = embed_model.encode("Python 编程规范").tolist()
    results = collection.query(query_embeddings=[emb], n_results=10)

    if not results["documents"] or not results["documents"][0]:
        return 0.0

    docs = results["documents"][0]
    # python 知识文件里有哪些关键词可识别
    python_hits = sum(1 for d in docs if "Python" in d or "python" in d.lower())
    # 预置 python 知识 5 条
    total_python = 5
    return round(min(python_hits, total_python) / total_python, 3)


def measure_ordering() -> str:
    """相关性排序：检查返回结果 distance 是否递增（越相关越前 → distance 越小）。"""
    from app.code_agent.rag.rag import embed_model

    emb = embed_model.encode("Python 字符串格式化").tolist()
    results = collection.query(query_embeddings=[emb], n_results=5)

    if not results.get("distances") or not results["distances"][0]:
        return "N/A"

    distances = results["distances"][0]
    is_sorted = all(distances[i] <= distances[i + 1] for i in range(len(distances) - 1))
    return "sorted" if is_sorted else "unsorted"


def main():
    print("=" * 55)
    print("RAG 工具性能基准测试")
    print("=" * 55)

    print("\n[1/4] 查询延迟...")
    latency = measure_latency()
    print(f"  平均延迟: {latency} ms (5次)")

    print("\n[2/4] 检索准确率...")
    acc_top1, acc_top3, details = measure_accuracy()
    print(f"  top-1 命中: {acc_top1} ({int(acc_top1 * 10)}/10)")
    print(f"  top-3 命中: {acc_top3} ({int(acc_top3 * 10)}/10)")

    print("\n[3/4] 召回率...")
    recall = measure_recall()
    print(f"  python 主题召回: {recall} (5条中)")

    print("\n[4/4] 相关性排序...")
    ordering = measure_ordering()
    print(f"  排序质量: {ordering}")

    # 保存结果
    result = {
        "run_id": f"rag_bench_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "latency_ms_avg": latency,
        "accuracy_top1": acc_top1,
        "accuracy_top3": acc_top3,
        "recall": recall,
        "ordering": ordering,
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
