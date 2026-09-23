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


def _decode_line(raw: bytes) -> str:
    """按「先 UTF-8、失败退 GBK」解一行输出。

    ⚠️ **不能固定一种编码**（2026-09-24 跑评估时实测出来的）：

      - PowerShell **自己**的输出走**控制台代码页**（中文 Windows 上是 GBK/936）；
      - 而它启动的 **Python 子进程**，只要继承了 `PYTHONIOENCODING=utf-8`
        （评估 runner 就是这么起的），就会按 **UTF-8** 输出；
      - 两者**混在同一路 stdout 里** ⇒ 固定任何一种编码，都会让另一种变成乱码。

    实测症状：模型看到 `鎶ュ憡锛氳緭鍏ヤ负 3`（其实是「报告：输入为 3」），
    于是花了好几轮去"确认文件编码"，还写了个脚本用 `repr()` 抓输出 —— 白烧 token。

    按行解是安全的：`\\n`(0x0A) 既不是 UTF-8 多字节序列的组成部分，也不是 GBK 的尾字节。
    """
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _echo_to_stderr(line: str) -> None:
    """把这一行同时写到 stderr（约定 2：MCP server 的日志只能走 stderr）。

    ⚠️ 必须兜异常：写线程是 daemon 线程，它一崩就**静默丢输出**，而且没有任何报错。
    """
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except (UnicodeEncodeError, ValueError):  # pragma: no cover - 取决于终端编码
        pass


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
        #
        # ⚠️⚠️ **shell 必须是 False**（2026-09-24 修）。原先是 `shell=True` + 列表，
        #    在 Windows 上 `subprocess` 会把列表拼成字符串交给 `cmd.exe /c`，
        #    也就是实际执行的是 `cmd.exe /c powershell -Command "<整条命令>"` ——
        #    **于是 cmd.exe 先解析一遍**，造成两个实测事故（都发生在 E016）：
        #      ① **多行命令在第一个换行处被截断**：那条多行脚本只跑了第一行
        #         `cd runtime/workspace`（静默成功）⇒ 工具返回"命令执行成功，但没有输出"，
        #         模型据此判断"多行脚本未真正执行"（它判断对了，但白花了好几轮）。
        #      ② **命令里的 `&` 被当成 cmd 的命令分隔符**：请求 `…/sum?a=1&b=2` 时，
        #         `b=2` 变成一条独立命令（日志里 `'b' 不是内部或外部命令`），返回码 255。
        #    回归测试：`tests/test_powershell_exec.py`。
        if capture_output:
            proc = subprocess.Popen(
                cmd,
                shell=False,
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                # ⚠️ 刻意**不用** `text=True, encoding=…`：同一路流可能是混合编码，
                #    交给 `_decode_line` 逐行判。见它的 docstring。
            )
            output_buffer = io.StringIO()

            def _read_output():
                for raw in proc.stdout:
                    line = _decode_line(raw)
                    _echo_to_stderr(line)
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
                shell=False,
                cwd=str(PROJECT_ROOT),
                stdout=sys.stderr,
                stderr=sys.stderr,
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
