"""`scripts/fetch_models.py` 的回归测试（**全程不联网**）。

守三件事：

1. **默认目录 = 仓库的上级目录**，不许写死任何人的个人路径
   （用户 2026-10-02 明确要求："你要从一个用户的角度去考虑才对"）；
2. **就位判据**与 `store.py` 的加载判据一致（都看 `model.safetensors`）；
3. `--dry-run` **绝不下载**（打桩下载函数，一旦被调用就判失败）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "fetch_models.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("fetch_models_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fm = _load_module()


# ── ① 默认位置：跟着仓库走，不写死个人路径 ──────────────────────────────────


def test_default_root_follows_config_not_a_hardcoded_path():
    """默认下载根目录必须**跟着 `config.py` 的默认值走**（不写死任何人的路径）。

    ⚠️ **这里刻意不断言"等于仓库的上级目录"**：用户按 `.env.example` 设了
    `CODE_AGENT_EMBEDDING_MODEL_PATH` 之后，那就不成立了 —— 2026-10-02 就是这句话
    在"模型路径被显式配置"的环境下变红。断言**关系**（脚本默认根 == config 路径的上两级）
    才是真正想锁的东西：**路径来自配置，不是硬编码**。
    """
    from app.code_agent.config import EMBEDDING_MODEL_PATH

    assert fm.DEFAULT_ROOT == EMBEDDING_MODEL_PATH.parent.parent


def test_default_root_is_used_when_no_target_given(tmp_path, monkeypatch):
    """不传 `--target` 时，落到默认根（而不是当前目录）—— 用打桩下载器验证。"""
    seen: list[str] = []

    def fake_download(repo_id, files, dest, *, endpoint):
        seen.append(str(dest))
        dest.mkdir(parents=True, exist_ok=True)
        for name in files:
            p = dest / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x", encoding="utf-8")

    monkeypatch.setattr(fm, "download_with_hf", fake_download)
    monkeypatch.setattr(fm, "DEFAULT_ROOT", tmp_path)
    assert fm.main(["--only", "reranker"]) == 0
    assert seen and seen[0].startswith(str(tmp_path))


def test_no_personal_path_in_script():
    """脚本里不许出现作者本机的个人目录名（这曾经是个真 bug 的来源）。"""
    src = SCRIPT.read_text(encoding="utf-8")
    assert ("agent" + "start") not in src
    assert ("lep" + "rite") not in src


def test_target_dir_layout_matches_config():
    """目录布局必须和 config.py 的默认路径拼法一致：<root>/<repo_id>。"""
    assert fm.target_dir("embedding", Path("/tmp/x")) == Path(
        "/tmp/x/sentence-transformers/all-MiniLM-L6-v2"
    )
    assert fm.target_dir("reranker", Path("/tmp/x")) == Path(
        "/tmp/x/cross-encoder/ms-marco-MiniLM-L-6-v2"
    )


# ── ② 就位判据 ──────────────────────────────────────────────────────────────


def test_missing_files_reports_everything_first(tmp_path):
    miss = fm.missing_files("reranker", tmp_path)
    assert len(miss) == len(fm.REQUIRED_FILES["reranker"][1])
    assert "model.safetensors" in miss


def test_missing_files_empty_when_all_present(tmp_path):
    dest = fm.target_dir("embedding", tmp_path)
    dest.mkdir(parents=True)
    for name in fm.REQUIRED_FILES["embedding"][1]:
        p = dest / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    assert fm.missing_files("embedding", tmp_path) == []


def test_required_set_covers_store_judgement():
    """脚本的必需文件里必须含 `model.safetensors` —— 那是 store.py 的就位判据。"""
    assert "model.safetensors" in fm.REQUIRED_FILES["embedding"][1]
    assert "model.safetensors" in fm.REQUIRED_FILES["reranker"][1]
    # 精排的判据是 config.json（见 store.reranker_model_ready）
    assert "config.json" in fm.REQUIRED_FILES["reranker"][1]


# ── ③ dry-run 与下载流程（都把下载函数打桩，绝不真联网）──────────────────────


def test_dry_run_never_downloads(monkeypatch, tmp_path, capsys):
    def boom(*args, **kwargs):
        raise AssertionError("--dry-run 不该触发任何下载")

    monkeypatch.setattr(fm, "download_with_hf", boom)
    monkeypatch.setattr(fm, "download_with_modelscope", boom)

    code = fm.main(["--only", "reranker", "--target", str(tmp_path), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "只打印计划" in out
    assert not fm.target_dir("reranker", tmp_path).exists()  # 什么都没建


def test_successful_download_creates_required_files(monkeypatch, tmp_path, capsys):
    def fake_download(repo_id, files, dest, *, endpoint):
        dest.mkdir(parents=True, exist_ok=True)
        for name in files:
            p = dest / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x", encoding="utf-8")

    monkeypatch.setattr(fm, "download_with_hf", fake_download)
    code = fm.main(["--only", "embedding", "--target", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "✅" in out
    assert fm.missing_files("embedding", tmp_path) == []


def test_already_installed_skips_download(monkeypatch, tmp_path, capsys):
    """幂等：已就位就跳过，**连下载函数都不调用**。"""
    dest = fm.target_dir("reranker", tmp_path)
    dest.mkdir(parents=True)
    for name in fm.REQUIRED_FILES["reranker"][1]:
        p = dest / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")

    def boom(*args, **kwargs):
        raise AssertionError("已就位不该再下载")

    monkeypatch.setattr(fm, "download_with_hf", boom)
    code = fm.main(["--only", "reranker", "--target", str(tmp_path)])
    assert code == 0
    assert "已就位" in capsys.readouterr().out


def test_failed_download_returns_nonzero_and_hints_alternative(monkeypatch, tmp_path, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(fm, "download_with_hf", boom)
    code = fm.main(["--only", "embedding", "--target", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "下载失败" in out
    assert "modelscope" in out  # 必须给出下一步


@pytest.mark.parametrize("kind", ["embedding", "reranker"])
def test_partial_download_is_detected(monkeypatch, tmp_path, capsys, kind):
    """只下了一半（没有 model.safetensors）也必须判失败，不能报成功。"""

    def fake_download(repo_id, files, dest, *, endpoint):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "config.json").write_text("x", encoding="utf-8")  # 故意只写一个

    monkeypatch.setattr(fm, "download_with_hf", fake_download)
    code = fm.main(["--only", kind, "--target", str(tmp_path)])
    assert code == 1
    assert "仍缺文件" in capsys.readouterr().out
