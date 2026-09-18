"""测试 config.py 配置加载。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent import config


class TestConfigDefaults:
    """默认值测试（不依赖 .env 文件）。"""

    def test_mysql_defaults(self):
        """MySQL 默认连接参数。"""
        assert config.MYSQL_HOST == "127.0.0.1"
        assert config.MYSQL_PORT == 3307
        assert config.MYSQL_USER == "root"
        assert config.MYSQL_CHARSET == "utf8mb4"
        assert config.MYSQL_DATABASE == "agent_test"

    def test_llm_defaults(self):
        """LLM 默认模型配置应非空。"""
        assert len(config.MODEL_NAME) > 0
        assert "api" in config.MODEL_BASE_URL

    def test_server_paths_exist(self):
        """MCP Server 路径配置应指向存在的文件或合理的默认路径。"""
        assert config.POWERSHELL_SERVER_PATH.name == "powershell_tools.py"
        assert config.MYSQL_SERVER_PATH.name == "mysql_tools.py"
        assert config.CODE_TOOLS_SERVER_PATH.name == "code_tools.py"

    def test_workspace_dir_exists(self):
        """workspace 目录应自动创建。"""
        assert config.WORKSPACE_DIR.exists()
        assert config.WORKSPACE_DIR.is_dir()

    def test_runtime_dirs_exist(self):
        """运行时目录应自动创建。"""
        assert config.CHECKPOINT_DIR.exists()
        assert config.CHROMA_DIR.exists()

    def test_thread_id_default(self):
        """默认 thread_id。"""
        assert isinstance(config.THREAD_ID, str)
        assert len(config.THREAD_ID) > 0
