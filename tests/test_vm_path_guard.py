"""VM 工具的路径防线 —— 2026-10-01 实锤事故的回归测试。

## 事故（有审计日志与磁盘证据）

模型把 **Windows 路径** `E:\\agentstart\\work\\ai-agent-test\\runtime\\workspace\\testprogram`
传给了 `make_dir_in_vm`（这个工具要的是 **WSL 路径**）。`shlex.quote` 把整串包成**一个**参数，
而 **Linux 里反斜杠不是分隔符** ⇒ WSL 在**当前目录**（NTFS 挂载的仓库根）建了一个
"名字就是这串路径"的目录；Windows 又不允许文件名含 `:` ⇒ WSL/驱动层用**私用区替身**写进文件名
（`:` → U+F03A、`\\` → U+F05C）。结果：仓库根多了一个畸形**空目录**，而 `git status` 一直是干净的
（git 不跟踪目录）—— 排查花了三轮才定位。

## 这里守两件事

1. **明确拒绝**：四个 VM 工具的 WSL 侧路径参数收到 Windows 路径 / 含反斜杠 → 抛 `VmPathError`，
   且**消息里带出路**（要操作 Windows 就换 `execute_powershell_command`）。
   ⚠️ 刻意**不**做"自动转换"：静默把 `E:\\…` 转成 `/mnt/e/…` 会掩盖"模型用错了工具"。
2. **根本走不到执行**：打桩 `subprocess.run`，断言拒绝发生在**启动子进程之前**
   （与 `test_dangerous_commands.py` 同款"机械证明"，而不是只看返回值）。
"""

from __future__ import annotations

import pytest

from app.code_agent.mcp_servers import vm

#: 事故里那条真实路径（Windows 盘符 + 反斜杠）
WINDOWS_ABS = r"E:\agentstart\work\ai-agent-test\runtime\workspace\testprogram"
WINDOWS_FILE = r"E:\agentstart\work\ai-agent-test\runtime\workspace\testprogram\index.html"


@pytest.fixture()
def no_exec(monkeypatch):
    """任何"真的去执行 WSL 命令"的行为都直接判测试失败。"""

    def boom(*args, **kwargs):
        raise AssertionError("拒绝必须发生在执行之前：不该走到 subprocess.run")

    monkeypatch.setattr(vm.subprocess, "run", boom)
    monkeypatch.setattr(
        vm,
        "run_vm_shell_command",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("拒绝必须发生在执行之前：不该调用 run_vm_shell_command")
        ),
    )


# ── ① 四个工具的 WSL 侧路径参数：Windows 路径必须被拒 ────────────────────────


def test_make_dir_rejects_windows_path(no_exec):
    with pytest.raises(vm.VmPathError) as ei:
        vm.make_dir_in_vm(WINDOWS_ABS)
    msg = str(ei.value)
    assert "WSL" in msg and "execute_powershell_command" in msg  # 消息必须带出路
    assert "E:" in msg  # 回显收到的值，便于排查


def test_list_files_rejects_windows_path(no_exec):
    with pytest.raises(vm.VmPathError):
        vm.list_files_in_vm(WINDOWS_ABS)


def test_write_file_rejects_windows_path(no_exec):
    with pytest.raises(vm.VmPathError):
        vm.write_file_to_vm(WINDOWS_FILE, "<div>hello</div>")


def test_upload_rejects_windows_dest(no_exec, tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    with pytest.raises(vm.VmPathError):
        vm.upload_directory_to_vm(str(tmp_path), WINDOWS_ABS)


# ── ② 其它"不合格"形态 ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        r"relative\with\backslash",  # 反斜杠（Linux 里根本不是分隔符）
        r"a\b",
        "E:",  # 只有盘符
        "E:/forward/slash/but/drive",  # 正斜杠也救不了盘符路径
        "",  # 空
        "   ",  # 全空白
    ],
)
def test_other_bad_shapes_are_rejected(no_exec, bad):
    with pytest.raises(vm.VmPathError):
        vm.make_dir_in_vm(bad)


# ── ③ 正例：WSL 内路径必须**照常放行**（别把功能一起改坏了）──────────────────


@pytest.mark.parametrize(
    "good",
    [
        "/home/user/nginx/uploads/test3",
        "/mnt/e/agentstart/work/ai-agent-test/runtime/workspace",
        "/tmp/x",
        "relative/posix/path",
    ],
)
def test_good_wsl_paths_pass_through(monkeypatch, good):
    seen: list[str] = []

    def fake_run(command: str) -> str:
        seen.append(command)
        return "OK"

    monkeypatch.setattr(vm, "run_vm_shell_command", fake_run)
    assert vm.make_dir_in_vm(good) == "OK"
    assert good in seen[0]
    assert seen[0].startswith("mkdir -p ")


def test_write_file_positive_path(monkeypatch):
    """正例：WSL 路径写文件照常工作（内部会 mkdir + cp + chmod 三次调用）。"""
    calls: list[str] = []

    def fake_run(command: str) -> str:
        calls.append(command)
        return "OK"

    monkeypatch.setattr(vm, "run_vm_shell_command", fake_run)
    result = vm.write_file_to_vm("/home/user/nginx/uploads/t2/index.html", "<div>hi</div>")
    assert "OK" in result
    assert any("mkdir -p /home/user/nginx/uploads/t2" in c for c in calls)
    assert any("cp " in c for c in calls)


def test_upload_positive_path(monkeypatch, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("x", encoding="utf-8")
    calls: list[str] = []

    def fake_run(command: str) -> str:
        calls.append(command)
        return ""

    monkeypatch.setattr(vm, "run_vm_shell_command", fake_run)
    msg = vm.upload_directory_to_vm(str(tmp_path), "/home/user/nginx/uploads/vue3-test")
    assert "上传" in msg and "1 个文件" in msg
    assert any("mkdir -p /home/user/nginx/uploads/vue3-test" in c for c in calls)


# ── ④ 本地侧转换器不能被改坏（它服务的是 local_dir，方向相反）────────────────


@pytest.mark.parametrize(
    ("windows", "expected_prefix"),
    [
        (r"E:\agentstart\work", "/mnt/e/agentstart/work"),
        ("E:/agentstart/work", "/mnt/e/agentstart/work"),
    ],
)
def test_windows_path_converter_still_works(windows, expected_prefix):
    assert vm.windows_path_to_wsl_path(windows).startswith(expected_prefix)
