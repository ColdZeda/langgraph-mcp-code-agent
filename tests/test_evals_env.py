"""`evals/env.py` 的守卫测试（语料隔离，订正 #36）。

守的是"**产品与评估不共用知识库**"这件事 —— 它出问题的方式很隐蔽：
评估跑完，产品会话里被塞进几条**故意写错的**干扰项，用户看到的是"模型莫名其妙说别的"。

| 风险 | 对应测试 |
|---|---|
| 夹具被挪走/没拉全，跑到一半才发现 | `test_missing_fixtures_*` |
| 评估往**仓库**里写东西（模型存知识、复位删文件） | `test_working_copy_lives_under_runtime` |
| 上一轮模型写的知识带到下一轮 | `test_copy_is_fresh_every_time` |
| **入口忘了在 import app 配置之前调用**（环境变量晚一步就不生效） | `test_entrypoints_call_it_before_app_import` |
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import env as E  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


# ═══════════════════════════════════════════════════════════════════
# 夹具本身
# ═══════════════════════════════════════════════════════════════════


def test_fixtures_exist_with_both_subdirs():
    assert E.FIXTURE_KNOWLEDGE_DIR.is_dir()
    for name in ("real_knowledge", "distractors"):
        assert (E.FIXTURE_KNOWLEDGE_DIR / name).is_dir(), f"夹具缺 {name}/"


def test_fixture_is_the_documented_7_files():
    """4 篇正解 + 3 篇干扰 = 7 篇（文档里到处写"35 条 / 7 文件"，这里钉住）。"""
    files = sorted(p.name for p in E.FIXTURE_KNOWLEDGE_DIR.rglob("*.txt"))
    real = [p for p in E.FIXTURE_KNOWLEDGE_DIR.glob("real_knowledge/*.txt")]
    dist = [p for p in E.FIXTURE_KNOWLEDGE_DIR.glob("distractors/*.txt")]
    assert len(files) == 7, f"夹具应该是 7 篇，实际 {len(files)}：{files}"
    assert len(real) == 4 and len(dist) == 3


def test_product_knowledge_dir_has_no_fixture_files():
    """**产品知识库目录里不许再有测试语料** —— 这是订正 #36 的核心不变量。"""
    from app.code_agent.config import KNOWLEDGE_DIR

    leftovers = [p.name for p in KNOWLEDGE_DIR.rglob("*.txt")] if KNOWLEDGE_DIR.exists() else []
    assert leftovers == [], (
        f"产品知识库 {KNOWLEDGE_DIR} 里还有语料：{leftovers} —— "
        "测试语料必须待在 evals/fixtures/knowledge/ 里，产品库要靠使用慢慢积累"
    )


# ═══════════════════════════════════════════════════════════════════
# 隔离动作
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """把工作副本/向量库指到 tmp，并让 monkeypatch 负责还原环境变量 **与 store 的全局量**。

    ⚠️ `use_eval_corpus()` 会改 `store.KNOWLEDGE_DIR/CHROMA_DIR` 并重置它的懒加载单例 ——
    那是**进程级**的，测试里必须能还原，否则会污染同一进程里的其他测试。
    """
    from app.code_agent.rag import store

    monkeypatch.setenv("CODE_AGENT_KNOWLEDGE_DIR", "sentinel-before")
    monkeypatch.setenv("CODE_AGENT_CHROMA_DIR", "sentinel-before")
    monkeypatch.setattr(E, "EVAL_KNOWLEDGE_DIR", tmp_path / "eval_knowledge")
    monkeypatch.setattr(E, "EVAL_CHROMA_DIR", tmp_path / "chroma_db_eval")
    monkeypatch.setattr(store, "KNOWLEDGE_DIR", store.KNOWLEDGE_DIR)
    monkeypatch.setattr(store, "CHROMA_DIR", store.CHROMA_DIR)
    monkeypatch.setattr(store, "_collection", store._collection)
    monkeypatch.setattr(store, "_seeded", store._seeded)
    return tmp_path


def test_use_eval_corpus_copies_and_points_elsewhere(isolated, monkeypatch, capsys):
    snapshot = E.use_eval_corpus(quiet=True)

    assert snapshot["fixtures"] == str(E.FIXTURE_KNOWLEDGE_DIR)
    copied = sorted(p.name for p in (isolated / "eval_knowledge").rglob("*.txt"))
    assert len(copied) == 7, "7 篇夹具都要复制过去"
    assert (isolated / "chroma_db_eval").is_dir()
    # 环境变量要指到评估专用路径 —— MCP 子进程靠继承这两个变量
    import os

    assert os.environ["CODE_AGENT_KNOWLEDGE_DIR"] == str(isolated / "eval_knowledge")
    assert os.environ["CODE_AGENT_CHROMA_DIR"] == str(isolated / "chroma_db_eval")
    capsys.readouterr()


