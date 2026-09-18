import json
import os
import subprocess
import sys
import time
from typing import Annotated
from urllib.parse import quote
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup, Comment
from mcp.server.fastmcp import FastMCP
from pydantic import Field
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
from app.code_agent.config import SEARXNG_URL

# MCP 通过 stdio 传输数据。Windows 默认控制台编码可能是 GBK，
# 搜索结果里如果有特殊字符，直接输出可能会编码失败，所以这里统一改成 UTF-8。
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

mcp = FastMCP()

# 显式指定 Edge 浏览器路径；msedgedriver 交给 Selenium Manager 自动匹配 Edge 版本，
# 避免硬编码版本（如 149.0.4022.62）在 Edge 升级后失配导致 CDP 连接失败。
EDGE_BROWSER_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
EDGE_DEBUGGER_ADDRESS = "127.0.0.1:9333"
EDGE_DEBUG_USER_DATA_DIR = r"C:\temp\edge-debug"

# True 表示后台无头运行，不弹出 Edge 窗口；False 表示弹出窗口，方便教学观察。
EDGE_HEADLESS = True


# 检查 Edge 的 9333 调试端口是否已经可用。
def is_edge_debugger_ready() -> bool:
    try:
        with urlopen(f"http://{EDGE_DEBUGGER_ADDRESS}/json/version", timeout=1):
            return True
    except Exception:
        return False


# 确保调试版 Edge 中至少有一个普通页面可供 Selenium 接管。
def ensure_edge_debugger_page() -> None:
    try:
        with urlopen(f"http://{EDGE_DEBUGGER_ADDRESS}/json/list", timeout=1) as response:
            targets = json.load(response)
        if any(target.get("type") == "page" for target in targets):
            return
    except Exception:
        pass

    request = Request(f"http://{EDGE_DEBUGGER_ADDRESS}/json/new?about:blank", method="PUT")
    with urlopen(request, timeout=2):
        pass


# 切到普通网页标签页，避免 Selenium 接到 edge://newtab 这类内部页面后行为不稳定。
def switch_to_normal_page(driver) -> None:
    fallback_handle = None
    for handle in driver.window_handles:
        driver.switch_to.window(handle)
        current_url = driver.current_url
        if fallback_handle is None:
            fallback_handle = handle
        if not current_url.startswith(("edge://", "chrome://")):
            return

    if fallback_handle:
        driver.switch_to.window(fallback_handle)


