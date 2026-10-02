import os
import posixpath
import re
import shlex
import subprocess
import sys
import tempfile
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

mcp = FastMCP()

# WSL_DISTRO 表示要操作的 WSL 发行版名称；先查环境变量中CODE_AGENT_WSL_DISTRO指定的系统，无则Ubuntu,默认使用你当前的 Ubuntu。
WSL_DISTRO = os.environ.get("CODE_AGENT_WSL_DISTRO", "Ubuntu")
VM_UPLOADS_DIR = os.environ.get("CODE_AGENT_VM_UPLOADS_DIR", "/home/user/nginx/uploads")

# 沙箱超时（秒），防止死循环命令卡住 Agent
VM_COMMAND_TIMEOUT = int(os.environ.get("CODE_AGENT_VM_TIMEOUT", "60"))

# 危险命令黑名单（**内容级**防线：同一个工具、这次参数危不危险 —— 不随三档权限模式变化）
#
# ⚠️ **阶段 5 订正 #24**：原版把最经典的写法漏了。旧模式是 `\brm\s+-rf\s+/\s` ——
#    它要求 `/` 后面**还得有一个空白字符**，而 `rm -rf /` 的 `/` 正好在结尾 →
#    **匹配不上、直接放行**。同样漏的还有 `sudo rm -rf /`、`rm -fr /`（flag 顺序不同）、
#    `rm -r -f /`（flag 分开写）、`chmod -R 777 /`（一模一样的毛病）。
#
#    当时没出事**只是因为** GNU rm 自带 `--preserve-root`（默认开）会拒绝"参数就是 `/` 自身"这一种，
#    而官方手册明写它**只管这一种**：`rm -rf /*`（shell 展开后每个参数都不是 `/`）、
#    `rm -rf /mnt/c/...` 它都不管，`--no-preserve-root` 更是主动把保护关掉；
#    `chmod` / `chown` / `chgrp` 递归操作 `/` 则**默认就不保护**。
#    → 结论：能不能删根**必须我们自己的黑名单说了算**，不能指望命令自带的开关。
#
#    ⚠️ 顺带记一个**可达性事实**（免得误判风险面）：VM 的四个工具
#    （`make_dir_in_vm` / `list_files_in_vm` / `write_file_to_vm` / `upload_directory_to_vm`）
#    **都把参数 `shlex.quote` 过**，没有任何工具能透传原始命令 → 这份黑名单在 VM 侧属于**纵深防御**；
#    真正能跑任意命令的入口是 `execute_powershell_command`（见 powershell_tools.py 的对应订正）。
#
#    回归测试：`tests/test_dangerous_commands.py`（打桩 `subprocess.run`，
#    断言危险命令**根本走不到启动子进程那一步** —— 这才是"拦住"的机械证明）。
_DANGEROUS_PATTERNS = [
    # rm 递归删除根目录：两个 flag 用后瞻匹配（**顺序无关**），`/` 后面允许是空白 / 结尾 / 通配符。
    # 覆盖 `rm -rf /`、`sudo rm -rf /`、`rm -fr /`、`rm -r -f /`、`rm -R /`、`rm -rf /*`、
    # `rm -rf / --no-preserve-root`；**不**误拦 `rm -rf ./build`、`rm -rf /tmp/foo`、`rm -rf build/`。
    r"\brm\b(?=[^|;&]*\s--?[a-z]*r)[^|;&]*\s+/(?:\s|$|\*)",
    # chmod / chown / chgrp 递归作用在根目录上（这三种命令**默认没有** --preserve-root 保护）
    r"\bch(?:mod|own|grp)\b(?=[^|;&]*\s--?[a-z]*r)[^|;&]*\s+/(?:\s|$|\*)",
    # 把根目录权限放开（非递归也一样是灾难）
    r"\bch(?:mod|own|grp)\b[^|;&]*\b777\b[^|;&]*\s+/(?:\s|$|\*)",
    r"\bdd\s+if=",  # dd 磁盘操作
    r"\bmkfs\b",  # 格式化
    r"fork\s*bomb",  # fork 炸弹
    r":\(\)\s*\{",  # shell fork bomb
    r">\s*/dev/sda",  # 写入磁盘设备
    r"\bshutdown\b",  # 关机/重启
    r"\breboot\b",
    r"\bpoweroff\b",
    r"\bhalt\b",
]


