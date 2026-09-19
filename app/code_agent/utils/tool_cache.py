"""T4.5 · 只读工具结果缓存（Redis）。

**缓存的是「工具调用的结果」，不是「工具本身」** —— 工具定义在启动时就加载进内存了。

三条安全约束（都是实测/推演出来的，别随手放宽）：

1. **只缓存只读工具**。写操作（`write_file` / `mysql_insert_data` /
   `execute_powershell_command` …）绝不能缓存，否则副作用会被跳过。
2. **写操作发生后清空当前作用域的缓存**。否则「读文件 → 改文件 → 再读」会拿到改之前的旧值。
   （这是缓存最容易出错的地方：命中率越高，错得越隐蔽。）
3. **默认不收 `mysql_execute_query`**。它虽然只读，但返回的是**会变的数据**；
   同一句 SELECT 在「插入前后」答案不同，缓存会让 Agent 拿到过期结果 ——
   而 evals 里恰好有「先写再查」的题。表结构类查询（list/describe）可以缓存。
"""

from __future__ import annotations

import hashlib
import json
import logging

from app.code_agent.config import (
    REDIS_URL,
    TOOL_CACHE_ENABLED,
    TOOL_CACHE_TTL,
)

logger = logging.getLogger(__name__)

# 可缓存的只读工具。
# ⚠️ 必须是 multi_agent.READONLY_TOOL_NAMES（Verifier 只读白名单）的子集 ——
#    tests/test_tool_cache.py 有一条断言守着这个不变式，防止两份名单漂移。
CACHEABLE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        # code_tools（MCP）
        "read_file_range",
        "generate_diff",
        "analyze_ast",
        "list_project_structure",
        # FileManagementToolkit
        "read_file",
        "list_directory",
        "file_search",
        # MySQL 结构（不含 mysql_execute_query，理由见模块 docstring 第 3 条）
        "mysql_list_databases",
        "mysql_list_tables",
        "mysql_describe_tables",
        # RAG 查询
        "query_rag",
    }
)

_KEY_PREFIX = "codeagent:toolcache"


def cache_key(scope: str, tool_name: str, args: dict) -> str:
    """参数完全相同的调用 → 同一个 key（args 排序后再序列化，保证键序无关）。"""
    payload = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{_KEY_PREFIX}:{scope}:{tool_name}:{digest}"


class ToolCache:
    """Redis 工具结果缓存；**Redis 不可用时优雅降级**（当作没有缓存，直接执行工具）。

    `scope`：缓存命名空间。评估时每个任务一个新 scope → 任务之间不会互相串味；
    REPL/Web 用会话 id。跨任务复用缓存看似更省，但会让「上一个任务改过的文件」
    污染「下一个任务的读取」，风险大于收益。
    """

    def __init__(self, scope: str, *, enabled: bool | None = None, url: str | None = None) -> None:
        self.scope = scope
        self.enabled = TOOL_CACHE_ENABLED if enabled is None else enabled
        self.url = url or REDIS_URL
        self._client = None
        self._unavailable = False  # 首次失败后不再反复重试（避免每次调用都等超时）
        self.hits = 0
        self.misses = 0

    # ── 连接 ──

    async def _get_client(self):
        if self._client is not None:
            return self._client
        import redis.asyncio as aioredis

        # 超时给得很短：Redis 只是加速器，不该拖慢主流程
        self._client = aioredis.from_url(
            self.url,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
            decode_responses=True,
        )
        return self._client

    def _degrade(self, exc: Exception) -> None:
        if not self._unavailable:
            self._unavailable = True
            logger.warning("Redis 不可用，工具缓存降级为直连执行：%s: %s", type(exc).__name__, exc)

    # ── 读写 ──

    async def get(self, tool_name: str, args: dict) -> str | None:
        if not self.enabled or self._unavailable:
            return None
        try:
            client = await self._get_client()
            value = await client.get(cache_key(self.scope, tool_name, args))
        except Exception as exc:  # noqa: BLE001 —— 任何 Redis 故障都只降级、不冒泡
            self._degrade(exc)
            return None
        if value is None:
            self.misses += 1
            logger.debug("[工具缓存] 未命中 %s(%s)", tool_name, args)
            return value
        self.hits += 1
        logger.info("[工具缓存] 命中 %s（跳过真实执行）", tool_name)
        return value

    async def set(self, tool_name: str, args: dict, value: str) -> None:
        if not self.enabled or self._unavailable:
            return
        try:
            client = await self._get_client()
            await client.set(
                cache_key(self.scope, tool_name, args), value, ex=TOOL_CACHE_TTL or None
            )
        except Exception as exc:  # noqa: BLE001
            self._degrade(exc)

    async def invalidate(self) -> int:
        """清空当前作用域的缓存（写操作之后调用）。

        用 `SCAN` 而不是 `KEYS`（后者在大库上会阻塞 Redis）。
        """
        if not self.enabled or self._unavailable:
            return 0
        pattern = f"{_KEY_PREFIX}:{self.scope}:*"
        removed = 0
        try:
            client = await self._get_client()
            async for key in client.scan_iter(match=pattern, count=200):
                await client.delete(key)
                removed += 1
        except Exception as exc:  # noqa: BLE001
            self._degrade(exc)
            return 0
        if removed:
            logger.info("[工具缓存] 写操作后失效 %d 条缓存", removed)
        return removed

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None


__all__ = ["CACHEABLE_TOOL_NAMES", "ToolCache", "cache_key"]
