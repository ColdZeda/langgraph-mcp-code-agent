"""RAG 分块测试（阶段 4 · T4.4 ①）。

这是**纯函数**测试：只 import `chunking`，不碰 embedding 模型、不碰 ChromaDB，
所以毫秒级跑完（`store.py` 一 import 就会加载 torch）。

关键设计（对方案"每块 200-500 字"的实测订正）：知识库里每条知识是**独立条目**
（实测 22~91 字符），所以**一条原子 = 一块**，不为了凑字数把不同主题合并 ——
合并等于把它们"平均"成一个向量，那正是改造前 top1 只有 0.6 的病根。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.rag.chunking import (  # noqa: E402
    split_atoms,
    split_into_chunks,
    split_long_atom,
)


def test_each_paragraph_is_its_own_chunk():
    """短条目**不合并**（本项目知识库全是这种形状）。"""
    text = "第一条知识，很短。\n\n第二条知识，也很短。\n\n第三条知识。"
    chunks = split_into_chunks(text)

    assert chunks == ["第一条知识，很短。", "第二条知识，也很短。", "第三条知识。"]


def test_headings_start_new_chunks():
    text = "# 标题一\n内容 A\n\n## 标题二\n内容 B"
    chunks = split_into_chunks(text)

    assert len(chunks) == 2
    assert chunks[0].startswith("# 标题一")
    assert chunks[1].startswith("## 标题二")


def test_blank_lines_and_whitespace_ignored():
    text = "\n\n  段落一  \n\n\n\n段落二\n\n   \n"
    assert split_into_chunks(text) == ["段落一", "段落二"]


def test_empty_text_gives_no_chunks():
    assert split_into_chunks("") == []
    assert split_into_chunks("   \n\n  ") == []


def test_long_atom_is_split_with_overlap():
    """超过 max_chars 的原子要切开，且相邻块**有重叠**（避免答案正好骑在切缝上）。"""
    atom = "".join(f"第{i}句。 " for i in range(200))  # 约 1400 字符
    chunks = split_into_chunks(atom, max_chars=200, overlap_ratio=0.15)

    assert len(chunks) > 1
    assert all(len(c) <= 220 for c in chunks), "每块不应明显超过 max_chars"
    # 重叠：后一块的开头应能在前一块里找到（说明没有硬切丢内容）
    assert chunks[1][:20] in chunks[0] or chunks[0][-40:] in chunks[1]


def test_long_atom_split_loses_no_content():
    """重叠切分不能把内容切丢：各块拼起来必须覆盖原文的所有字符区间。"""
    atom = "".join(f"句子{i}。" for i in range(300))
    chunks = split_long_atom(atom, max_chars=100, overlap_ratio=0.2)

    assert chunks[0].startswith("句子0。")
    assert chunks[-1].rstrip().endswith("句子299。")
    covered = set()
    for c in chunks:
        start = atom.find(c)
        assert start >= 0, "每块都必须是原文的连续片段"
        covered.update(range(start, start + len(c)))
    assert len(covered) == len(atom), "所有字符都应至少被一块覆盖"


def test_chunk_size_boundary():
    """正好等于 max_chars 不切；超一个字符才切。"""
    exact = "x" * 500
    assert split_into_chunks(exact, max_chars=500) == [exact]
    assert len(split_into_chunks("x" * 501, max_chars=500)) == 2


def test_split_atoms_keeps_short_and_long_paragraphs():
    atoms = split_atoms("短\n\n" + "长" * 100)
    assert atoms == ["短", "长" * 100]


def test_real_knowledge_files_shape():
    """用**真实知识库文件**跑一遍：每个文件应切出 5 块（= 5 条独立知识）。"""
    knowledge_dir = Path(__file__).resolve().parents[1] / "data" / "knowledge"
    files = sorted(knowledge_dir.rglob("*.txt"))

    assert len(files) == 7, "预置知识库是 7 个文件"
    for f in files:
        chunks = split_into_chunks(f.read_text(encoding="utf-8"))
        assert len(chunks) == 5, f"{f.name} 应切成 5 块（5 条独立知识），实际 {len(chunks)}"
        assert all(c.strip() for c in chunks)
