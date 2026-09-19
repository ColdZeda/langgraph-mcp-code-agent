"""T4.5 · Redis 工具缓存的测试。

重点是**安全性**而不是命中率：
- 只读白名单与 Verifier 的只读白名单不能漂移；
- 写操作必须让缓存失效（否则"读→写→读"会拿到旧值，这是缓存最阴的坑）；
- Redis 挂了必须**优雅降级**（当作没有缓存，而不是把任务搞崩）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.agent.multi_agent import READONLY_TOOL_NAMES  # noqa: E402
from app.code_agent.utils import tool_cache as tc  # noqa: E402


class _FakeRedis:
    """内存版 Redis（够用即可：get/set/scan_iter/delete）。"""

    def __init__(self):
        self.data: dict[str, str] = {}
        self.set_calls = 0

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.set_calls += 1
        self.data[key] = value

    async def scan_iter(self, match=None, count=None):
        import fnmatch

        for key in list(self.data):
            if fnmatch.fnmatch(key, match or "*"):
                yield key

    async def delete(self, key):
        self.data.pop(key, None)

    async def aclose(self):
        pass


@pytest.fixture
def fake_cache(monkeypatch):
    cache = tc.ToolCache(scope="t", enabled=True)
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr(cache, "_get_client", _client)
    cache._fake = fake
    return cache


# ── 名单不变式 ──


def test_cacheable_is_subset_of_readonly_whitelist():
    """可缓存名单必须是 Verifier 只读白名单的子集。

    否则将来有人往缓存名单里加了个写工具，就会静默跳过副作用 ——
    这条断言就是拦这个的。
    """
    assert tc.CACHEABLE_TOOL_NAMES <= READONLY_TOOL_NAMES


def test_write_tools_are_not_cacheable():
    for name in (
        "write_file",
        "file_delete",
        "move_file",
        "copy_file",
        "execute_powershell_command",
        "mysql_insert_data",
        "mysql_update_data",
        "mysql_delete_data",
        "mysql_create_table",
        "mysql_create_database",
        "mysql_execute_command",
        "save_knowledge",
        "update_knowledge",
        "delete_knowledge",
        "write_file_to_vm",
        "upload_directory_to_vm",
        "make_dir_in_vm",
        "close_powershell",
    ):
        assert name not in tc.CACHEABLE_TOOL_NAMES, f"{name} 是写操作，绝不能缓存"


def test_mysql_query_deliberately_not_cacheable():
    """`mysql_execute_query` 虽然只读，但返回的是**会变的数据**：
    同一句 SELECT 在插入前后答案不同，缓存会让 Agent 拿到过期结果
    （evals 里恰好有"先写再查"的题）。"""
    assert "mysql_execute_query" not in tc.CACHEABLE_TOOL_NAMES
    assert "mysql_list_tables" in tc.CACHEABLE_TOOL_NAMES, "表结构类查询可以缓存"


# ── key ──


def test_cache_key_is_argument_order_independent():
    a = tc.cache_key("s", "read_file", {"path": "x", "start": 1})
    b = tc.cache_key("s", "read_file", {"start": 1, "path": "x"})
    assert a == b, "参数顺序不同、语义相同 → 必须是同一个 key"


def test_cache_key_differs_by_scope_and_args():
    assert tc.cache_key("s1", "read_file", {"p": 1}) != tc.cache_key("s2", "read_file", {"p": 1})
    assert tc.cache_key("s", "read_file", {"p": 1}) != tc.cache_key("s", "read_file", {"p": 2})


# ── 命中 / 未命中 ──


async def test_miss_then_hit(fake_cache):
    assert await fake_cache.get("read_file", {"path": "a"}) is None
    assert fake_cache.misses == 1

    await fake_cache.set("read_file", {"path": "a"}, "内容")
    assert await fake_cache.get("read_file", {"path": "a"}) == "内容"
    assert fake_cache.hits == 1


async def test_invalidate_clears_only_own_scope(fake_cache):
    await fake_cache.set("read_file", {"path": "a"}, "A")
    await fake_cache.set("read_file", {"path": "b"}, "B")
    # 另一个作用域的数据（模拟别的任务/会话）
    other = tc.cache_key("someone-else", "read_file", {"path": "a"})
    fake_cache._fake.data[other] = "别人的"

    removed = await fake_cache.invalidate()

    assert removed == 2
    assert await fake_cache.get("read_file", {"path": "a"}) is None
    assert fake_cache._fake.data[other] == "别人的", "不能把别人的作用域也清了"


# ── 降级 ──


async def test_redis_down_degrades_gracefully(monkeypatch):
    """Redis 连不上 → get 返回 None、set 静默跳过，**都不抛异常**。"""
    cache = tc.ToolCache(scope="t", enabled=True, url="redis://127.0.0.1:1/0")

    assert await cache.get("read_file", {"path": "a"}) is None
    await cache.set("read_file", {"path": "a"}, "内容")  # 不抛
    assert await cache.invalidate() == 0
    assert cache._unavailable is True, "失败一次后应标记不可用，避免每次调用都等超时"


async def test_disabled_cache_never_touches_redis():
    cache = tc.ToolCache(scope="t", enabled=False)
    assert await cache.get("read_file", {"path": "a"}) is None
    await cache.set("read_file", {"path": "a"}, "内容")
    await cache.invalidate()
    assert cache._client is None, "关掉缓存时不该建立连接"
