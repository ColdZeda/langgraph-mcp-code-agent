"""RAG 文本分块（阶段 4 · T4.4 ①）。

**单独成一个模块**是有意的：这里全是纯函数、不 import torch / chromadb，
所以单测能毫秒级跑完（`store.py` 一 import 就会加载 embedding 模型和向量库）。

⚠️ 刻意**不把小块合并到 200-500 字**（这是对方案"每块 200-500 字"的实测订正）：
本项目知识库里每条知识是**独立条目**，实测长度 22~91 字符（7 个文件 × 5 条，
每文件总长 140~370 字符）。若为了凑够 200 字把小条目合并，等于把几个不同主题
重新"平均"成一个向量 —— 那正是 top1 只有 0.6 的病根（整篇一个向量）。
所以规则是：**一条原子 = 一块**；只有单条超过 max_chars 时才带重叠切开。
"""

from __future__ import annotations

import re

DEFAULT_MAX_CHARS = 500
DEFAULT_OVERLAP_RATIO = 0.15

# 切开超长原子时优先寻找的断点（从"最不影响语义"到"最次"）
_BREAK_SEPARATORS = ("\n", "。", "；", "！", "？", ". ", "，", " ")


def split_atoms(text: str) -> list[str]:
    """按【标题层级 + 段落】切成语义原子。"""
    atoms: list[str] = []
    # markdown 标题另起一段
    for part in re.split(r"(?m)^(?=#{1,6}\s)", text):
        for seg in re.split(r"\n\s*\n", part):
            seg = seg.strip()
            if seg:
                atoms.append(seg)
    return atoms


def split_long_atom(atom: str, max_chars: int, overlap_ratio: float) -> list[str]:
    """单个超长原子 → 带重叠滑窗切开（尽量落在换行/句末，别把句子劈两半）。"""
    overlap = int(max_chars * overlap_ratio)
    step = max(1, max_chars - overlap)
    out: list[str] = []
    start = 0
    while start < len(atom):
        end = min(start + max_chars, len(atom))
        if end < len(atom):
            for sep in _BREAK_SEPARATORS:
                idx = atom.rfind(sep, start + step, end)
                if idx > start:
                    end = idx + len(sep)
                    break
        piece = atom[start:end].strip()
        if piece:
            out.append(piece)
        if end >= len(atom):
            break
        # 回退 overlap 个字符形成重叠；至少前进 1，避免死循环
        start = max(start + 1, end - overlap)
    return out


def split_into_chunks(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_ratio: float = DEFAULT_OVERLAP_RATIO,
) -> list[str]:
    """把一篇文档切成若干语义块（一条原子 = 一块；超长才切）。"""
    chunks: list[str] = []
    for atom in split_atoms(text):
        if len(atom) <= max_chars:
            chunks.append(atom)
        else:
            chunks.extend(split_long_atom(atom, max_chars, overlap_ratio))
    return chunks


__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_OVERLAP_RATIO",
    "split_atoms",
    "split_into_chunks",
    "split_long_atom",
]