def _is_dangerous(command: str) -> str | None:
    """检查命令是否包含危险操作。返回 None 表示安全，返回字符串表示拦截原因。"""
    import re as _re

    for pattern in _DANGEROUS_PATTERNS:
        if _re.search(pattern, command, _re.IGNORECASE):
            return f"🚫 安全拦截：命令匹配危险模式 '{pattern}'，已阻止执行。"
    return None


def run_vm_shell_command(command: str) -> str:
    # 安全检查
    danger = _is_dangerous(command)
    if danger:
        return danger

    try:
        shell_command = ["wsl", "-d", WSL_DISTRO, "--", "bash", "-lc", command]
        sys.stderr.write(f"shell_command {shell_command}\n")

        res = subprocess.run(
            shell_command,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=VM_COMMAND_TIMEOUT,
        )

        if res.returncode != 0:
            return res.stderr
        return res.stdout
    except subprocess.TimeoutExpired:
        return f"⏱️ 命令执行超时（>{VM_COMMAND_TIMEOUT}秒），已中断。"
    except Exception as e:
        return str(e)


# 把 Windows 路径转换成 WSL Ubuntu 可以识别的 /mnt/盘符 路径。
def windows_path_to_wsl_path(path: str) -> str:
    # ⚠️ 先按**语法**判断"像不像 Windows 盘符路径"，再决定要不要 `os.path.abspath`。
    #    为什么（2026-10-02 CI 实测）：在 **Linux** 上 `os.path.abspath("E:\\a\\b")` 会把它
    #    当**相对路径**、前面拼上当前目录 —— 实测得到
    #    `/home/runner/work/<repo>/<repo>/E:/agentstart/work`，于是盘符正则永远匹配不上
    #    （CI 上就是这么红的）。先看语法 ⇒ 这个转换函数**跨平台结果一致**；
    #    而在 Windows 上结果与改造前**完全相同**（`abspath("E:\\a\\b")` 本来就是 `E:\a\b`）。
    raw = path or ""
    direct = re.match(r"^([a-zA-Z]):[\\/](.*)$", raw)
    if direct:
        return f"/mnt/{direct.group(1).lower()}/{direct.group(2).replace('\\', '/')}"

    normalized = os.path.abspath(raw)
    match = re.match(r"^([a-zA-Z]):[\\/](.*)$", normalized)
    if not match:
        return normalized.replace("\\", "/")

    drive = match.group(1).lower()
    rest = match.group(2).replace("\\", "/")
    return f"/mnt/{drive}/{rest}"


# ── VM 侧路径校验（2026-10-01 实锤事故之后加的防线）──────────────────────────
#
# 事故现场：模型把 **Windows 路径** `E:\…\runtime\workspace\testprogram` 传给了下面的
# `make_dir_in_vm`（这个工具要的是 **WSL 路径**）。`shlex.quote` 把整串包成**一个**参数，
# 而 **Linux 里反斜杠不是路径分隔符** ⇒ WSL 就在当前目录（NTFS 挂载的仓库根）建了一个
# "名字就是这串路径"的目录；又因为 Windows 文件名不允许 `:`，WSL/驱动层改用**私用区替身**
# 写进文件名（`:`→U+F03A、`\`→U+F05C）—— 于是仓库根出现一个谁也看不懂的畸形目录
# （**空目录**，所以 git 完全看不见它，`git status` 一直是干净的）。
#
# 结论：**VM 工具只接受 WSL 内路径**；收到 Windows 路径或含反斜杠的路径一律**明确拒绝**。
# ⚠️ 刻意**不**做"自动转换"：静默把 `E:\…` 转成 `/mnt/e/…` 会掩盖"模型用错了工具"这件事，
# 让它一直错下去；明确报错才能把它推回正确的工具（PowerShell / 文件工具）。
# 回归测试：`tests/test_vm_path_guard.py`（打桩 subprocess，断言**根本走不到执行那一步**）。
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class VmPathError(ValueError):
    """VM 工具的路径参数不是 WSL 内路径（Windows 盘符路径，或含反斜杠）。"""