# 自动启动带远程调试端口的 Edge；如果已经启动，就直接复用。
def start_edge_with_debugger() -> None:
    if is_edge_debugger_ready():
        ensure_edge_debugger_page()
        return

    edge_args = [
        EDGE_BROWSER_PATH,
        "--remote-debugging-port=9333",
        f"--user-data-dir={EDGE_DEBUG_USER_DATA_DIR}",
    ]
    if EDGE_HEADLESS:
        edge_args.extend(["--headless=new", "--disable-gpu"])
    edge_args.append("about:blank")

    subprocess.Popen(
        edge_args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    for _ in range(20):
        time.sleep(0.5)
        if is_edge_debugger_ready():
            ensure_edge_debugger_page()
            return

    raise RuntimeError(f"Edge 调试端口未启动成功: http://{EDGE_DEBUGGER_ADDRESS}/json/version")


# 获取可复用的 Edge driver 实例，其他浏览器工具都从这里拿 driver。
def get_edge_instance():
    start_edge_with_debugger()

    edge_options = EdgeOptions()
    edge_options.add_experimental_option("debuggerAddress", EDGE_DEBUGGER_ADDRESS)
    # 不指定 executable_path：Selenium Manager 自动下载与 Edge 版本匹配的 msedgedriver
    service = EdgeService()

    driver = None
    try:
        driver = webdriver.Edge(service=service, options=edge_options)
        switch_to_normal_page(driver)
        driver.current_url
    except Exception:
        try:
            if driver:
                driver.quit()
        except Exception:
            pass

        ensure_edge_debugger_page()
        try:
            driver = webdriver.Edge(service=service, options=edge_options)
            switch_to_normal_page(driver)
            driver.current_url
        except Exception as e:
            raise RuntimeError(
                "无法连接到可用的 Edge 标签页。\n"
                "程序已尝试自动启动 Edge 调试窗口，可手动检查命令：\n"
                f'& "{EDGE_BROWSER_PATH}" --remote-debugging-port=9333 --user-data-dir="{EDGE_DEBUG_USER_DATA_DIR}"\n'
                f"然后确认 http://{EDGE_DEBUGGER_ADDRESS}/json/version 可以打开。"
            ) from e
    return driver


# MCP 正式搜索工具：返回 SearXNG 搜索结果区域的瘦身 HTML。
@mcp.tool(description="在 SearXNG 搜索引擎中搜索关键词，返回简化后的结果 HTML。用于实时信息搜索（新闻/最新内容/网络资料）")
def search_in_searXNG_with_html(query: Annotated[str, Field(description="搜索关键词", examples=["Python asyncio 教程"])]) -> str:
    driver = get_edge_instance()

    try:
        page_html_list = []

        # SearXNG 的 pageno 从 1 开始；当前只抓第一页，避免返回内容过长。
        for page_num in range(1, 2):
            search_url = (
                f"{SEARXNG_URL}/search"
                f"?q={quote(query)}&pageno={page_num}&language=auto"
            )
            driver.get(search_url)

            # 等待搜索结果区域加载完成。
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "urls"))
            )

            # 滚动到底部，触发可能存在的懒加载内容。
            last_height = driver.execute_script("return document.body.scrollHeight")
            while True:
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(2)
                new_height = driver.execute_script("return document.body.scrollHeight")
                if new_height == last_height:
                    break
                last_height = new_height

            # 提取搜索结果区域的 innerHTML，交给 pretty_html 做瘦身。
            results_area = driver.find_element(By.ID, "urls")
            page_html = results_area.get_attribute("innerHTML")
            page_html_list.append(f"===== page {page_num} HTML =====\n{page_html}")

        html = "\n\n\n".join(page_html_list)
        return pretty_html(html)

    except Exception as e:
        return f"搜索出错: {e}"

    finally:
        driver.quit()


# 清洗 SearXNG 搜索结果 HTML，删除无用标签、无关属性和包装结构。
def pretty_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # 教程里的 display:none 清理更适配 Baidu；SearXNG 的 #urls 里这类内容很少，所以先保留思路但不启用。
    # display_none_re = re.compile(r"display\s*:\s*none", re.IGNORECASE)
    # for tag in soup.find_all(True):
    #     if display_none_re.search(tag.get("style", "")):
    #         tag.extract()
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    # 删除脚本、样式、图片、SVG 等 Agent 不需要阅读的标签。
    for tag in soup(["script", "style", "link", "meta", "symbol", "path", "canvas", "svg", "img"]):
        tag.extract()

    # 删除 SearXNG 搜索结果中的辅助区域：搜索来源、快照、缩略图链接、布局占位等。
    for tag in soup.select(".engines, .cache_link, .thumbnail_link, .break"):
        tag.extract()

    # 删除搜索结果顶部重复展示的 URL 面包屑，减少噪音。
    for tag in soup.select(".url_header"):
        tag.extract()

    # 清理标签属性：只给 a 标签保留 href，其它展示属性全部删除。
    for tag in soup.find_all(True):
        if tag.name == "a" and tag.get("href"):
            tag.attrs = {"href": tag["href"]}
        else:
            tag.attrs = {}

    # 拆掉无语义的 div/span 包装，保留其中的文字和链接。
    for tag in soup(["div", "span"]):
        tag.unwrap()

    return str(soup)


if __name__ == "__main__":
    mcp.run(transport="stdio")
