"""RAG 消融对照（阶段 6 · T6.3 ①）——「整篇 + 无精排」vs「分块 + 精排」谁贡献了多少。

**为什么不能拿两次 `rag_bench` 的数字相减**：那是两个不同时间的数字，中间隔着好几个变量
（语料、模型、机器负载），说不清差距是谁的功劳。这里用**同一批查询 / 同一个 embedding 模型 /
同一份知识库**跑一个 2×2 四方格，把"分块"和"精排"两个改动分开归因：

| 组合 | 索引粒度 | 精排 | 等于 |
|---|---|---|---|
| **A** | 整篇（一个文件一个向量） | 否 | **改造前** |
| **B** | 整篇 | 是 | 只加精排 |
| **C** | 分块（生产索引） | 否 | 只加分块 |
| **D** | 分块（生产索引） | 是 | **现状**（生产路径 `store.search_knowledge`） |
| **E** | 分块 | 是 | **对照组**：分块 + 精排 + **全量召回** |

**为什么必须有 E（2026-09-22 第一次实测撞出来的）**：A/B 只有 7 个候选（7 篇文档全都进精排），
而 C/D 走生产配置 `recall_k=10` → 35 块里只有 10 块能进精排。**候选集大小不一样**，
"整篇 vs 分块"就被这个差异污染了：B 就算赢了也说不清是"整篇更好"还是"它让精排看全了"。
E 把 `recall_k` 放大到全部块，压掉这个差异，剩下的才是**粒度本身**的效果。
（第一次实测 B 在主指标上 0.70 > D 的 0.60 → 这个对照**不做就会得出错误结论**。）

指标（同一批 10 条查询）：

- **`correct_source_top1`（主指标）**：top-1 的来源**是不是这道题该去的那个文件**。
  正解文件是人工核对知识库原文写下的（`rag_bench.EXPECTED_SOURCE`），不是模型猜的。
- `real_source_top1/top3`：top-1/top3 是否来自 `real_knowledge/`（比关键词严、比正解文件松）；
- `file_top1/top3`：**与改造前同口径** —— 期望关键词是否出现在**整个文件**里；
- `chunk_top1/top3`：块粒度（**A/B 恒为 `null`**：整篇文档的"块"就是文件本身，报出来是自欺）；
- `distractor_top1`：top-1 落在 `distractors/`（故意写错的知识）的比例，**越低越好**；
- 延迟：每条查询的墙钟耗时（先预热一轮，再测 `--reps` 轮）。

**不需要 LLM，也不写生产 chroma**：整篇索引建在 `runtime/runs/` 下的临时目录里，跑完就删。

用法：
    uv run python evals/rag_ablation.py
    uv run python evals/rag_ablation.py --reps 10
    uv run python evals/rag_ablation.py --archive        # 另存一份到 docs/evidence/

⚠️ **局限（写报告时必须如实带上）**：

1. 知识库只有 **4 篇正解 + 3 篇干扰 = 35 条原子**，且文件本身只有 140~370 字符 ——
   语料太小会把"整篇 vs 分块"的差距**压小**。下面的数字是**真实的下界**，不能外推到大语料。
2. CrossEncoder（`ms-marco-MiniLM-L-6-v2`）是在**段落级**语料上训练的，B 组喂给它的候选是
   **整篇文件** → 分布外输入。这本身就是结论的一半：**精排要配分块才好用**。
3. reranker 缺失时 B/D 自动退化成 A/C（`store.get_reranker()` 返回 `None`）。
   脚本会在 `env.rerank_enabled` 里点名，并在打印时警告 —— 不让它**静默**变成"两个改动都没效果"。
4. 延迟是本机单进程内的数字；冷启动（模型懒加载，只发生一次）单独测，不混进稳态均值。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.config import (  # noqa: E402
    RAG_COLLECTION,
    RAG_RECALL_K,
    RAG_TOP_K,
    RERANKER_PATH,
    RUNS_DIR,
)
from app.code_agent.rag import store  # noqa: E402
from evals.env import knowledge_dir, use_eval_corpus  # noqa: E402
from evals.rag_bench import EXPECTED_SOURCE, QUERIES  # noqa: E402

#: 整篇索引用的 collection 名（建在**内存**里，不落盘 —— 见 `build_whole_doc_index`）
WHOLE_DOC_COLLECTION = "ablation_whole_doc"

COMBO_LABELS = {
    "A": "整篇 + 无精排 = 改造前",
    "B": "整篇 + 精排",
    "C": "分块 + 无精排",
    "D": "分块 + 精排 = 现状（生产）",
    "E": "分块 + 精排 + 全量召回",
}

#: 哪些组合的"块粒度"指标有意义（A/B 的块就是整个文件，报 null）
CHUNKED = {"C", "D", "E"}

_FILE_TEXT_CACHE: dict[str, str] = {}


def _source_full_text(source: str) -> str:
    """读来源文件全文（小写）—— 复现改造前的**文件粒度**口径。"""
    if source not in _FILE_TEXT_CACHE:
        try:
            _FILE_TEXT_CACHE[source] = (
                (knowledge_dir() / source).read_text(encoding="utf-8").lower()
            )
        except OSError:
            _FILE_TEXT_CACHE[source] = ""
    return _FILE_TEXT_CACHE[source]


def _embed(texts: list[str]) -> list[list[float]]:
    """与 `store._embed` 完全同一条调用（同一个模型、同样不归一化）。"""
    return [v.tolist() for v in store.get_embed_model().encode(texts)]


# ═══════════════════════════════════════════════════════════════════
# 整篇索引（改造前的形态）：一个文件一个向量，**建在内存里**
# ═══════════════════════════════════════════════════════════════════


def build_whole_doc_index() -> tuple[object, int]:
    """建"整篇一个向量"的索引，返回 `(collection, 文档数)`。

    ⚠️ 用 **`EphemeralClient`（纯内存）**，不是 `PersistentClient`（2026-09-22 实测订正）：
    第一版建在 `runtime/runs/rag_ablation_chroma/` 下，跑完 `shutil.rmtree(..., ignore_errors=True)`
    清理 —— 但 **chromadb 还开着那个 SQLite 文件**，Windows 上 rmtree 直接失败，
    而 `ignore_errors=True` 把它咽了下去 → **每次都留一个 0.39 MB 的垃圾目录**（正好是"静默失败"那个老毛病）。
    检索语义不受影响（两边都是同一套 HNSW + L2 距离、同一个 embedding 模型），
    但少了一份要手工擦的痕迹；顺带也不会碰到生产向量库。
    """
    import chromadb

    collection = chromadb.EphemeralClient().get_or_create_collection(name=WHOLE_DOC_COLLECTION)

    kd = knowledge_dir()
    files = sorted(
        f for f in list(kd.rglob("*.txt")) + list(kd.rglob("*.md")) if "__pycache__" not in str(f)
    )
    sources, texts = [], []
    for filepath in files:
        text = filepath.read_text(encoding="utf-8")
        if not text.strip():
            continue
        sources.append(store.source_of(filepath))
        texts.append(text)
    if texts:
        collection.add(
            ids=sources,
            documents=texts,
            embeddings=_embed(texts),
            metadatas=[{"source": s} for s in sources],
        )
    return collection, len(texts)


# ═══════════════════════════════════════════════════════════════════
# 四种检索组合
# ═══════════════════════════════════════════════════════════════════


def _query_raw(collection, query: str, n: int) -> tuple[list[dict], dict]:
    """向量粗召回（不做精排）。返回 (按距离升序的候选, 分段耗时)。"""
    t0 = time.perf_counter()
    embedding = _embed([query])[0]
    t1 = time.perf_counter()
    res = collection.query(
        query_embeddings=[embedding],
        n_results=n,
        include=["documents", "metadatas", "distances"],
    )
    t2 = time.perf_counter()

    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    items = [
        {
            "text": doc,
            "source": (metas[i] or {}).get("source", "") if i < len(metas) else "",
            "distance": float(dists[i]) if i < len(dists) else None,
        }
        for i, doc in enumerate(docs)
    ]
    # chroma 默认按距离升序返回；显式再排一次，免得依赖隐式行为
    items.sort(key=lambda x: x["distance"] if x["distance"] is not None else 1e9)
    return items, {
        "embed_ms": round((t1 - t0) * 1000, 2),
        "vector_ms": round((t2 - t1) * 1000, 2),
    }


def _rerank(query: str, items: list[dict], top_k: int) -> tuple[list[dict], float, bool]:
    """CrossEncoder 精排；reranker 不可用时**原样返回**（并如实报告没排）。"""
    reranker = store.get_reranker()
    if reranker is None or len(items) <= 1:
        return items[:top_k], 0.0, False
    t0 = time.perf_counter()
    scores = reranker.predict([(query, it["text"]) for it in items])
    for it, score in zip(items, scores, strict=True):
        it["rerank_score"] = float(score)
    items.sort(key=lambda x: x["rerank_score"], reverse=True)
    return items[:top_k], round((time.perf_counter() - t0) * 1000, 2), True


def combo_searches(whole_coll, chunk_coll, *, top_k: int, recall_k: int, full_recall: int):
    """五个组合的检索函数：`(query) -> (items, timings)`。

    `full_recall` 是 E 组用的候选集大小（= 全部块数），用来压掉"候选集大小"这个混杂变量。
    """

    def combo_a(query: str):
        items, timings = _query_raw(whole_coll, query, recall_k)
        return items[:top_k], {**timings, "rerank_ms": 0.0}

    def combo_b(query: str):
        items, timings = _query_raw(whole_coll, query, recall_k)
        ranked, rerank_ms, _ = _rerank(query, items, top_k)
        return ranked, {**timings, "rerank_ms": rerank_ms}

    def combo_c(query: str):
        items, timings = _query_raw(chunk_coll, query, recall_k)
        return items[:top_k], {**timings, "rerank_ms": 0.0}

    def combo_d(query: str):
        # 生产路径原样调用 —— 这是"现状"的数字，不是这里的复现
        start = time.perf_counter()
        items = store.search_knowledge(query, top_k=top_k, recall_k=recall_k)
        return items, {
            "rerank_ms": None,
            "total_ms": round((time.perf_counter() - start) * 1000, 2),
        }

    def combo_e(query: str):
        # 对照组：同样分块 + 精排，但候选集放大到全部块（生产路径，只是 recall_k 不同）
        start = time.perf_counter()
        items = store.search_knowledge(query, top_k=top_k, recall_k=full_recall)
        return items, {
            "rerank_ms": None,
            "total_ms": round((time.perf_counter() - start) * 1000, 2),
        }

    return {"A": combo_a, "B": combo_b, "C": combo_c, "D": combo_d, "E": combo_e}


# ═══════════════════════════════════════════════════════════════════
# 指标
# ═══════════════════════════════════════════════════════════════════


def _ranked_sources(items: list[dict], limit: int = 3) -> list[str]:
    out: list[str] = []
    for it in items:
        if it["source"] and it["source"] not in out:
            out.append(it["source"])
        if len(out) >= limit:
            break
    return out


def evaluate_combo(records: list[dict], *, chunk_granularity: bool) -> dict:
    """把一轮（10 条查询 × 1 个组合）的检索结果算成指标。"""
    n = len(records)
    correct_top1 = real_top1 = real_top3 = distractor_top1 = 0
    file_top1 = file_top3 = chunk_top1 = chunk_top3 = 0
    latencies: list[float] = []
    details = []

    for rec in records:
        items = rec["items"]
        query = rec["query"]
        needle = rec["expected"].lower()

        sources = _ranked_sources(items)
        file_hits = [needle in _source_full_text(s) for s in sources]
        f1 = bool(file_hits[0]) if file_hits else False
        f3 = any(file_hits)
        file_top1 += int(f1)
        file_top3 += int(f3)

        if chunk_granularity:
            flags = [needle in it["text"].lower() for it in items[:3]]
            c1 = bool(flags[0]) if flags else False
            c3 = any(flags)
            chunk_top1 += int(c1)
            chunk_top3 += int(c3)
        else:
            c1 = c3 = None

        wanted = EXPECTED_SOURCE.get(query, "")
        hit_correct = bool(sources) and sources[0] == wanted
        correct_top1 += int(hit_correct)

        is_real = [s.startswith("real_knowledge/") for s in sources]
        real_top1 += int(is_real[0]) if is_real else 0
        real_top3 += int(any(is_real))

        is_distractor = [s.startswith("distractors/") for s in sources]
        distractor_top1 += int(is_distractor[0]) if is_distractor else 0

        latencies.append(rec["total_ms"])
        details.append(
            {
                "query": query,
                "expected": rec["expected"],
                "expected_source": wanted,
                "top1_source": sources[0] if sources else "",
                "correct_source_top1": hit_correct,
                "hit_top1_chunk": c1,
                "hit_top1_file": f1,
                "hit_top3_file": f3,
                "top3_sources": sources,
            }
        )

    return {
        "correct_source_top1": round(correct_top1 / n, 4),
        "real_source_top1": round(real_top1 / n, 4),
        "real_source_top3": round(real_top3 / n, 4),
        "file_top1": round(file_top1 / n, 4),
        "file_top3": round(file_top3 / n, 4),
        "chunk_top1": round(chunk_top1 / n, 4) if chunk_granularity else None,
        "chunk_top3": round(chunk_top3 / n, 4) if chunk_granularity else None,
        "distractor_top1": round(distractor_top1 / n, 4),
        "latency_ms_avg": round(sum(latencies) / n, 2),
        "latency_ms_max": round(max(latencies), 2),
        "queries": n,
        "details": details,
    }


def run_combo(search, *, reps: int, top_k: int) -> tuple[dict, list[dict]]:
    """跑一个组合：预热一轮 → 测 `reps` 轮。

    返回 `(指标, 第一轮的原始检索结果)` —— 成绩取**第一轮**（预热之后才算数），
    延迟取全部预热轮次的平均。原始结果回传是为了让"块粒度"指标用**同一批结果**算，
    不用再跑一遍检索（跑两遍可能因为缓存/负载不同而给出不一致的两个数字）。
    """
    stage_totals: dict[str, float] = {}
    records: list[dict] = []
    warm: list[float] = []

    for rep in range(reps + 1):
        round_records = []
        for query, expected, _topic in QUERIES:
            start = time.perf_counter()
            items, timings = search(query)
            total_ms = round((time.perf_counter() - start) * 1000, 2)
            round_records.append(
                {"query": query, "expected": expected, "items": items, "total_ms": total_ms}
            )
            if rep == 0:
                for key, value in timings.items():
                    if value is not None:
                        stage_totals[key] = round(stage_totals.get(key, 0.0) + value, 2)
            else:
                warm.append(total_ms)
        if rep == 0:
            records = round_records

    metrics = evaluate_combo(records, chunk_granularity=False)
    metrics["latency_ms_avg_warm"] = round(sum(warm) / len(warm), 2) if warm else None
    metrics["stage_totals_ms_per_round"] = {k: round(v, 2) for k, v in stage_totals.items()}
    return metrics, records


# ═══════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════


def run(*, reps: int = 5, top_k: int = RAG_TOP_K, recall_k: int = RAG_RECALL_K) -> dict:
    store.ensure_seeded()
    chunk_coll = store.get_collection()
    reranker = store.get_reranker()

    # 冷启动单独测：把模型懒加载的耗时从稳态里拎出来（否则会把均值抬高一大截）
    cold_start = time.perf_counter()
    _embed(["预热"])
    store.get_reranker()
    cold_ms = round((time.perf_counter() - cold_start) * 1000, 2)

    whole_coll, whole_docs = build_whole_doc_index()
    chunk_total = store.get_chunk_count()
    searches = combo_searches(
        whole_coll, chunk_coll, top_k=top_k, recall_k=recall_k, full_recall=max(chunk_total, 1)
    )

    combos: dict[str, dict] = {}
    for key in ("A", "B", "C", "D", "E"):
        metrics, records = run_combo(searches[key], reps=reps, top_k=top_k)
        if key in CHUNKED:
            # 块粒度只在分块索引上有意义；用**同一批**检索结果算，不重跑
            chunk_metrics = evaluate_combo(records, chunk_granularity=True)
            metrics["chunk_top1"] = chunk_metrics["chunk_top1"]
            metrics["chunk_top3"] = chunk_metrics["chunk_top3"]
        combos[key] = {"label": COMBO_LABELS[key], **metrics}

    return {
        "run_id": f"rag_ablation_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        "kind": "rag_ablation",
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "env": {
            "chunks_total": chunk_total,
            "whole_docs_indexed": whole_docs,
            "rerank_enabled": reranker is not None,
            "reranker_path": str(RERANKER_PATH),
            "collection": RAG_COLLECTION,
            "top_k": top_k,
            "recall_k": recall_k,
            "full_recall_k": max(chunk_total, 1),
            "reps": reps,
            "queries": len(QUERIES),
            "cold_start_ms": cold_ms,
        },
        "combos": combos,
    }


def _print_report(payload: dict) -> None:
    env = payload["env"]
    print("=" * 78)
    print("RAG 消融对照（整篇 vs 分块｜无精排 vs 精排）")
    print("=" * 78)
    print(
        f"\n索引：{env['chunks_total']} 块 / {env['whole_docs_indexed']} 篇；"
        f"查询 {env['queries']} 条；D 组 recall_k={env['recall_k']}"
        f"（E 组全量 {env['full_recall_k']}）→ top_k={env['top_k']}；预热 {env['reps']} 轮"
    )
    print(f"精排：{'已启用（本地 CrossEncoder）' if env['rerank_enabled'] else '⚠️ 未启用'}")
    if not env["rerank_enabled"]:
        print("  ⚠️ reranker 不可用 → B/D/E 已退化成 A/C，本次结果**不能**说明精排的效果")
    print(f"冷启动（模型懒加载，只发生一次）：{env['cold_start_ms']} ms")

    head = (
        f"\n{'组合':<26}{'正解文件top1':>12}{'正解来源top1':>13}"
        f"{'文件top1':>10}{'块top1':>9}{'干扰项top1':>11}{'稳态ms':>10}"
    )
    print(head)
    print("-" * 74)
    for key in ("A", "B", "C", "D", "E"):
        c = payload["combos"][key]
        chunk = "—" if c["chunk_top1"] is None else f"{c['chunk_top1']:.2f}"
        warm = c["latency_ms_avg_warm"]
        warm_s = "—" if warm is None else f"{warm:.1f}"
        print(
            f"{key} {c['label']:<24}{c['correct_source_top1']:>11.2f}"
            f"{c['real_source_top1']:>13.2f}{c['file_top1']:>10.2f}{chunk:>9}"
            f"{c['distractor_top1']:>11.2f}{warm_s:>10}"
        )

    a = payload["combos"]["A"]
    print("\n主指标（top-1 命中【正解文件】）—— 每个改动各贡献多少：")
    print(f"  A 改造前（整篇 + 无精排）      {a['correct_source_top1']:.2f}")
    for key, note in (
        ("C", "只加分块"),
        ("B", "只加精排"),
        ("D", "两个都加 = 现状（生产）"),
        ("E", "两个都加 + 全量召回（对照）"),
    ):
        c = payload["combos"][key]
        print(
            f"  {key} {note:<26}{c['correct_source_top1']:.2f}"
            f"（相对 A {c['correct_source_top1'] - a['correct_source_top1']:+.2f}）"
        )

    print("\n每条查询的 top-1 来源：")
    for key in ("A", "B", "C", "D", "E"):
        c = payload["combos"][key]
        right = sum(1 for d_ in c["details"] if d_["correct_source_top1"])
        print(f"  [{key}] {c['label']}　（{right}/{len(c['details'])} 命中正解文件）")
        for d_ in c["details"]:
            mark = "✓" if d_["correct_source_top1"] else "✗"
            print(f"    {mark} {d_['query']:<34} → {d_['top1_source'] or '(空)'}")

    print("\n⚠️ 局限：语料仅 4 篇正解 + 3 篇干扰（文件 140~370 字符），数字是**真实下界**，")
    print("   不可外推到大语料；精排模型是段落级训练的，B 组喂整篇文件属分布外输入；")
    print("   A/B 的候选集是全部 7 篇，C/D 只有 10 块 —— 比 B 与 D 时必须看 E 这一行。")


def main() -> int:
    # ⚠️ 第一步：切到评估专用语料与向量库（订正 #36）
    use_eval_corpus()

    parser = argparse.ArgumentParser(description="RAG 消融对照（不需要 LLM）")
    parser.add_argument("--reps", type=int, default=5, help="预热轮数（默认 5）")
    parser.add_argument("--top-k", type=int, default=RAG_TOP_K)
    parser.add_argument("--recall-k", type=int, default=RAG_RECALL_K)
    parser.add_argument("--out", type=Path, default=None, help="结果 JSON 路径")
    parser.add_argument("--archive", action="store_true", help="另存一份到 docs/evidence/")
    args = parser.parse_args()

    payload = run(reps=args.reps, top_k=args.top_k, recall_k=args.recall_k)
    _print_report(payload)

    out = args.out or (RUNS_DIR / f"{payload['run_id']}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")
    if args.archive:
        archived = Path(__file__).resolve().parents[1] / "docs" / "evidence" / out.name
        archived.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out, archived)
        print(f"已归档: {archived}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