def ensure_wsl_path(value: str, *, tool: str, arg: str) -> str:
    """校验 VM 侧路径参数，不合格就抛 `VmPathError`（消息里必须带**出路**）。

    合格形态：`/home/user/…`、`/mnt/e/…`、`/tmp/…` —— 绝对/相对都行，**就是不能含 `\\`**。
    """
    text = (value or "").strip()
    if not text:
        raise VmPathError(f"{tool} 的 {arg} 不能为空：请给一个 WSL 路径，例如 {VM_UPLOADS_DIR}")
    if "\\" in text or _WINDOWS_DRIVE_RE.match(text):
        raise VmPathError(
            f"{tool} 只接受 **WSL 内路径**（例如 {VM_UPLOADS_DIR} 或 /mnt/e/…），"
            f"但 {arg} 收到的是 Windows 路径：{value!r}。\n"
            "两条出路：① 要操作 Windows 侧的文件 → 改用 execute_powershell_command（或文件工具）；"
            "② 确实要在 WSL 里操作 → 把路径写成 `/mnt/<盘符小写>/…` 的形式。"
        )
    return text


# 在 Ubuntu 虚拟机中创建目录，对应 Linux 的 mkdir -p。
@mcp.tool(
    name="make_dir_in_vm",
    description=(
        "在 Ubuntu(WSL) 虚拟机中创建目录，相当于 Linux 的 mkdir -p。"
        "⚠️ 只接受 **WSL 内路径**（如 /home/user/… 或 /mnt/e/…）；"
        "要操作 Windows 侧的文件/目录请改用 execute_powershell_command 或文件工具。"
    ),
)
def make_dir_in_vm(
    dir_path: Annotated[
        str,
        Field(description="要创建的目录路径（WSL 内路径）", examples=[VM_UPLOADS_DIR + "/test3"]),
    ],
) -> str:
    dir_path = ensure_wsl_path(dir_path, tool="make_dir_in_vm", arg="dir_path")
    sys.stderr.write(f"dir_path {dir_path}\n")
    return run_vm_shell_command("mkdir -p " + shlex.quote(dir_path))


# 查看 Ubuntu 虚拟机中的目录内容，对应 Linux 的 ls -al。
@mcp.tool(
    name="list_files_in_vm",
    description=(
        "查看 Ubuntu(WSL) 虚拟机中指定目录，相当于 Linux 的 ls -al。"
        "⚠️ 只接受 **WSL 内路径**（如 /home/user/… 或 /mnt/e/…）。"
    ),
)
def list_files_in_vm(
    dir_path: Annotated[
        str, Field(description="要查看的目录路径（WSL 内路径）", examples=[VM_UPLOADS_DIR])
    ],
) -> str:
    dir_path = ensure_wsl_path(dir_path, tool="list_files_in_vm", arg="dir_path")
    sys.stderr.write(f"dir_path {dir_path}\n")
    return run_vm_shell_command("ls -al " + shlex.quote(dir_path))


# 向 Ubuntu 虚拟机中的指定路径写入文件内容。
@mcp.tool(
    name="write_file_to_vm",
    description=(
        "向 Ubuntu(WSL) 虚拟机中写入指定文件。"
        "⚠️ file_path 只接受 **WSL 内路径**（如 /home/user/… 或 /mnt/e/…）；"
        "要写 Windows 侧的文件请改用文件工具（write_file）或 execute_powershell_command。"
    ),
)
def write_file_to_vm(
    file_path: Annotated[
        str,
        Field(
            description="写入虚拟机中的文件地址（WSL 内路径）",
            examples=[VM_UPLOADS_DIR + "/test2/index.html"],
        ),
    ],
    content: Annotated[
        str, Field(description="写入虚拟机中的文件内容", examples=["<div>hello wsl</div>"])
    ],
) -> str:
    file_path = ensure_wsl_path(file_path, tool="write_file_to_vm", arg="file_path")
    with tempfile.NamedTemporaryFile(
        delete=False, mode="w", encoding="utf-8", newline=""
    ) as tmp_file:
        tmp_file.write(content)
        tmp_file_path = tmp_file.name

    sys.stderr.write(f"本地临时文件已创建 {tmp_file_path}\n")
    try:
        tmp_file_wsl_path = windows_path_to_wsl_path(tmp_file_path)
        vm_dir = posixpath.dirname(file_path)
        result = run_vm_shell_command(
            f"mkdir -p {shlex.quote(vm_dir)} && "
            f"cp {shlex.quote(tmp_file_wsl_path)} {shlex.quote(file_path)}"
        )
        change_file_permission_in_vm(file_path, "755")
        return result or f"写入文件成功：{file_path}"
    finally:
        try:
            os.unlink(tmp_file_path)
        except OSError:
            pass


