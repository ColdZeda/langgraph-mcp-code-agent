"""测试 mysql_tools.py 中的 _safe_ident 防注入函数。"""

import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.mcp_servers.mysql_tools import _safe_ident


class TestSafeIdent:
    """SQL 标识符转义测试。"""

    def test_normal_table_name(self):
        """正常表名：用反引号包裹。"""
        assert _safe_ident("users") == "`users`"
        assert _safe_ident("agent_test") == "`agent_test`"

    def test_empty_string(self):
        """空字符串：也应包裹。"""
        assert _safe_ident("") == "``"

    def test_sql_injection_attempt(self):
        """注入攻击：内部反引号被双重转义，整个字符串被包裹。"""
        result = _safe_ident("users`; DROP DATABASE test; --")
        # 内部的反引号 ` 变成 ``，外部再加一层反引号
        assert result == "`users``; DROP DATABASE test; --`"
        # 确认不会出现未包裹的 SQL 关键字
        assert "DROP DATABASE" not in result.split("`")[0]

    def test_table_with_backtick(self):
        """表名本身包含反引号：双重转义。"""
        assert _safe_ident("a`b") == "`a``b`"

    def test_numbers_and_underscores(self):
        """数字和下划线不触发转义。"""
        assert _safe_ident("tbl_2024_v2") == "`tbl_2024_v2`"
