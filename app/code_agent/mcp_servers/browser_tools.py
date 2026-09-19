"""搜索 MCP Server —— 直接调 SearXNG 的 JSON API，不需要浏览器。

阶段 2 / T2.1 的改造：原先用 Selenium 驱动 Edge 取搜索页 HTML（228 行，还需要匹配版本
的 msedgedriver + 调试端口），但 SearXNG 本身就提供 JSON 输出 → 取数用一次 HTTP 请求即可。

⚠️ 边界：本工具只负责「**搜索取数**」。将来若要做「**操作真实网页**」（Computer Use /
Browser Agent，需要点击、填表、截图），那是另一件事，应另建 Playwright 工具。
"""

import sys
from typing import Annotated

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from app.code_agent.config import SEARXNG_URL

mcp = FastMCP()

# 统一 stdio 为 UTF-8（Windows 控制台默认 GBK；只在支持时调用，便于被测试导入）
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


@mcp.tool(
    description=(
        "在 SearXNG 搜索引擎中搜索关键词，返回结构化结果"
        "（标题 / URL / 摘要 / 来源引擎 / 结果总数）。"
        "用于实时信息搜索（新闻、最新内容、网络资料）"
    )
)
def search_in_searxng(
    query: Annotated[str, Field(description="搜索关键词", examples=["Python asyncio 教程"])],
    max_results: Annotated[int, Field(description="最多返回多少条结果", examples=[8])] = 8,
) -> str:
    """调 SearXNG 的 JSON API 搜索（不经过浏览器）。"""
    try:
        resp = httpx.get(
            f"{SEARXNG_URL}/search",
            params={"q": query, "format": "json", "language": "auto"},
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        return f"搜索出错: {type(e).__name__}: {e}"

    results = payload.get("results") or []
    if not results:
        return "未找到相关结果"

    shown = results[:max_results]
    lines = [
        f"[{i}] {r.get('title', '(无标题)')}\n"
        f"    URL: {r.get('url', '')}\n"
        f"    来源引擎: {r.get('engine', '未知')}\n"
        f"    {r.get('content', '')}"
        for i, r in enumerate(shown, 1)
    ]

    # 结果总数一并返回（原先靠数 HTML 里的 <article> 得到，现在用 JSON 数组长度）
    header = f"共找到 {len(results)} 条结果，返回前 {len(shown)} 条"
    unresponsive = payload.get("unresponsive_engines") or []
    if unresponsive:
        names = ", ".join(
            str(x[0]) if isinstance(x, (list, tuple)) and x else str(x) for x in unresponsive
        )
        header += f"\n（以下引擎本次无响应：{names}）"

    return header + "\n\n" + "\n\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="stdio")
