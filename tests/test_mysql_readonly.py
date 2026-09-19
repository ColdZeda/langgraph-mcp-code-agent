"""MySQL 只读工具的语句白名单测试（T1.3）。

背景：`mysql_execute_query` 挂在 Verifier 的只读工具白名单里，但 MySQL 的 DDL
（DROP/ALTER/TRUNCATE）会**隐式提交**，`commit=False` 挡不住 → 必须有应用层白名单。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.mcp_servers.mysql_tools import READONLY_STATEMENTS, _is_readonly_sql

REJECT = [
    "DROP TABLE evals",
    "TRUNCATE TABLE evals",
    "ALTER TABLE evals ADD COLUMN x INT",
    "UPDATE evals SET status='x'",
    "DELETE FROM evals",
    "INSERT INTO evals VALUES (1)",
    "CREATE TABLE t (id INT)",
    "  drop table evals  ",  # 前后空白 + 大小写
    "(DROP TABLE evals)",  # 括号开头
    "SELECT 1; DROP TABLE evals",  # 多语句
    "SELECT * FROM evals; DELETE FROM evals;",
    "",  # 空语句
]

ALLOW = [
    "SELECT * FROM evals",
    "select count(*) from evals",
    "SHOW TABLES",
    "DESCRIBE evals",
    "DESC evals",
    "EXPLAIN SELECT 1",
    "WITH cte AS (SELECT 1 AS a) SELECT * FROM cte",
    "SELECT * FROM evals;",  # 合法的尾部单个分号
]


@pytest.mark.parametrize("sql", REJECT)
def test_rejects_non_readonly_and_multi_statement(sql):
    ok, reason = _is_readonly_sql(sql)
    assert ok is False, f"{sql!r} 应被拒绝"
    assert reason, "拒绝时必须给出原因（模型需要知道为什么被拒）"


@pytest.mark.parametrize("sql", ALLOW)
def test_allows_readonly_statements(sql):
    ok, reason = _is_readonly_sql(sql)
    assert ok is True, f"{sql!r} 应被放行，但被拒：{reason}"


def test_whitelist_content():
    """白名单只包含只读语句。"""
    assert READONLY_STATEMENTS == {"SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "WITH"}
