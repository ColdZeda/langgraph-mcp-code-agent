"""真集成测试：**真去调 MCP 工具**（阶段 7 · T7.4）。

与单元测试的分工（这是本文件存在的唯一理由）：
- `tests/test_tool_wrap.py` / `test_permissions.py` 那类用**打桩**，证明"**逻辑对**"
  （权限判定必须在缓存前、结果外置的豁免名单、缓存命中要补二元组…）；
- 这一层**真起 stdio 子进程、真连 MySQL、真跑 WSL 命令、真搜一次**，证明"**线接对了**"。
  ⚠️ 阶段 5 那次 RAG 工具**死锁**（调用永不返回、副作用却已经发生）就是单元测试抓不到、
  只能靠人工跑一个真实任务才发现的 —— 这几条就是那类问题的最小回归网。

⚠️ **默认不跑**：`pyproject.toml` 的 `addopts` 里有 `-m "not integration"`，
   所以平时那句 `uv run python -m pytest tests/ -q` 依旧是"快且全绿"的那一套（CI 也是这套）。
   要专门跑这一层（**本机**，4 个容器与 WSL 都开着时）：

       uv run python -m pytest -m integration -v

   环境不可用时（没起容器 / 没有 WSL）**自动 skip**，不会红 —— 因为这一层**只能在 Windows 本机跑**：
   GitHub 的 runner 是 Linux，**没有 WSL**（MySQL/Redis/SearXNG 倒是能在 CI 里用 service 容器配，
   但配了也测不全，所以本项目不把它放进 CI）。
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.config import (  # noqa: E402
    BROWSER_SERVER_PATH,
    MYSQL_PORT,
    MYSQL_SERVER_PATH,
    RAG_SERVER_PATH,
    REDIS_URL,
    SEARXNG_URL,
    VM_SERVER_PATH,
)
from app.code_agent.utils.mcp import load_mcp_tools  # noqa: E402
from app.code_agent.utils.tool_cache import ToolCache  # noqa: E402

pytestmark = pytest.mark.integration

# 单次工具调用上限：防的正是"永不返回"那类死锁把整个测试挂死
CALL_TIMEOUT = 90.0


def _tcp_ok(host: str, port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, int(port))) == 0


def _host_port(url: str, default_port: int) -> tuple[str, int]:
    parts = urlsplit(url)
    return parts.hostname or "127.0.0.1", parts.port or default_port


def _wsl_ok() -> bool:
    try:
        done = subprocess.run(  # noqa: S603
            ["wsl", "-e", "echo", "ok"], capture_output=True, timeout=20, check=False
        )
        return done.returncode == 0
    except Exception:  # noqa: BLE001 - 没装 WSL / 超时，都算"环境不可用"
        return False


# ⚠️ 端口**一律从 config 取**，不要硬编码 —— 踩过一次：
#    曾经把搜索探测写成 8123，结果永远 skip。而 8123 其实是**评估题 E016 里被测应用**的端口
#    （`evals/preflight.py` 的 `HTTP_TEST_PORT`），跟 SearXNG（`SEARXNG_URL`，默认 8888）毫无关系。
MYSQL_HOST, MYSQL_PORT_ = "127.0.0.1", int(MYSQL_PORT)
REDIS_HOST, REDIS_PORT = _host_port(REDIS_URL, 6379)
SEARCH_HOST, SEARCH_PORT = _host_port(SEARXNG_URL, 8888)

MYSQL_UP = _tcp_ok(MYSQL_HOST, MYSQL_PORT_)
REDIS_UP = _tcp_ok(REDIS_HOST, REDIS_PORT)
SEARXNG_UP = _tcp_ok(SEARCH_HOST, SEARCH_PORT)
WSL_UP = _wsl_ok()

skip_no_mysql = pytest.mark.skipif(
    not MYSQL_UP, reason=f"MySQL({MYSQL_HOST}:{MYSQL_PORT_}) 没起：.\\scripts\\run\\start-deps.ps1"
)
skip_no_redis = pytest.mark.skipif(
    not REDIS_UP, reason=f"Redis({REDIS_HOST}:{REDIS_PORT}) 没起：.\\scripts\\run\\start-deps.ps1"
)
skip_no_search = pytest.mark.skipif(
    not SEARXNG_UP, reason=f"SearXNG({SEARCH_HOST}:{SEARCH_PORT}) 没起：.\\scripts\\run\\start-deps.ps1"
)
skip_no_wsl = pytest.mark.skipif(not WSL_UP, reason="WSL 不可用（这条只能在 Windows 本机跑）")


async def _call(server_path: Path, client_id: str, tool_name: str, **kwargs) -> str:
    """起一个 MCP server、找到工具、调一次，返回纯文本。"""
    tools = await load_mcp_tools(client_id=client_id, server_path=server_path)
    tool = next((t for t in tools if t.name == tool_name), None)
    assert tool is not None, f"{client_id} server 里没有 {tool_name}，实际有：{[t.name for t in tools]}"
    result = await asyncio.wait_for(tool.ainvoke(kwargs), timeout=CALL_TIMEOUT)
    return result if isinstance(result, str) else str(result)


# ── ① MySQL：真连库、真跑一条只读查询 ──────────────────────────────


@skip_no_mysql
async def test_mysql_tool_reaches_real_server():
    text = await _call(MYSQL_SERVER_PATH, "mysql", "mysql_list_databases")
    # information_schema 是 MySQL 自带库，任何一本 MySQL 都有 —— 拿它当"我真的连上了"的判据
    assert "information_schema" in text, f"没在库列表里看到 information_schema：{text[:300]}"


# ── ② WSL：真起一次 WSL 命令（这条 CI 永远跑不了）────────────────────


@skip_no_wsl
async def test_vm_tool_really_runs_in_wsl():
    text = await _call(VM_SERVER_PATH, "vm", "list_files_in_vm", dir_path="/tmp")
    # `ls -al` 一定会列出 `.` 这个目录项，权限位里有 drwx
    assert "drwx" in text, f"WSL 里 ls -al /tmp 的输出不像目录列表：{text[:300]}"


# ── ③ SearXNG：真搜一次 ────────────────────────────────────────────


@skip_no_search
async def test_search_tool_reaches_searxng():
    text = await _call(
        BROWSER_SERVER_PATH, "browser", "search_in_searxng", query="MCP protocol", max_results=3
    )
    assert text.strip(), "搜索返回空"
    assert not any(mark in text for mark in ("搜索失败", "Traceback", "ConnectError")), (
        f"搜索报了错：{text[:300]}"
    )


# ── ④ Redis：真写一次、真读一次（工具缓存那条链）────────────────────


@skip_no_redis
async def test_tool_cache_roundtrip_on_real_redis():
    # ⚠️ 必须显式 enabled=True：tests/conftest.py 为了测试隔离把工具缓存**全局关掉**了
    cache = ToolCache("integration-test", enabled=True)
    args = {"probe": "itest"}
    await cache.set("itest_probe", args, "cached-value")
    assert await cache.get("itest_probe", args) == "cached-value", "写进去的值读不回来（Redis 没通？）"
    await cache.invalidate()
    assert await cache.get("itest_probe", args) is None, "invalidate() 之后还能读到旧值"
    await cache.aclose()


# ── ⑤ RAG：真查一次知识库（守的是"永不返回"那类死锁，订正 #27）─────────


async def test_rag_tool_answers_without_hanging():
    # ⚠️ 这条**故意不加 skipif**：RAG 是纯本地库（chromadb + 本地 embedding），
    #    没有外部依赖；它的风险是"**永远不返回**"（阶段 5 的死锁），
    #    所以判据就是"在 CALL_TIMEOUT 内拿到非空结果"。
    text = await _call(RAG_SERVER_PATH, "rag", "query_rag", query="MCP 协议")
    assert text.strip(), "RAG 查询返回空"
