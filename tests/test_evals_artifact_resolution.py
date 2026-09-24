"""回归守卫：产物解析**不能把项目自己的文件当成选手的产物**（2026-09-24 修 D4）。

实测怎么踩到的（E011，修复后重跑那一轮）：

- 题面要求 `utils.py` / `report.py` / `main.py` 三个文件互相 import，能 `python main.py` 跑出含 `6` 的输出；
- 模型那次把三个文件建在了 **`tri_import/` 子目录**里；
- 于是 `RunContext.resolve("main.py")` 在 workspace 找不到，**回退到仓库根** ——
  而仓库根**真有** `main.py`（**项目自己的 CLI 入口**）⇒ 判定器把它当成了选手的产物：
  - `py_compile_ok("main.py")` **假通过**；
  - `no_fabricated_success("main.py")` **假通过**（"产物在：E:\\…\\ai-agent-test\\main.py"）；
  - `python_script_stdout("main.py")` **真的把 Code Agent 自己启动了一遍**
    （断言抓到的 stdout 是 `执行模式：auto / 权限模式：需确认…` 那套启动横幅）。

修法：裸文件名 + **被 git 跟踪** ⇒ 视为项目自带文件，**不做仓库根回退**。
⚠️ 带目录的路径（`app/code_agent/config.py`）**不受影响** —— 那类断言本来就要读项目文件。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import verifiers as V  # noqa: E402


def _ctx(tmp_path: Path, project: Path) -> V.RunContext:
    return V.RunContext(
        task_id="E011", mode="single", result={}, workspace=tmp_path, project=project
    )


def test_project_file_is_not_treated_as_an_artifact(tmp_path):
    """仓库根的 `main.py`（项目自己的入口）**不能**被当成选手产物。"""
    project = tmp_path / "repo"
    project.mkdir()
    (project / "main.py").write_text("print('我是项目自己的入口')\n", encoding="utf-8")
    ws = tmp_path / "workspace"
    ws.mkdir()

    ctx = _ctx(ws, project)
    # 把仓库根那个 main.py 登记成"被 git 跟踪的项目文件"（不打桩真跑 git）
    V._TRACKED_FILES = {"main.py"}

    assert ctx.resolve("main.py") is None, "项目自己的 main.py 被当成产物了（D4）"


def test_workspace_artifact_still_wins(tmp_path):
    """选手真的在 workspace 里造了 `main.py` ⇒ 正常解析（不受 D4 修复影响）。"""
    project = tmp_path / "repo"
    project.mkdir()
    (project / "main.py").write_text("print('项目自己的')\n", encoding="utf-8")
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "main.py").write_text("print('选手的')\n", encoding="utf-8")

    ctx = _ctx(ws, project)
    V._TRACKED_FILES = {"main.py"}

    assert ctx.resolve("main.py") == ws / "main.py"


def test_untracked_file_at_repo_root_is_still_accepted(tmp_path):
    """选手用 PowerShell 把产物写在**仓库根**（未跟踪的新文件）⇒ 仍要能找到（保留原有的宽容）。"""
    project = tmp_path / "repo"
    project.mkdir()
    (project / "hello.py").write_text("print('选手写在仓库根的产物')\n", encoding="utf-8")
    ws = tmp_path / "workspace"
    ws.mkdir()

    ctx = _ctx(ws, project)
    V._TRACKED_FILES = {"main.py"}  # hello.py 不在跟踪名单里

    assert ctx.resolve("hello.py") == project / "hello.py"


def test_qualified_project_paths_are_unaffected(tmp_path):
    """带目录的路径（断言"项目文件有没有被动过"）**不受影响**。"""
    project = tmp_path / "repo"
    project.mkdir()
    nested = project / "app" / "code_agent"
    nested.mkdir(parents=True)
    (nested / "config.py").write_text("X = 1\n", encoding="utf-8")
    ws = tmp_path / "workspace"
    ws.mkdir()

    ctx = _ctx(ws, project)
    V._TRACKED_FILES = {"main.py", "app/code_agent/config.py"}

    assert ctx.resolve("app/code_agent/config.py") == nested / "config.py"


def test_real_repo_marks_its_own_main_py_as_project_file():
    """真仓库自检：`main.py` 确实是被 git 跟踪的项目文件（防这条规则空转）。"""
    V._TRACKED_FILES = None  # 让懒加载真去读一次 git
    assert V._is_project_owned("main.py") is True
    assert V._is_project_owned("utils.py") is False  # 仓库根没有它
    assert V._is_project_owned("app/code_agent/config.py") is False  # 带目录 ⇒ 不走这条规则
