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
    params = {
        "command": PYTHON_EXECUTABLE,
        "args": [str(server_path)],
        "env": env,
    }
    return await create_mcp_stdio_client(client_id, params)
