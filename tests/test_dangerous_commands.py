"""内容级防线（危险命令黑名单）的回归测试 —— 阶段 5 订正 #24。

**为什么必须打桩 `subprocess.run` / `subprocess.Popen`**：
这才是"拦住"的机械证明。只断言 `_is_dangerous()` 返回非空，证明不了"命令没被执行"；
而**真去打这些命令是不可接受的** —— 阶段 5 实测就踩过：
`rm -rf /` 当时根本没被拦住、**真的进了 WSL**，全靠 GNU rm 自带的 `--preserve-root`
才没出事（官方手册明写它只管"参数就是 `/` 自身"这一种，`rm -rf /*`、`rm -rf /mnt/c/...` 都不管）。
所以这里的断言是：**危险命令根本走不到启动子进程那一步**。

两层各自独立，两侧都要覆盖：
- `vm.py`（WSL 侧；四个 VM 工具都 `shlex.quote` 过参数，属纵深防御）
- `powershell_tools.py`（**唯一能透传原始命令的入口**，内容级防线的主战场）
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.mcp_servers import powershell_tools as ps  # noqa: E402
from app.code_agent.mcp_servers import vm  # noqa: E402

# ═══════════════════════════════════════════════════════════════════
# WSL / vm.py
# ═══════════════════════════════════════════════════════════════════

#: 订正 #24 之前**全部放行**的写法（旧模式要求 `/` 后面还得有空白）
VM_REGRESSION_FORMS = [
    "rm -rf /",  # ← 最经典的一种，`/` 在结尾
    "sudo rm -rf /",
    "rm -fr /",  # flag 顺序不同
    "rm -r -f /",  # flag 分开写
    "rm -R /",  # 大写 + 不带 -f
    "chmod -R 777 /",  # 一模一样的毛病
    "chmod 777 /",
    "chown -R root:root /",
]

VM_OTHER_DANGEROUS = [
    "rm -rf /*",  # shell 展开后每个参数都不是 `/` → GNU rm 的 --preserve-root 不管
    "rm -rf / --no-preserve-root",
    "rm -rf --no-preserve-root /",  # 保护被主动关掉
    "dd if=/dev/zero of=/dev/sda bs=1M count=100",
    "mkfs.ext4 /dev/sda1",
    ":(){ :|:& };:",
    "shutdown -h now",
    "reboot",
]

VM_SAFE = [
    "ls -al /tmp",
    "rm -rf ./build",  # 正常构建清理
    "rm -rf /tmp/foo",  # 目标不是根目录
    "rm -rf build/",  # 末尾斜杠但不是根
    "rm file.txt",
    "rm -r ./dist",
    "cat /etc/passwd",
    "chmod -R 755 ./dist",
]


@pytest.mark.parametrize("command", VM_REGRESSION_FORMS)
def test_vm_regression_forms_are_blocked(command):
    """这几种写法在阶段 5 之前**全部放行** —— 这条测试就是钉住订正 #24。"""
    assert vm._is_dangerous(command) is not None, f"{command!r} 又漏了"


@pytest.mark.parametrize("command", VM_OTHER_DANGEROUS)
def test_vm_other_dangerous_commands_still_blocked(command):
    assert vm._is_dangerous(command) is not None, f"{command!r} 漏了"


@pytest.mark.parametrize("command", VM_SAFE)
def test_vm_safe_commands_are_not_blocked(command):
    """**反面对照**：不能为了堵漏把正常操作也拦了（误拦会让 Agent 直接不可用）。"""
    assert vm._is_dangerous(command) is None, f"{command!r} 被误拦"


@pytest.mark.parametrize("command", VM_REGRESSION_FORMS + VM_OTHER_DANGEROUS)
def test_vm_dangerous_command_never_spawns_a_subprocess(monkeypatch, command):
    """**核心断言**：被拦下的命令绝不允许启动子进程（等价于"真的没执行"）。"""

    def _must_not_run(*args, **kwargs):
        raise AssertionError(f"危险命令竟然走到了启动子进程：{args!r}")

    monkeypatch.setattr(vm.subprocess, "run", _must_not_run)

    result = vm.run_vm_shell_command(command)

    assert "安全拦截" in result, f"{command!r} 没被拦住：{result}"


