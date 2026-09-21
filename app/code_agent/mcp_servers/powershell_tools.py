"""PowerShell MCP Server — 执行命令 + 进程管理。"""

import io
import subprocess
import sys
import threading
from typing import Annotated

import psutil
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from app.code_agent.config import PROJECT_ROOT

mcp = FastMCP()

# ── 危险命令检测 ──
#
# ⚠️ **阶段 5 订正 #24**：这里是内容级防线里**最要紧的一份** ——
#    `execute_powershell_command` 是**唯一能把原始命令透传下去**的入口
#    （VM 那四个工具都把参数 `shlex.quote` 过，透传不了原始命令）。
#    原版第二条 `\bRemove-Item\s+.*-Recurse\s+-Force\b` 有两个漏口：
#      ① 要求 `-Recurse` 必须写在 `-Force` **前面** → `Remove-Item -Force -Recurse C:\` 漏拦；
#      ② 只认 cmdlet 全名 → `rm C:\ -r -fo` / `del C:\ -r -fo` 漏拦
#         （`rm` / `del` / `erase` / `rd` / `rmdir` / `ri` 在 PowerShell 里**都是 Remove-Item 的别名**）。
#    现改为**两个后瞻**：同一段命令里同时出现"递归参数"和"强制参数"即可，顺序不限、允许 PowerShell 的缩写。
#    注意这**不放松**原有的严格度：合法的 `Remove-Item ./build -Recurse -Force` 原来就被拦，现在还是拦
#    （要删 workspace 里的东西请走 `file_delete` 工具 —— 它受三档权限管，且只能动 workspace）。
_DANGEROUS_POWERSHELL_PATTERNS = [
    r"\bRemove-Item\s+-Path\s+/\*",  # Remove-Item 根目录通配
    # 强制递归删除（顺序无关 + 认别名，见上面的订正说明）
    r"\b(?:Remove-Item|ri|rm|del|erase|rd|rmdir)\b"
    r"(?=[^|;&]*\s-(?:r|rec\w*)\b)(?=[^|;&]*\s-(?:fo|for\w*)\b)",
    r"\bFormat-\w+",  # Format-Volume / Format-HardDisk
    r"\bdel\s+/[fsq]",  # del /f /s /q
    r"\brd\s+/[sq]\b",  # rd /s /q
    r"\brmdir\s+/[sq]\b",  # rmdir /s /q
    r"\bStop-Computer\b",  # 关机
    r"\bRestart-Computer\b",  # 重启
    r"\bshutdown\b",  # shutdown 命令
    r"\bformat\s+[a-zA-Z]:",  # format C: 等
    r"\bdel /[fsq].*system32",  # 删除系统目录
    r"\bRemove-Item.*system32",  # 删除系统目录
    r"\bdiskpart\b",  # 磁盘分区
    r"\bClear-Content\s+.*\.(dll|exe|sys)\b",  # 清空系统文件
]


def _is_dangerous(command: str) -> str | None:
    """检查 PowerShell 命令是否包含危险操作。返回 None 表示安全，返回字符串表示拦截原因。"""
    import re as _re

    for pattern in _DANGEROUS_POWERSHELL_PATTERNS:
        if _re.search(pattern, command, _re.IGNORECASE):
            return f"🚫 安全拦截：PowerShell 命令匹配危险模式 '{pattern}'，已阻止执行。"
    return None


def run_powershell_command(command: str, capture_output: bool = True):
    """执行 PowerShell 命令。"""
    # 安全检查
    danger = _is_dangerous(command)
    if danger:
        sys.stderr.write(danger + "\n")
        return danger, danger, 1
    try:
        cmd = ["powershell", "-Command", command]
        # cwd 固定为项目根：命令的相对路径基准不再取决于"用户从哪个目录启动 Agent"。
        # （Agent 需要按相对路径读 app/…、跑 uv run pytest tests/、git status，基准都必须是项目根）
        if capture_output:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="gbk",
                errors="replace",
            )
            output_buffer = io.StringIO()

            def _read_output():
                for line in proc.stdout:
                    sys.stderr.write(line)
                    sys.stderr.flush()
                    output_buffer.write(line)

            reader = threading.Thread(target=_read_output, daemon=True)
            reader.start()
            proc.wait()
            reader.join(timeout=5)

            full_output = output_buffer.getvalue().strip()
            if proc.returncode != 0:
                return full_output, f"命令返回码: {proc.returncode}", proc.returncode
            return full_output, "", 0
        else:
            # 不捕获输出的分支：输出仍必须走 stderr —— stdout 是 MCP 的 JSON-RPC 通道，
            # 让子进程继承 stdout 会直接污染协议。
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=str(PROJECT_ROOT),
                stdout=sys.stderr,
                stderr=sys.stderr,
                encoding="gbk",
            )
            return "", "", result.returncode
    except Exception as e:
        return "", str(e), 1


def _get_powershell_processes():
    """获取所有 PowerShell 进程（内部辅助函数）。"""
    processes = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.info["name"] and "powershell" in proc.info["name"].lower():
                processes.append(
                    {
                        "pid": proc.info["pid"],
                        "name": proc.info["name"],
                        "cmdline": proc.info["cmdline"],
                    }
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return processes


# ── MCP 工具 ──


@mcp.tool(name="close_powershell", description="关闭所有 PowerShell 进程")
def close_all_powershell() -> str:
    """关闭所有 PowerShell 进程。"""
    try:
        processes = _get_powershell_processes()
        if not processes:
            return "没有找到需要关闭的 PowerShell 进程"

        closed_count = 0
        for proc_info in processes:
            try:
                proc = psutil.Process(proc_info["pid"])
                proc.terminate()
                closed_count += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        return f"已成功关闭 {closed_count} 个 PowerShell 进程"
    except Exception as e:
        return f"关闭 PowerShell 进程失败: {str(e)}"


@mcp.tool(name="execute_powershell_command", description="直接执行 PowerShell 命令并返回结果")
def execute_powershell_command(
    command: Annotated[
        str, Field(description="要执行的 PowerShell 命令", examples=["Get-Process"])
    ],
) -> str:
    """直接执行 PowerShell 命令并返回结果。"""
    try:
        sys.stderr.write("-" * 50 + "\n")
        sys.stderr.write("execute_powershell_command:\n")
        sys.stderr.write(command + "\n")
        sys.stderr.write("-" * 50 + "\n")

        stdout, stderr, returncode = run_powershell_command(command)

        if returncode != 0:
            if stderr:
                return f"命令执行失败: {stderr}"
            return "命令执行失败，但没有错误信息"

        if stdout:
            return f"命令执行成功:\n{stdout}"
        return "命令执行成功，但没有输出"

    except Exception as e:
        return f"执行 PowerShell 命令失败: {str(e)}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
