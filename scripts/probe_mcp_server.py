"""MCP server 探针 —— 手工发 JSON-RPC，看某个 MCP server **到底回没回**。

**为什么需要它**（阶段 5 订正 #27 就是靠它定位的）：
从 Agent 那一侧看，"服务端不返回"和"客户端读不到"是同一种症状 —— **都表现成卡死**。
`load_mcp_tools` 那层会把两者混在一起，只有手工发协议才能分清：

  · 服务端**压根没回**（响应不在 stdout 上）
  · 服务端回了**脏东西**（stdout 上混进非 JSON 行 → 协议被污染）
  · 服务端**收下了但没执行**（stderr 上能看到请求分发日志、却没有业务日志）

**典型用法**：

```bash
# 冒烟：这个 server 的工具还能调通吗（超时 = 有问题）
uv run python scripts/probe_mcp_server.py rag query_rag --args '{"query":"MCP"}'

# 只看工具清单（等价于 initialize + tools/list）
uv run python scripts/probe_mcp_server.py rag --list

# 关注册了哪些工具 + 实时看子进程 stderr（排查用）
uv run python scripts/probe_mcp_server.py code_tools read_file_range \\
    --args '{"file_path":"README.md","start_line":1,"end_line":3}' --stderr --timeout 30
```

**退出码**：`0` = 拿到响应；`2` = 超时（服务端没回）；`3` = 子进程提前挂了；`1` = 用法错误。
—— 所以它可以直接塞进脚本/CI：`if ! probe ...; then echo 挂了; fi`。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.config import (  # noqa: E402
    BROWSER_SERVER_PATH,
    CODE_TOOLS_SERVER_PATH,
    MYSQL_SERVER_PATH,
    POWERSHELL_SERVER_PATH,
    PROJECT_ROOT,
    PYTHON_EXECUTABLE,
    RAG_SERVER_PATH,
    VM_SERVER_PATH,
)

#: 短名 → server 脚本。用短名是为了少打字、也避免路径写错。
SERVERS: dict[str, Path] = {
    "powershell": POWERSHELL_SERVER_PATH,
    "rag": RAG_SERVER_PATH,
    "browser": BROWSER_SERVER_PATH,
    "vm": VM_SERVER_PATH,
    "mysql": MYSQL_SERVER_PATH,
    "code_tools": CODE_TOOLS_SERVER_PATH,
}

# MCP 的 stdio 传输是「一行一个 JSON-RPC 消息」，握手按规范来。
PROTOCOL_VERSION = "2024-11-05"
EXIT_OK, EXIT_USAGE, EXIT_TIMEOUT, EXIT_CRASHED = 0, 1, 2, 3


class Probe:
    """一次探针会话：起子进程 → 握手 → 发一条请求 → 等响应。"""

    def __init__(self, server_path: Path, *, echo_stderr: bool = False) -> None:
        env = dict(os.environ)
        # ⚠️ 必须和 app/code_agent/utils/mcp.py 一样把项目根塞进 PYTHONPATH ——
        #    子进程是 `python <路径>/xxx.py` 起的，sys.path[0] 是那个目录、不是项目根。
        env["PYTHONPATH"] = str(PROJECT_ROOT)
        self.proc = subprocess.Popen(
            [PYTHON_EXECUTABLE, str(server_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            bufsize=1,
        )
        self.stdout_lines: list[str] = []
        self.stderr_lines: list[str] = []
        self.echo_stderr = echo_stderr
        threading.Thread(
            target=self._pump, args=(self.proc.stdout, self.stdout_lines), daemon=True
        ).start()
        threading.Thread(
            target=self._pump, args=(self.proc.stderr, self.stderr_lines), daemon=True
        ).start()

    def _pump(self, stream, sink: list[str]) -> None:
        for line in stream:
            text = line.rstrip("\n")
            sink.append(text)
            if self.echo_stderr and sink is not self.stdout_lines:
                print(f"  [server] {text[:200]}")

    def send(self, payload: dict) -> None:
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def wait_for_id(self, msg_id: int, timeout: float) -> str | None:
        """等一条 id 匹配的响应（必须是**合法 JSON** —— 顺带验证协议没被污染）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None and self.proc.returncode is not None:
                return None  # 子进程挂了，不必再等
            for line in list(self.stdout_lines):
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("id") == msg_id:
                    return line
            time.sleep(0.15)
        return None

    def initialize(self, timeout: float) -> bool:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "probe", "version": "0.1"},
                },
            }
        )
        if self.wait_for_id(1, timeout) is None:
            return False
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return True

    def list_tools(self, timeout: float) -> list[str] | None:
        self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        raw = self.wait_for_id(2, timeout)
        if raw is None:
            return None
        try:
            return [t["name"] for t in json.loads(raw)["result"]["tools"]]
        except (KeyError, json.JSONDecodeError):
            return None

    def call_tool(self, name: str, arguments: dict, timeout: float) -> tuple[str | None, float]:
        start = time.time()
        self.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        raw = self.wait_for_id(3, timeout)
        return raw, time.time() - start

    def report_failure(self, name: str) -> None:
        print(f"  ❌ **{name} 没有返回**")
        if self.proc.poll() is not None:
            print(f"     子进程已退出，returncode={self.proc.returncode}")
        print(
            f"     stdout 上收到的行数：{len(self.stdout_lines)}（正常应含 initialize / list / call 的响应）"
        )
        for line in self.stdout_lines:
            print(f"       [stdout] {line[:160]}")
        print("     子进程 stderr 尾部（看它执行到哪一步）：")
        for line in self.stderr_lines[-15:]:
            print(f"       | {line[:160]}")

    def close(self) -> None:
        try:
            self.proc.kill()
        except Exception:  # noqa: BLE001
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MCP server 探针：手工发 JSON-RPC，看服务端到底回没回。",
        epilog="退出码：0=有响应，2=超时（服务端没回），3=子进程挂了，1=用法错误。",
    )
    parser.add_argument("server", choices=sorted(SERVERS), help="MCP server 短名")
    parser.add_argument("tool", nargs="?", help="要调的工具名；配 --list 时可以省略")
    parser.add_argument("--args", default="{}", help='工具参数（JSON），例如 \'{"query":"MCP"}\'')
    parser.add_argument("--list", action="store_true", help="只列工具清单，不调工具")
    parser.add_argument(
        "--timeout", type=float, default=60.0, help="等响应/握手的超时秒数（默认 60）"
    )
    parser.add_argument("--stderr", action="store_true", help="实时打印子进程 stderr（排查用）")
    return parser


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.list and not args.tool:
        parser.error("要么给工具名，要么用 --list")
    if not args.list and args.tool is None:
        parser.error("缺少工具名")

    try:
        arguments = json.loads(args.args)
    except json.JSONDecodeError as exc:
        parser.error(f"--args 不是合法 JSON：{exc}")
        return EXIT_USAGE

    server_path = SERVERS[args.server]
    print(f"== 探针：{args.server}  ({server_path.name}) ==")
    probe = Probe(server_path, echo_stderr=args.stderr)
    try:
        t0 = time.time()
        if not probe.initialize(args.timeout):
            probe.report_failure("initialize")
            return EXIT_CRASHED if probe.proc.poll() is not None else EXIT_TIMEOUT
        print(f"  initialize  ✅ {time.time() - t0:.1f}s")

        tools = probe.list_tools(args.timeout)
        if tools is None:
            probe.report_failure("tools/list")
            return EXIT_TIMEOUT
        print(f"  tools/list  ✅ 共 {len(tools)} 个：{', '.join(tools)}")

        if args.list:
            return EXIT_OK

        raw, elapsed = probe.call_tool(args.tool, arguments, args.timeout)
        if raw is None:
            probe.report_failure(f"tools/call {args.tool}")
            return EXIT_TIMEOUT

        print(f"  tools/call  ✅ {elapsed:.1f}s")
        print(f"  <<< {raw[:400]}")
        return EXIT_OK
    finally:
        probe.close()


if __name__ == "__main__":
    raise SystemExit(main())
