from pathlib import Path

from langchain_mcp_adapters.client import MultiServerMCPClient

from app.code_agent.config import PROJECT_ROOT, PYTHON_EXECUTABLE


async def create_mcp_stdio_client(name: str, params: dict):
    config = {
        name: {
            "transport": "stdio",
            **params,
        }
    }

    client = MultiServerMCPClient(config)
    tools = await client.get_tools()
    return client, tools


async def load_mcp_tools(*, client_id: str, server_path: Path) -> list:
    """统一工厂：根据 server_path 启动 MCP stdio 子进程并返回工具列表。"""
    _client, tools = await _load_mcp_with_client(client_id=client_id, server_path=server_path)
    return tools


async def _load_mcp_with_client(*, client_id: str, server_path: Path) -> tuple:
    import os as _os

    env = dict(_os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    # ⚠️ **必须固定 UTF-8**：Windows 上子进程的 stdout/stderr 默认编码是 **GBK(cp936)**，
    #    而 MCP 客户端按 **UTF-8** 解子进程的 stderr ⇒ 子进程一旦往 stderr 打中文，
    #    关闭连接时就会 `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xce ...`
    #    （实测：RAG 那条集成测试就是被这个卡住的；查了半天才定位到编码，不是逻辑问题）。
    #    stdout 是 JSON-RPC 通道，本来也该是 UTF-8 —— 一起固定，两全。
    env["PYTHONIOENCODING"] = "utf-8"
    params = {
        "command": PYTHON_EXECUTABLE,
        "args": [str(server_path)],
        "env": env,
    }
    return await create_mcp_stdio_client(client_id, params)
