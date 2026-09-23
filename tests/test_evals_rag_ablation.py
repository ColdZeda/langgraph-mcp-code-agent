"""阶段 6 · RAG 消融脚本的守卫测试（**不加载模型、不连库**）。

守两件事：

1. **指标算法本身对**（`evaluate_combo`）—— 主指标的定义是这份报告可信度的地基；
2. **正解来源这份"人工标注"没跑偏**（`EXPECTED_SOURCE`）—— 题面关键词命中会高估质量，
   所以主指标拿"该去哪个文件"当答案；那个答案必须①**每条查询都有**、
   ②**文件真的存在**、③**文件里真的有那个关键词**。任何一条不成立，测试就红。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import rag_ablation as A  # noqa: E402
from evals.env import FIXTURE_KNOWLEDGE_DIR  # noqa: E402   # 语料已挪进夹具（订正 #36）
from evals.rag_bench import EXPECTED_SOURCE, QUERIES  # noqa: E402


def _rec(query: str, expected: str, sources: list[str], *, chunk_text: str | None = None) -> dict:
    return {
        "query": query,
        "expected": expected,
        "items": [
            {"text": chunk_text if chunk_text is not None else expected, "source": s}
            for s in sources
        ],
        "total_ms": 10.0,
    }


# ═══════════════════════════════════════════════════════════════════
# 人工标注的"正解来源"必须与语料一致
# ═══════════════════════════════════════════════════════════════════


def test_every_query_has_an_expected_source():
    missing = [q for q, _e, _t in QUERIES if q not in EXPECTED_SOURCE]
    assert not missing, f"这些查询没有标注正解来源（主指标会算不出）：{missing}"


def test_no_stale_expected_source_entries():
    known = {q for q, _e, _t in QUERIES}
    stale = [q for q in EXPECTED_SOURCE if q not in known]
    assert not stale, f"EXPECTED_SOURCE 里有已经不在题面里的查询：{stale}"


def test_expected_source_files_exist():
    for query, source in EXPECTED_SOURCE.items():
        assert (FIXTURE_KNOWLEDGE_DIR / source).exists(), f"{query} 标注的来源不存在：{source}"


def test_expected_source_actually_contains_the_keyword():
    """**这条最关键**：标注的文件里必须真的有那个期望关键词，否则"正解"是假的。

    否则会出现"检索到了正确文件却判不过"的怪现象 —— 那是标注错了，不是系统错了。
    """
    needle_by_query = {q: e for q, e, _t in QUERIES}
    for query, source in EXPECTED_SOURCE.items():
        text = (FIXTURE_KNOWLEDGE_DIR / source).read_text(encoding="utf-8").lower()
        needle = needle_by_query[query].lower()
        assert needle in text, f"「{query}」标注来源 {source}，但里面找不到关键词「{needle}」"


def test_expected_sources_are_all_real_knowledge():
    """正解来源必须是 `real_knowledge/` —— 标到 `distractors/` 上等于把错误答案当正解。"""
    for query, source in EXPECTED_SOURCE.items():
        assert source.startswith("real_knowledge/"), f"{query} 的正解来源标错目录：{source}"


# ═══════════════════════════════════════════════════════════════════
# 指标算法
# ═══════════════════════════════════════════════════════════════════


def test_correct_source_top1_counts_only_exact_top1():
    query = next(iter(EXPECTED_SOURCE))
    right = EXPECTED_SOURCE[query]
    metrics = A.evaluate_combo(
        [
            _rec(query, "kw", [right, "real_knowledge/other.txt"]),
            _rec(query, "kw", ["real_knowledge/other.txt", right]),
        ],
        chunk_granularity=False,
    )
    assert metrics["correct_source_top1"] == 0.5, "只有 top-1 才算，排第二不算"


def test_distractor_top1_is_counted():
    query = next(iter(EXPECTED_SOURCE))
    metrics = A.evaluate_combo(
        [_rec(query, "kw", ["distractors/outdated_tips.txt"])],
        chunk_granularity=False,
    )
    assert metrics["distractor_top1"] == 1.0
    assert metrics["correct_source_top1"] == 0.0
    assert metrics["real_source_top1"] == 0.0


def test_chunk_granularity_is_none_for_whole_doc_combos():
    """A/B 的"块"就是整个文件 → 块粒度指标必须是 `None`（报出来是自欺）。"""
    query = next(iter(EXPECTED_SOURCE))
    metrics = A.evaluate_combo(
        [_rec(query, "kw", [EXPECTED_SOURCE[query]])], chunk_granularity=False
    )
    assert metrics["chunk_top1"] is None
    assert metrics["chunk_top3"] is None


def test_chunk_granularity_checks_the_top_chunk_text():
    """块粒度问的是"top-1 那个块里有没有关键词"，不是"它所在文件里有没有"。"""
    query = next(iter(EXPECTED_SOURCE))
    source = EXPECTED_SOURCE[query]
    hit = A.evaluate_combo(
        [_rec(query, "needle", [source], chunk_text="这里写着 needle 哦")],
        chunk_granularity=True,
    )
    miss = A.evaluate_combo(
        [_rec(query, "needle", [source], chunk_text="这个块里没有那个词")],
        chunk_granularity=True,
    )
    assert hit["chunk_top1"] == 1.0
    assert miss["chunk_top1"] == 0.0
    assert miss["correct_source_top1"] == 1.0, "来源是对的，只是那个块里没有关键词"


def test_ranked_sources_dedupes_and_keeps_order():
    items = [
        {"source": "a.txt"},
        {"source": "a.txt"},
        {"source": "b.txt"},
        {"source": "c.txt"},
        {"source": "d.txt"},
    ]
    assert A._ranked_sources(items, limit=3) == ["a.txt", "b.txt", "c.txt"]


def test_empty_results_do_not_crash():
    metrics = A.evaluate_combo(
        [{"query": "q", "expected": "kw", "items": [], "total_ms": 1.0}],
        chunk_granularity=True,
    )
    assert metrics["correct_source_top1"] == 0.0
    assert metrics["chunk_top1"] == 0.0
    assert metrics["queries"] == 1


def test_latency_is_aggregated():
    metrics = A.evaluate_combo(
        [
            {"query": "q1", "expected": "kw", "items": [], "total_ms": 10.0},
            {"query": "q2", "expected": "kw", "items": [], "total_ms": 30.0},
        ],
        chunk_granularity=False,
    )
    assert metrics["latency_ms_avg"] == 20.0
    assert metrics["latency_ms_max"] == 30.0


# ═══════════════════════════════════════════════════════════════════
# 别碰生产数据
# ═══════════════════════════════════════════════════════════════════


def test_ablation_does_not_write_a_vector_db_to_disk():
    """整篇索引必须**建在内存**里 —— 生产向量库和临时目录都不许碰。

    ⚠️ 这条是实测订正（2026-09-22）：第一版用 `PersistentClient` 建在
    `runtime/runs/rag_ablation_chroma/`，跑完 `rmtree(..., ignore_errors=True)` 清理，
    但 chromadb 还开着 SQLite 文件 → Windows 上 rmtree 失败 → **每次留一个 0.39 MB 的垃圾目录**，
    而且被 `ignore_errors=True` 咽掉了。现在改成 `EphemeralClient`，
    用源码级检查钉住（真的建库要加载 embedding 模型，单测里几十秒不值得）。
    """
    source = Path(A.__file__).read_text(encoding="utf-8")
    # 只看**代码**里的调用（docstring 里会提到 PersistentClient 来解释为什么不许用它）
    assert "PersistentClient(" not in source, "消融不能往磁盘写向量库"
    assert "EphemeralClient()" in source, "整篇索引应该是内存客户端"
    assert "ABLATION_CHROMA_DIR" not in source, "临时目录那套已经删了，别加回来"


def test_verifier_probe_is_not_used_for_the_model_api():
    """模型 API 探针必须自己带鉴权头；`verifiers.http_reachable` 不带任何头（只打本机）。"""
    source = Path(A.__file__).read_text(encoding="utf-8")
    assert "http_reachable" not in source


def test_combos_cover_the_control_group():
    """E（全量召回对照组）不能少 —— 少了就会把"候选集大小"的差异误读成"整篇更好"。"""
    assert set(A.COMBO_LABELS) == {"A", "B", "C", "D", "E"}
    assert "全量召回" in A.COMBO_LABELS["E"]
    assert A.CHUNKED == {"C", "D", "E"}, "块粒度指标只对分块索引有意义"