# ═══════════════════════════════════════════════════════════════════
# PowerShell / powershell_tools.py（原始命令的真正入口）
# ═══════════════════════════════════════════════════════════════════

#: 订正 #24 之前漏拦的写法：参数**顺序反了**、或者用了 PowerShell **别名**
PS_REGRESSION_FORMS = [
    "Remove-Item -Force -Recurse C:\\",  # -Force 写在 -Recurse 前面
    "Remove-Item C:\\ -Recurse -Force",  # 路径夹在中间（旧模式能拦，这里一并钉住）
    "rm C:\\ -Recurse -Force",  # rm = Remove-Item 别名
    "rm C:\\ -r -fo",  # 别名 + PowerShell 缩写
    "del C:\\ -r -fo",
    "erase C:\\ -r -fo",
    "rd C:\\ -r -fo",
    "ri C:\\ -Recurse -Force",  # ri = Remove-Item 的官方短别名
]

PS_OTHER_DANGEROUS = [
    "Remove-Item -Recurse -Force C:\\",
    "Remove-Item -Path /*",
    "del /f /s /q C:\\",
    "rd /s /q C:\\",
    "rmdir /s /q C:\\",
    "Format-Volume -DriveLetter C",
    "format C:",
    "diskpart",
    "Stop-Computer",
    "Restart-Computer",
    "shutdown /s /t 0",
    "Clear-Content C:\\Windows\\System32\\kernel32.dll",
]

PS_SAFE = [
    "Get-Process",
    "Get-ChildItem .",
    "Remove-Item ./temp.txt",  # 删单个文件：不该拦
    "Remove-Item ./dist -Recurse",  # 只有递归、没有强制：不该拦
    'python -c "print(1)"',
    "git status",
    "Get-ChildItem -Recurse -Force",  # 只读列目录，名字里带这两个参数：不该拦
    # ⚠️ 2026-10-07 实测订正：这两个是**纯排版** cmdlet，原来被 `\bFormat-\w+` 误伤
    #    （用户白点一次"允许"、模型多跑 3 步绕开）⇒ 现在必须放行。
    "Get-Process | Format-Table -AutoSize",
    "Get-ChildItem | Format-List Name,Length",
]


@pytest.mark.parametrize("command", PS_REGRESSION_FORMS)
def test_powershell_regression_forms_are_blocked(command):
    """参数顺序反过来、以及用别名写的强制递归删除 —— 阶段 5 之前全部放行。"""
    assert ps._is_dangerous(command) is not None, f"{command!r} 又漏了"


@pytest.mark.parametrize("command", PS_OTHER_DANGEROUS)
def test_powershell_other_dangerous_commands_still_blocked(command):
    assert ps._is_dangerous(command) is not None, f"{command!r} 漏了"


@pytest.mark.parametrize("command", PS_SAFE)
def test_powershell_safe_commands_are_not_blocked(command):
    assert ps._is_dangerous(command) is None, f"{command!r} 被误拦"


@pytest.mark.parametrize("command", PS_REGRESSION_FORMS + PS_OTHER_DANGEROUS)
def test_powershell_dangerous_command_never_spawns_a_subprocess(monkeypatch, command):
    def _must_not_run(*args, **kwargs):
        raise AssertionError(f"危险命令竟然走到了启动子进程：{args!r}")

    monkeypatch.setattr(ps.subprocess, "Popen", _must_not_run)

    stdout, _stderr, returncode = ps.run_powershell_command(command)

    assert "安全拦截" in stdout, f"{command!r} 没被拦住：{stdout}"
    assert returncode == 1, "被拦截时应当返回非零返回码"
