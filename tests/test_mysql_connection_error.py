"""回归守卫：MySQL 工具在**连接失败**时必须把真实错误透出来（2026-09-24 修 D3）。

旧行为（实测踩过两次）：`execute_query()` 连接失败时 **`return <错误字符串>`**，
而所有调用方写的都是 `result, rowcount = execute_query(...)` ⇒ **在解包处炸掉**，
用户/模型看到的是

    query tables error: too many values to unpack (expected 2)

—— **真正的 `mysql connection error: …` 被吃掉**。

命中记录：
- single 轮 E007 自己写进知识库的诊断笔记：*"用 `mysql_execute_query` 查询**新建数据库** `eval_shop` 的表时
  报错 `query tables error: too many values to unpack`"*；
- multi 轮 E009 的 Verifier 意见里引用了同一错误 —— 而那次 Executor 为此反复自造诊断脚本，
  跑了 33 次工具 / 50 步 / **214,786 token**，最终**没产出 `books.md`**（0.4）。

修法：`execute_query` 的契约固定为「**成功返回二元组，失败一律抛**」，错误信息原样往上冒。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.mcp_servers import mysql_tools as mt  # noqa: E402

CONN_ERROR = "mysql connection error: (1045, 'Access denied for user agent_readonly')"


def test_execute_query_raises_the_real_connection_error(monkeypatch):
    monkeypatch.setattr(mt, "get_connection", lambda *a, **k: CONN_ERROR)

    with pytest.raises(RuntimeError) as excinfo:
        mt.execute_query("SELECT 1")

    assert "mysql connection error" in str(excinfo.value)


def test_readonly_tool_surfaces_connection_error_not_unpack_error(monkeypatch):
    """模型看到的那句话里必须是**真实错误**，不能再是解包错误。"""
    monkeypatch.setattr(mt, "get_connection", lambda *a, **k: CONN_ERROR)

    message = mt.mysql_execute_query(command="SELECT * FROM books", database="eval_lib")

    assert "mysql connection error" in message
    assert "too many values to unpack" not in message


def test_execute_query_returns_rows_on_success(monkeypatch):
    """成功路径的形状不能变：仍是 `(rows, rowcount)`。"""

    class _Cursor:
        rowcount = 2

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, command, params=None):
            return None

        def fetchall(self):
            return [{"id": 1}, {"id": 2}]

    class _Conn:
        def cursor(self, *a, **k):
            return _Cursor()

        def commit(self):
            return None

    import pymysql

    monkeypatch.setattr(mt, "get_connection", lambda *a, **k: _Conn())
    monkeypatch.setattr(mt.pymysql, "Connection", _Conn)  # 让 isinstance 通过

    rows, rowcount = mt.execute_query("SELECT id FROM books")

    assert rows == [{"id": 1}, {"id": 2}]
    assert rowcount == 2
    assert pymysql is not None
