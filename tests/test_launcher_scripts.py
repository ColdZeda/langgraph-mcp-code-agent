"""启动脚本的机械守卫（两次真机事故换来的）。

事故 1（2026-10-09）：`start-app.cmd` 双击后**闪退**。
  根因：`& uv run uvicorn …` 绑定失败（端口被 Windows 保留）**不会**成为 `powershell.exe` 的退出码
  ⇒ `.cmd` 的 `if errorlevel 1 pause` 不触发 ⇒ 窗口直接关闭，用户看不到任何错误。

事故 2（2026-10-09，同一天）：用编辑器改 `start-app.ps1` 时**丢掉了 UTF-8 BOM**。
  `start-app.cmd` 调的是 **Windows PowerShell 5.1**，它读无 BOM 的 `.ps1` 会按 ANSI(GBK) 解 ⇒
  中文变乱码、且**每个中文字符串末尾的引号可能被吞**⇒ 语法直接坏掉（实测解析报
  「Unexpected token」）。而 `pwsh` 7 下一切正常 —— 所以只有双击启动的人会撞上。

这两条都属于"改一次就可能复发"的类别，所以用测试钉住。
"""

from __future__ import annotations

from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
BOM = b"\xef\xbb\xbf"


def test_every_powershell_script_keeps_the_utf8_bom() -> None:
    """`scripts/**/*.ps1` 必须带 UTF-8 BOM —— PS 5.1 读无 BOM 的 UTF-8 会乱码 + 语法错误。"""
    offenders = []
    for path in sorted(SCRIPTS.rglob("*.ps1")):
        head = path.read_bytes()[:3]
        if head != BOM:
            offenders.append(str(path.relative_to(SCRIPTS.parent)))
    assert not offenders, (
        "这些 .ps1 丢了 UTF-8 BOM（Windows PowerShell 5.1 会乱码/解析失败）："
        + "、".join(offenders)
    )


def test_start_app_propagates_exit_code() -> None:
    """`start-app.ps1` 结尾必须把退出码传出去 —— 否则失败时 `.cmd` 的 `pause` 不生效（闪退）。"""
    text = (SCRIPTS / "run" / "start-app.ps1").read_text(encoding="utf-8")
    assert "exit $LASTEXITCODE" in text, "结尾要 `exit $LASTEXITCODE`，否则失败时退出码仍是 0"


def test_start_app_probes_port_by_binding() -> None:
    """端口判据必须是**真 bind**，不能只查"有没有人在监听"（那查不出"被系统保留"）。"""
    text = (SCRIPTS / "run" / "start-app.ps1").read_text(encoding="utf-8")
    assert "TcpListener" in text, "端口可用性要用 TcpListener 实测绑定"
    assert "10013" in text, "要区分 10013（被系统保留）与 10048（被进程占用）"
