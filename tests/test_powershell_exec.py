"""`execute_powershell_command` 的**执行正确性**回归测试（2026-09-24 修三个缺陷时新增）。

守的是三个**在 E016 上真实翻过车**的缺陷：

| # | 缺陷 | 症状（实测） |
|---|---|---|
| 1 | `shell=True` + 列表 ⇒ 实际走 `cmd.exe /c` | **多行命令在第一个换行处被截断**：那条多行脚本只跑了第一行 `cd runtime/workspace`（静默成功），工具却返回"命令执行成功，但没有输出" |
| 2 | 同上 | **命令里的 `&` 被 cmd 当成分隔符**：`…/sum?a=1&b=2` 里的 `b=2` 变成一条独立命令（`'b' 不是内部或外部命令`），返回码 255 |
| 3 | 固定 `encoding="gbk"` | PowerShell 自己的输出是 GBK，但**它启动的 Python 子进程**可能因继承 `PYTHONIOENCODING=utf-8` 而输出 UTF-8 ⇒ **同一路流混合编码**，固定任何一种都乱码（模型看到 `鎶ュ憡锛氳緭鍏ヤ负 3`） |

⚠️ **为什么必须有"真跑一次"的测试**：缺陷 1/2 的破坏发生在 **cmd.exe 的解析阶段**，
纯打桩（断言"我们传参传对了"）**证明不了它真的不再被截断** —— 只有真起一次子进程才算数。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.mcp_servers import powershell_tools as ps  # noqa: E402

WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="只验证 Windows 上的 cmd.exe 行为"
)


# ═══════════════════════════════════════════════════════════════════
# 一、打桩：守住"我们是怎么调的"
# ═══════════════════════════════════════════════════════════════════


class _FakeProc:
    def __init__(self, lines: list[bytes], returncode: int = 0) -> None:
        self.stdout = iter(lines)
        self.returncode = returncode

    def wait(self) -> int:
        return self.returncode


def _patch_popen(monkeypatch, proc, captured: dict) -> None:
    def _factory(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return proc

    monkeypatch.setattr(ps.subprocess, "Popen", _factory)


def test_popen_is_called_with_shell_false(monkeypatch):
    """**这条是三个缺陷的总闸**：只要 shell=True 回来，cmd.exe 就又会插一手。"""
    captured: dict = {}
    _patch_popen(monkeypatch, _FakeProc([b"ok\n"]), captured)

    ps.run_powershell_command("Write-Output ok")

    assert captured["shell"] is False, "shell=True 会把命令交给 cmd.exe 再解析一遍"


def test_multiline_command_is_passed_through_intact(monkeypatch):
    """多行命令必须**原样**作为 `-Command` 的单个参数传下去，不能少了任何一行。"""
    command = 'cd runtime/workspace\nWrite-Output "第一行"\nWrite-Output "第二行"'
    captured: dict = {}
    _patch_popen(monkeypatch, _FakeProc([b"x\n"]), captured)

    ps.run_powershell_command(command)

    assert captured["cmd"] == ["powershell", "-Command", command]
    assert captured["cmd"][2].count("\n") == 2, "换行被吃掉了"


def test_ampersand_is_not_treated_as_a_separator(monkeypatch):
    """含 `&` 的命令必须原样传下去（`&` 只在 cmd.exe 眼里才是分隔符）。"""
    command = 'curl.exe -s "http://127.0.0.1:8123/sum?a=1&b=2"'
    captured: dict = {}
    _patch_popen(monkeypatch, _FakeProc([b"x\n"]), captured)

    ps.run_powershell_command(command)

    assert captured["cmd"][2] == command


# ═══════════════════════════════════════════════════════════════════
# 二、编码：逐行判定，UTF-8 / GBK 都要能正确解出来
# ═══════════════════════════════════════════════════════════════════


def test_decode_line_handles_utf8_and_gbk():
    assert ps._decode_line("中文 UTF-8\n".encode()) == "中文 UTF-8\n"
    assert ps._decode_line("中文 GBK\n".encode("gbk")) == "中文 GBK\n"


def test_mixed_encoding_output_is_decoded_line_by_line(monkeypatch):
    """**同一路流里混着两种编码**是真实发生过的，必须逐行判。"""
    lines = [
        "PowerShell 自己输出的 GBK 行\n".encode("gbk"),
        "Python 子进程输出的 UTF-8 行\n".encode(),
        b"plain ascii\n",
    ]
    captured: dict = {}
    _patch_popen(monkeypatch, _FakeProc(lines), captured)

    stdout, _stderr, code = ps.run_powershell_command("whatever")

    assert code == 0
    assert "PowerShell 自己输出的 GBK 行" in stdout
    assert "Python 子进程输出的 UTF-8 行" in stdout
    assert "plain ascii" in stdout


# ═══════════════════════════════════════════════════════════════════
# 三、真跑一次（唯一能证明 cmd.exe 那一层真的不再插手的方式）
# ═══════════════════════════════════════════════════════════════════


@WINDOWS_ONLY
def test_real_multiline_command_runs_every_line():
    """真起子进程：多行的**每一行**都必须执行到。

    修复前：只有第一行 `Write-Output "L1"` 会跑（若第一行是静默成功的命令，
    还会伪装成"命令执行成功，但没有输出"）。
    """
    stdout, _stderr, code = ps.run_powershell_command('Write-Output "L1"\nWrite-Output "L2"')
    assert code == 0, stdout
    assert "L1" in stdout and "L2" in stdout, f"第二行没执行：{stdout!r}"


@WINDOWS_ONLY
def test_real_command_with_ampersand_survives():
    """真起子进程：`&` 必须原样到达 PowerShell，不被 cmd 拆开。

    修复前：`b` 会被当成一条独立命令 ⇒ `'b' 不是内部或外部命令` + 返回码 255。
    """
    stdout, _stderr, code = ps.run_powershell_command('Write-Output "a&b"')
    assert code == 0, stdout
    assert "a&b" in stdout, f"`&` 被吃掉了：{stdout!r}"


@WINDOWS_ONLY
def test_real_chinese_output_is_not_garbled():
    """真起子进程：中文不得出现乱码（`鎶ュ憡锛` 这类）。"""
    stdout, _stderr, code = ps.run_powershell_command('Write-Output "报告：输入为 3"')
    assert code == 0, stdout
    assert "报告：输入为 3" in stdout, f"中文乱了：{stdout!r}"
