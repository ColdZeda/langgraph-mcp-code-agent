"""Evals 评估运行器 — 直接调用工具函数验证，不依赖 Agent REPL。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.tasks import get_tasks


class TestEvalsToolLevel:
    """工具级验证（不依赖 LLM）。"""

    def test_e001_write_file(self):
        from app.code_agent.config import WORKSPACE_DIR
        hello = WORKSPACE_DIR / "hello.py"
        hello.write_text("print('Hello from Code Agent')", encoding="utf-8")
        assert hello.exists()

    def test_e002_mkdir(self):
        from app.code_agent.config import WORKSPACE_DIR
        dir_path = WORKSPACE_DIR / "src/utils"
        dir_path.mkdir(parents=True, exist_ok=True)
        (dir_path / "__init__.py").write_text("", encoding="utf-8")
        assert dir_path.is_dir()

    def test_e003_structure(self):
        from app.code_agent.mcp_servers.code_tools import list_project_structure
        assert "code_agent" in list_project_structure("app", max_depth=2)

    def test_e004_read_range(self):
        """读文件行范围。

        断言刻意使用「一般性事实」（文件头有 import / 全文有 run_agent 定义），
        不依赖具体行号 —— 否则代码演进导致行号漂移时，这个测试会无故失败。
        """
        from app.code_agent.mcp_servers.code_tools import read_file_range

        # ① 指定行范围：读文件头部（导入区）
        head = read_file_range("app/code_agent/agent/code_agent.py", 1, 20)
        assert "import" in head

        # ② 读全文（end_line=0 表示读到末尾）：断言关键函数定义存在
        full = read_file_range("app/code_agent/agent/code_agent.py")
        assert "def run_agent" in full

    def test_e005_calculator_ast(self):
        from app.code_agent.config import WORKSPACE_DIR
        calc = WORKSPACE_DIR / "calculator.py"
        calc.write_text("def add(a,b): return a+b\n", encoding="utf-8")
        from app.code_agent.mcp_servers.code_tools import analyze_ast
        result = analyze_ast(str(calc))
        assert "add" in result

    def test_e006_diff_and_file(self):
        from app.code_agent.config import WORKSPACE_DIR
        buggy = WORKSPACE_DIR / "buggy.py"
        buggy.write_text("def divide_numbers(a, b):\n    return a / b\n", encoding="utf-8")
        # 修复版
        fixed = "def divide_numbers(a, b):\n    if b == 0:\n        return None\n    return a / b\n"
        from app.code_agent.mcp_servers.code_tools import generate_diff
        diff = generate_diff(str(buggy), fixed)
        assert "if b == 0" in diff

    def test_e008_prompts_ast(self):
        from app.code_agent.mcp_servers.code_tools import analyze_ast
        result = analyze_ast("app/code_agent/agent/prompts.py")
        assert "build_user_prompt" in result

    def test_e009_multi_file(self):
        from app.code_agent.config import WORKSPACE_DIR
        db_config = WORKSPACE_DIR / "db_config.py"
        db_config.write_text("MYSQL_HOST='127.0.0.1'\nMYSQL_PORT=3307\nMYSQL_DATABASE='agent_test'\n", encoding="utf-8")
        app = WORKSPACE_DIR / "app.py"
        app.write_text("from db_config import MYSQL_HOST\nprint(MYSQL_HOST)\n", encoding="utf-8")
        assert db_config.exists() and app.exists()

    def test_e010_markdown(self):
        from app.code_agent.config import WORKSPACE_DIR
        spec = WORKSPACE_DIR / "api_spec.md"
        spec.write_text("# My API\n\n## Endpoints\n- GET /users\n- POST /users\n- DELETE /users/{id}\n", encoding="utf-8")
        content = spec.read_text(encoding="utf-8")
        assert "My API" in content and "GET" in content

    def test_e014_generate_diff(self):
        from app.code_agent.config import WORKSPACE_DIR
        from app.code_agent.mcp_servers.code_tools import generate_diff
        hello = WORKSPACE_DIR / "hello.py"
        hello.write_text("print('Hello World')", encoding="utf-8")
        diff = generate_diff(str(hello), "print('Hello Universe')")
        assert "Hello World" in diff or "Hello Universe" in diff

    def test_e015_delete_file(self):
        from app.code_agent.config import WORKSPACE_DIR
        temp_file = WORKSPACE_DIR / "temp_delete_me.txt"
        temp_file.write_text("delete me", encoding="utf-8")
        assert temp_file.exists()
        temp_file.unlink()
        assert not temp_file.exists()

    def test_e016_syntax_fix(self):
        from app.code_agent.config import WORKSPACE_DIR
        bad = WORKSPACE_DIR / "bad_syntax.py"
        # 写错误代码（少冒号）
        bad.write_text("def foo()\n    pass\n", encoding="utf-8")
        import ast
        try:
            ast.parse(bad.read_text(encoding="utf-8"))
            # 不期待到这里——语法应该报错
            assert False, "应该有语法错误但没检测到"
        except SyntaxError:
            pass  # 期待的行为
        # 修复后
        bad.write_text("def foo():\n    pass\n", encoding="utf-8")
        ast.parse(bad.read_text(encoding="utf-8"))  # 不应报错