def test_working_copy_lives_under_runtime():
    """工作副本与向量库必须在 gitignore 的 runtime/ 下 —— 评估绝不能往仓库里写东西。"""
    for path in (E.EVAL_KNOWLEDGE_DIR, E.EVAL_CHROMA_DIR):
        assert path.is_relative_to(REPO_ROOT / "runtime"), f"{path} 不在 runtime/ 下"
        assert not path.is_relative_to(E.FIXTURE_KNOWLEDGE_DIR)
        assert not path.is_relative_to(REPO_ROOT / "data")


def test_copy_is_fresh_every_time(isolated):
    """上一轮模型写进知识库的东西，下一轮开跑前必须没了（`use_eval_corpus` 是每轮的第一道闸）。"""
    E.use_eval_corpus(quiet=True)
    stray = isolated / "eval_knowledge" / "上一轮模型写的.txt"
    stray.write_text("模型自己存的经验", encoding="utf-8")

    E.use_eval_corpus(quiet=True)

    assert not stray.exists(), "工作副本要在每次调用时重建（否则残留会带进下一轮）"
    assert len(sorted((isolated / "eval_knowledge").rglob("*.txt"))) == 7


def test_missing_fixtures_raise_with_a_clear_message(monkeypatch, tmp_path):
    monkeypatch.setattr(E, "FIXTURE_KNOWLEDGE_DIR", tmp_path / "nope")
    with pytest.raises(RuntimeError, match="找不到评估语料夹具"):
        E.use_eval_corpus(quiet=True)


def test_incomplete_fixtures_raise(monkeypatch, tmp_path):
    partial = tmp_path / "partial"
    (partial / "real_knowledge").mkdir(parents=True)
    monkeypatch.setattr(E, "FIXTURE_KNOWLEDGE_DIR", partial)
    with pytest.raises(RuntimeError, match="夹具不完整"):
        E.use_eval_corpus(quiet=True)


# ═══════════════════════════════════════════════════════════════════
# 调用顺序（看不见但致命）
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "entry",
    ["evals/run_e2e.py", "evals/rag_bench.py", "evals/rag_ablation.py", "evals/preflight.py"],
)
def test_entrypoints_activate_it_inside_main_only(entry):
    """入口必须在 `main()` 里调用它，**不能在模块顶层** —— import 不该有副作用。

    踩过的坑：一开始把调用放在模块顶层，结果任何 import 这些模块的测试
    （`tests/test_evals_preflight.py`、`tests/test_evals_rag_ablation.py` 都会 import）
    都会改掉进程的环境变量 —— pytest 里 `KNOWLEDGE_DIR` 被指到评估目录，一条测试假失败。
    改成"运行期改 store 的模块级路径"之后就不依赖 import 顺序了。
    """
    source = (REPO_ROOT / entry).read_text(encoding="utf-8")
    main_at = source.find("\ndef main(")
    call_at = source.find("\n    use_eval_corpus()")
    assert main_at != -1, f"{entry} 里找不到 main()"
    assert call_at != -1, f"{entry} 的 main() 里没有调用 use_eval_corpus()"
    assert call_at > main_at, f"{entry}: 调用必须在 main() 之内"
    assert "\nuse_eval_corpus()" not in source, f"{entry} 在模块顶层调用了它（import 副作用）"


def test_accessors_point_at_eval_side_paths():
    """两个访问器必须指到评估自己的路径（`runtime/` 下），而不是产品的 `data/`。"""
    assert E.knowledge_dir() == E.EVAL_KNOWLEDGE_DIR
    assert E.chroma_dir() == E.EVAL_CHROMA_DIR
    for path in (E.knowledge_dir(), E.chroma_dir()):
        assert path.is_relative_to(REPO_ROOT / "runtime")
        assert not path.is_relative_to(REPO_ROOT / "data")


def test_env_module_is_import_safe():
    """`evals/env.py` **顶层**不许 import app 配置 —— 否则 import 它就会改变进程行为。

    （只看行首：docstring 里出现示例代码是允许的。）
    """
    source = (REPO_ROOT / "evals" / "env.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        if line.startswith(("from app.", "import app.")):
            pytest.fail(f"evals/env.py 顶层 import 了 app：{line}")