# 修改 Ubuntu 虚拟机中文件或目录的权限，当前作为内部辅助函数使用。
def change_file_permission_in_vm(file_path: str, mode: str) -> str:
    return run_vm_shell_command(f"chmod {shlex.quote(mode)} {shlex.quote(file_path)}")


# 把 Windows 本地目录上传到 Ubuntu 虚拟机目录，并尽量保持原有目录结构。
@mcp.tool(
    name="upload_directory_to_vm",
    description=(
        "将本地(Windows)文件目录上传至 Ubuntu(WSL) 虚拟机指定目录。"
        "⚠️ 注意区分两个参数：`local_dir` 是 **Windows 本地路径**（会被自动转成 /mnt/…），"
        "而 `vm_dest_dir` 只接受 **WSL 内路径**（如 /home/user/… 或 /mnt/e/…）。"
    ),
)
def upload_directory_to_vm(
    local_dir: Annotated[
        str,
        Field(
            description="本地文件目录（Windows 路径）",
            examples=["E:/agentstart/work/ai-agent-test/.temp/vue3-test"],
        ),
    ],
    vm_dest_dir: Annotated[
        str,
        Field(
            description="虚拟机文件目录（**WSL 内路径**）",
            examples=[VM_UPLOADS_DIR + "/vue3-test"],
        ),
    ],
) -> str:
    vm_dest_dir = ensure_wsl_path(vm_dest_dir, tool="upload_directory_to_vm", arg="vm_dest_dir")
    local_dir = os.path.abspath(local_dir)
    if not os.path.exists(local_dir):
        msg = f"本地目录不存在：{local_dir}"
        sys.stderr.write(f"[UPLOAD] {msg}\n")
        return msg

    if not os.path.isdir(local_dir):
        msg = f"指定路径不是文件夹：{local_dir}"
        sys.stderr.write(f"[UPLOAD] {msg}\n")
        return msg

    make_dir_in_vm(vm_dest_dir)
    copied_count = 0

    for root, dirs, files in os.walk(local_dir):
        if "node_modules" in dirs:
            dirs.remove("node_modules")

        if ".git" in dirs:
            dirs.remove(".git")

        sys.stderr.write(f"{root} {dirs} {files}\n")

        rel_path = os.path.relpath(root, local_dir)
        vm_subdir = (
            vm_dest_dir
            if rel_path == "."
            else posixpath.join(vm_dest_dir, rel_path.replace("\\", "/"))
        )
        make_dir_in_vm(vm_subdir)

        for file_name in files:
            local_file_path = os.path.join(root, file_name)
            local_file_wsl_path = windows_path_to_wsl_path(local_file_path)
            vm_file_path = posixpath.join(vm_subdir, file_name)
            result = run_vm_shell_command(
                f"cp {shlex.quote(local_file_wsl_path)} {shlex.quote(vm_file_path)}"
            )
            sys.stderr.write(f"{result}\n")
            copied_count += 1

    change_file_permission_in_vm(vm_dest_dir, "755")
    return f"上传 [{local_dir}] 目录至 {WSL_DISTRO}:[{vm_dest_dir}] 目录成功，共上传 {copied_count} 个文件"


if __name__ == "__main__":
    mcp.run(transport="stdio")
