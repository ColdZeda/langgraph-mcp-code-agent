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


def _has_latin_supplement(text: str) -> bool:
    """文本里有没有落在 **U+0080–U+02FF**（拉丁补充 / 拉丁扩展-A/B）的字符？

    这个区间是"**GBK 被误当 UTF-8 解开**"的典型指纹：`目录`(GBK) 的字节
    `C4 BF C2 BC` **恰好也是合法 UTF-8**，解出来是 `Ŀ¼` —— 两个字符都在这个区间里。
    正常的中文/英文输出几乎不会用到这一段。
    """
    return any(0x80 <= ord(ch) <= 0x2FF for ch in text)


def _decode_line(raw: bytes) -> str:
    """按「优先 UTF-8；**解出来像拉丁乱码就换 GBK**」解一行输出。

    ⚠️ 为什么不能只写"先 UTF-8、失败退 GBK"（那是第一版，2026-09-24 实测不够）：

      - GBK 的字节序列**有时恰好也是合法 UTF-8** —— 于是永远轮不到 GBK 分支。
        实例：PowerShell 的中文表头 `目录: ` 在 GBK 下是 `C4 BF C2 BC ...`，
        UTF-8 也解得开，但结果是 `Ŀ¼: `（乱码）。**模型看到的就是这个乱码。**
      - 所以判据要加上"**解出来的东西是不是拉丁补充区的怪字符**"（见 `_has_latin_supplement`）。

    为什么不能固定一种编码（2026-09-24 跑评估时实测）：

      - PowerShell **自己**的输出走**控制台代码页**（中文 Windows 上是 GBK/936）；
      - 而它启动的 **Python 子进程**，只要继承了 `PYTHONIOENCODING=utf-8`
        （评估 runner 就是这么起的），就会按 **UTF-8** 输出；
      - 两者**混在同一路 stdout 里** ⇒ 固定任何一种编码，都会让另一种变成乱码。

    实测症状：模型看到 `鎶ュ憡锛氳緭鍏ヤ负 3`（其实是「报告：输入为 3」），
    于是花了好几轮去"确认文件编码"，还写了个脚本用 `repr()` 抓输出 —— 白烧 token。

    按行解是安全的：`\\n`(0x0A) 既不是 UTF-8 多字节序列的组成部分，也不是 GBK 的尾字节。

    ⚠️ **已知的假阳性**（刻意接受）：真正的 UTF-8 拉丁文本（如 `café`）也会命中
    `_has_latin_supplement` → 被按 GBK 解成 `caf茅`。本项目的输出是中文 + 英文，
    出现带音标拉丁字母的概率极低，代价（一个词乱码）远小于上面那种整行乱码。
    """
    utf8: str | None = None
    try:
        utf8 = raw.decode("utf-8")
    except UnicodeDecodeError:
        pass

    if utf8 is not None and not _has_latin_supplement(utf8):
        return utf8

    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        pass
    return utf8 if utf8 is not None else raw.decode("utf-8", errors="replace")


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
