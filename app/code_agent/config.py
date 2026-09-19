import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")

APP_DIR = PROJECT_ROOT / "app"
CODE_AGENT_DIR = APP_DIR / "code_agent"
DATA_DIR = PROJECT_ROOT / "data"
RUNTIME_DIR = PROJECT_ROOT / "runtime"

KNOWLEDGE_DIR = Path(os.getenv("CODE_AGENT_KNOWLEDGE_DIR", DATA_DIR / "knowledge"))
WORKSPACE_DIR = Path(os.getenv("CODE_AGENT_WORKSPACE_DIR", RUNTIME_DIR / "workspace"))
CHECKPOINT_DIR = Path(os.getenv("CODE_AGENT_CHECKPOINT_DIR", RUNTIME_DIR / "checkpoint"))
# 阶段 1 起：跨轮记忆改用 SQLite（langgraph 的 AsyncSqliteSaver，按 thread_id 恢复）。
# 旧的 CHECKPOINT_DIR（JSON 目录）已废弃 —— 保留常量只为兼容与清理。
CHECKPOINT_DB = Path(os.getenv("CODE_AGENT_CHECKPOINT_DB", RUNTIME_DIR / "checkpoints.db"))
CHROMA_DIR = Path(os.getenv("CODE_AGENT_CHROMA_DIR", RUNTIME_DIR / "chroma_db"))
RUNS_DIR = Path(os.getenv("CODE_AGENT_RUNS_DIR", RUNTIME_DIR / "runs"))

# 运行时目录由配置层统一创建。
# 为什么放在这里：这些目录不属于版本控制（runtime/ 被 gitignore），全新 clone 下来并不存在；
# 而 tests/test_config.py 断言 CHECKPOINT_DIR / CHROMA_DIR 存在、运行期也需要它们。
# （实测：不创建时，全新 clone 下 `test_runtime_dirs_exist` 会失败。）
for _d in (RUNTIME_DIR, WORKSPACE_DIR, CHECKPOINT_DIR, CHROMA_DIR, RUNS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

PYTHON_EXECUTABLE = os.getenv("CODE_AGENT_PYTHON", sys.executable)

RAG_SERVER_PATH = CODE_AGENT_DIR / "rag" / "rag.py"
BROWSER_SERVER_PATH = CODE_AGENT_DIR / "mcp_servers" / "browser_tools.py"
POWERSHELL_SERVER_PATH = CODE_AGENT_DIR / "mcp_servers" / "powershell_tools.py"
MYSQL_SERVER_PATH = CODE_AGENT_DIR / "mcp_servers" / "mysql_tools.py"
VM_SERVER_PATH = CODE_AGENT_DIR / "mcp_servers" / "vm.py"
CODE_TOOLS_SERVER_PATH = CODE_AGENT_DIR / "mcp_servers" / "code_tools.py"

EMBEDDING_MODEL_PATH = Path(
    os.getenv(
        "CODE_AGENT_EMBEDDING_MODEL_PATH",
        PROJECT_ROOT.parent / "embedding-model" / "sentence-transformers" / "all-MiniLM-L6-v2",
    )
)
EMBEDDING_MODEL_CACHE_DIR = Path(
    os.getenv("CODE_AGENT_EMBEDDING_MODEL_CACHE_DIR", PROJECT_ROOT.parent / "embedding-model")
)

SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8888")

MODEL_NAME = os.getenv("MODEL_NAME", "deepseek-v4-flash")
MODEL_BASE_URL = os.getenv("MODEL_BASE_URL", "https://api.deepseek.com")
MODEL_API_KEY = os.getenv("MODEL_API_KEY")

# 单次 LLM 调用的超时（秒）。管的是"模型**太慢**/挂起"，
# 与降级链（管"模型**报错**"）是两件事，两个都要有。
LLM_TIMEOUT = int(os.getenv("CODE_AGENT_LLM_TIMEOUT", "60"))

# 流式请求时请求 API 返回 usage 统计（DeepSeek 支持 stream_options.include_usage）。
# 若换成不支持的 API 可设 LLM_STREAM_USAGE=0 关闭，此时 token 统计会退化为 0。
LLM_STREAM_USAGE = os.getenv("LLM_STREAM_USAGE", "1").lower() in ("1", "true", "yes", "on")

MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3307"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "root")
MYSQL_CHARSET = os.getenv("MYSQL_CHARSET", "utf8mb4")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "agent_test")

# 只读账号：给「只读工具」用的**数据库层兜底**。
# 主防线是应用层的语句白名单（见 mysql_tools.py 的 _is_readonly_sql）；
# 这里再叠一层，保证即使应用层被绕过也改不了数据。
# 未配置时只读工具回落到上面的普通账号（行为与改造前一致）。
MYSQL_READONLY_USER = os.getenv("MYSQL_READONLY_USER")
MYSQL_READONLY_PASSWORD = os.getenv("MYSQL_READONLY_PASSWORD")

WSL_DISTRO = os.getenv("CODE_AGENT_WSL_DISTRO", "Ubuntu")
VM_UPLOADS_DIR = os.getenv("CODE_AGENT_VM_UPLOADS_DIR", "/home/leprite/nginx/uploads")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
THREAD_ID = os.getenv("CODE_AGENT_THREAD_ID", "default")

# 日志格式开关：LOG_JSON=1 输出单行 JSON（便于检索/聚合）；默认仍是人类可读文本
# （开发时看 JSON 很痛苦，所以默认关闭）。
LOG_JSON = os.getenv("LOG_JSON", "0").lower() in ("1", "true", "yes", "on")


class JsonFormatter(logging.Formatter):
    """把日志输出成单行 JSON，便于检索和聚合。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(name: str = "code_agent") -> logging.Logger:
    """配置并返回 logger 实例。

    ⚠️ handler 必须写 **stderr**：MCP server 的 stdout 是 JSON-RPC 通道，
    往 stdout 写任何日志都会污染协议。
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        if LOG_JSON:
            handler.setFormatter(JsonFormatter())
        else:
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"
                )
            )
        logger.addHandler(handler)
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    return logger
