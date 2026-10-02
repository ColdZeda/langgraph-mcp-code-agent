"""RAG 模型"就位 / 缺失"的行为守卫（2026-10-02 定案：**默认不自动下载**）。

## 背景（一次实测翻车换来的规则）

改造前，缺向量模型时 `store._load_embedding_model()` 会**自动联网下载**（ModelScope）。
实测结果：**236 秒 / 下了 671MB**（`ignore_file_pattern` 没滤掉 ONNX/OpenVINO），
而真正需要的只有 87MB；更糟的是外面套着 `redirect_stderr(None)`，把下载器的报错一起堵死，
用户看到的是 `AttributeError: 'NoneType' object has no attribute 'write'`。

现在（用户决策 · 甲方案）：**不自动下载**；缺模型 → **几十毫秒内**抛出可读错误，
消息里直接给出"怎么装"。装模型走 `scripts/fetch_models.py` 或手工放文件。

## 这里守四条

1. 缺模型 → 抛 `RagModelMissing`，且提示里**必须有出路**（脚本名 / env 变量名）；
2. 判据在**重库 import 之前**（源码级检查：宁可机械，也不靠"我记得"）；
3. 老的自动下载代码**不许回来**（源码级检查：`snapshot_download` / `redirect_stderr(None)` 都不该出现）；
4. 精排缺失仍然**优雅降级**（返回 None，不抛）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.code_agent.rag import store

STORE_SRC = Path(store.__file__).read_text(encoding="utf-8")


# ── ① 缺模型：快抛 + 提示可执行 ─────────────────────────────────────────────


def test_missing_embedding_model_raises_with_actionable_hint(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "EMBEDDING_MODEL_PATH", tmp_path / "not-there")
    with pytest.raises(store.RagModelMissing) as ei:
        store._load_embedding_model()

    msg = str(ei.value)
    assert "fetch_models.py" in msg  # 出路一：一键脚本
    assert "CODE_AGENT_EMBEDDING_MODEL_PATH" in msg  # 出路二：自己指路径
    assert "query_rag" in msg  # 说清影响面，别让人以为整个工具挂了


def test_ready_judgement_follows_the_core_file(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "EMBEDDING_MODEL_PATH", tmp_path)
    assert store.embedding_model_ready() is False
    (tmp_path / "model.safetensors").write_text("x", encoding="utf-8")
    assert store.embedding_model_ready() is True


# ── ②③ 源码级守卫（机械可查，不靠自觉）────────────────────────────────────


def test_existence_check_happens_before_heavy_import():
    """判据必须在 `from sentence_transformers import …` **之前**（否则缺模型要先 import torch 十几秒）。"""
    src = STORE_SRC
    fn = src[src.index("def _load_embedding_model(") : src.index("def get_embed_model(")]
    assert fn.index("embedding_model_ready()") < fn.index("from sentence_transformers import")


def test_no_auto_download_code_anywhere_in_store():
    """老的"缺模型就自动下载"这条路**不许回来**（实测 671MB / 236 秒 / 报错被掩盖）。

    ⚠️ 只查**代码**，不查注释：`embedding_model_hint()` 的文档字符串里**故意**留着
    "ModelScope / redirect_stderr(None)" 这段历史（不写下来，下一个人只会再踩一次）。
    """
    assert "from modelscope import" not in STORE_SRC
    assert "import modelscope" not in STORE_SRC
    assert "snapshot_download(" not in STORE_SRC  # 带括号 = 真的在调用
    assert "with redirect_stderr(" not in STORE_SRC


# ── ④ 精排缺失：优雅降级（与向量模型的"硬报错"不同，这是有意的）─────────────


def test_missing_reranker_degrades_but_does_not_raise(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "RERANKER_PATH", tmp_path / "not-there")
    monkeypatch.setattr(store, "_reranker", None, raising=False)
    monkeypatch.setattr(store, "_reranker_resolved", False, raising=False)
    assert store.reranker_model_ready() is False
    assert store.get_reranker() is None  # 降级成纯向量召回，而不是报错
