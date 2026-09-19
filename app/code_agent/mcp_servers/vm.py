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
VM_UPLOADS_DIR = os.environ.get("CODE_AGENT_VM_UPLOADS_DIR", "/home/leprite/nginx/uploads")

# 沙箱超时（秒），防止死循环命令卡住 Agent
VM_COMMAND_TIMEOUT = int(os.environ.get("CODE_AGENT_VM_TIMEOUT", "60"))

# 危险命令黑名单
_DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\s+/\s",  # rm -rf / (仅匹配根目录，不误拦 /home/...)
    r"\brm\s+-rf\s+/\*",  # rm -rf /*
    r"\bdd\s+if=",  # dd 磁盘操作
    r"\bmkfs\b",  # 格式化
    r"fork\s*bomb",  # fork 炸弹
    r":\(\)\s*\{",  # shell fork bomb
    r"chmod\s+.*777\s+/\s",  # chmod 777 / (仅匹配根目录)
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
    normalized = os.path.abspath(path)
    match = re.match(r"^([a-zA-Z]):[\\/](.*)$", normalized)
    if not match:
        return normalized.replace("\\", "/")

    drive = match.group(1).lower()
    rest = match.group(2).replace("\\", "/")
    return f"/mnt/{drive}/{rest}"


# 在 Ubuntu 虚拟机中创建目录，对应 Linux 的 mkdir -p。
@mcp.tool(name="make_dir_in_vm", description="在 Ubuntu 虚拟机中创建目录，相当于 mkdir -p 命令")
def make_dir_in_vm(
    dir_path: Annotated[
        str, Field(description="要创建的目录路径", examples=[VM_UPLOADS_DIR + "/test3"])
    ],
) -> str:
    sys.stderr.write(f"dir_path {dir_path}\n")
    return run_vm_shell_command("mkdir -p " + shlex.quote(dir_path))


# 查看 Ubuntu 虚拟机中的目录内容，对应 Linux 的 ls -al。
@mcp.tool(name="list_files_in_vm", description="查看 Ubuntu 虚拟机中指定目录，相当于 ls -al 命令")
def list_files_in_vm(
    dir_path: Annotated[str, Field(description="要查看的目录路径", examples=[VM_UPLOADS_DIR])],
) -> str:
    sys.stderr.write(f"dir_path {dir_path}\n")
    return run_vm_shell_command("ls -al " + shlex.quote(dir_path))


# 向 Ubuntu 虚拟机中的指定路径写入文件内容。
@mcp.tool(name="write_file_to_vm", description="向 Ubuntu 虚拟机中写入指定文件")
def write_file_to_vm(
    file_path: Annotated[
        str,
        Field(
            description="写入虚拟机中的文件地址", examples=[VM_UPLOADS_DIR + "/test2/index.html"]
        ),
    ],
    content: Annotated[
        str, Field(description="写入虚拟机中的文件内容", examples=["<div>hello wsl</div>"])
    ],
) -> str:
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
@mcp.tool(name="upload_directory_to_vm", description="将本地文件目录上传至 Ubuntu 虚拟机指定目录")
def upload_directory_to_vm(
    local_dir: Annotated[
        str,
        Field(
            description="本地文件目录",
            examples=["E:/agentstart/work/ai-agent-test/.temp/vue3-test"],
        ),
    ],
    vm_dest_dir: Annotated[
        str, Field(description="虚拟机文件目录", examples=[VM_UPLOADS_DIR + "/vue3-test"])
    ],
) -> str:
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
