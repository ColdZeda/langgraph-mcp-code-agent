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
# 阶段 4（T4.1）：超长工具结果外置到这里，context 里只留预览 + 路径
TOOL_RESULTS_DIR = Path(os.getenv("CODE_AGENT_TOOL_RESULTS_DIR", RUNTIME_DIR / "tool_results"))

# 运行时目录由配置层统一创建。
# 为什么放在这里：这些目录不属于版本控制（runtime/ 被 gitignore），全新 clone 下来并不存在；
# 而 tests/test_config.py 断言 CHECKPOINT_DIR / CHROMA_DIR 存在、运行期也需要它们。
# （实测：不创建时，全新 clone 下 `test_runtime_dirs_exist` 会失败。）
for _d in (RUNTIME_DIR, WORKSPACE_DIR, CHECKPOINT_DIR, CHROMA_DIR, RUNS_DIR, TOOL_RESULTS_DIR):
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


# ═══════════════════════════════════════════════════════════════════
# 阶段 4 · 上下文工程与分层记忆
# ═══════════════════════════════════════════════════════════════════

# ── T4.1 工具结果外置 ──
# 阈值按**实测**定：第六版建议 2000 字符会把「71 行的文件读取」(2,824 字符) 也外置，
# 等于几乎每次读文件都多一次「落盘 + 回路径」的往返，收益不划算 →
# 实测 5,845 字符（154 行的 powershell_tools.py）仍值得保留原文，故取 6000。
EXTERNALIZE_THRESHOLD = int(os.getenv("CODE_AGENT_EXTERNALIZE_THRESHOLD", "6000"))
# 外置后 context 里保留多少字符的预览
EXTERNALIZE_PREVIEW_CHARS = int(os.getenv("CODE_AGENT_EXTERNALIZE_PREVIEW_CHARS", "500"))
# 行数兜底：有些输出单行很短、总字符数不超标，但行数极多（长清单），同样吃 token。
EXTERNALIZE_MAX_LINES = int(os.getenv("CODE_AGENT_EXTERNALIZE_MAX_LINES", "150"))
# ⚠️ **豁免名单：这两个工具的输出"就是要给模型看的内容"**，不能外置。
#    实测（同一道题"读取 multi_agent.py 全文并总结"，见第八版订正 #15）：
#      外置 read_file_range → 模型只看得到 500 字符预览 → 它改用**分段读**绕过去：
#      12 次 read_file_range / 25 步，token 从 17,361 涨到 **127,071（7.3 倍）**。
#    更糟的是"回读外置文件"会得到同一份内容 → 同一个 hash → 又指向同一个文件，等于死循环。
#    → 所以只外置**过程性/附带性**的长输出（命令输出 / 目录清单 / 搜索结果 / 查询结果…），
#      豁免"取内容"类工具。
EXTERNALIZE_EXEMPT_TOOLS = {
    t.strip()
    for t in os.getenv("CODE_AGENT_EXTERNALIZE_EXEMPT", "read_file_range,read_file").split(",")
    if t.strip()
}

# ── T4.2 对话压实 ──
# 历史消息估算 token 超过它 → 把最老的若干轮压成四段式摘要
COMPACT_THRESHOLD_TOKENS = int(os.getenv("CODE_AGENT_COMPACT_THRESHOLD", "6000"))
# 压实后**原样保留**的最近消息条数（越近的信息越要逐字保留）
COMPACT_KEEP_MESSAGES = int(os.getenv("CODE_AGENT_COMPACT_KEEP", "8"))

# ── T4.3 token 预算 ──
# 节点级：进入 Executor 前估算 prompt token，超了就先剪枝（砍最老、最长的工具结果）
NODE_TOKEN_BUDGET = int(os.getenv("CODE_AGENT_NODE_TOKEN_BUDGET", "30000"))
# 任务级硬上限：单任务累计 token 超了 → **主动终止并报告**（而不是烧到失控）
TASK_TOKEN_BUDGET = int(os.getenv("CODE_AGENT_TASK_TOKEN_BUDGET", "200000"))

# ── T4.5 Redis 工具结果缓存 ──
REDIS_URL = os.getenv("CODE_AGENT_REDIS_URL", "redis://127.0.0.1:6379/0")
TOOL_CACHE_TTL = int(os.getenv("CODE_AGENT_TOOL_CACHE_TTL", "600"))  # 秒
# 评估/调试时可关掉（缓存会改变"工具真的执行了几次"的语义）
TOOL_CACHE_ENABLED = os.getenv("CODE_AGENT_TOOL_CACHE", "1").lower() in ("1", "true", "yes", "on")

# ── T4.4 分层记忆（RAG 分块 + rerank） ──
# 换 collection 名：旧的「整篇一个向量」和新「每块一个向量」混在一个 collection 里
# 会让检索质量**更差**（两种粒度互相干扰），所以必须分家。
RAG_COLLECTION = os.getenv("CODE_AGENT_RAG_COLLECTION", "terminal_knowledge_v2")
# 单块最大字符数；超出就按重叠切分（知识库里一条条短条目则天然各成一块）
RAG_CHUNK_MAX_CHARS = int(os.getenv("CODE_AGENT_RAG_CHUNK_MAX_CHARS", "500"))
# 相邻块的重叠比例（仅在"单块超长被切开"时生效）
RAG_CHUNK_OVERLAP_RATIO = float(os.getenv("CODE_AGENT_RAG_CHUNK_OVERLAP", "0.15"))
# 粗召回条数 → 精排后取 top-K
RAG_RECALL_K = int(os.getenv("CODE_AGENT_RAG_RECALL_K", "10"))
RAG_TOP_K = int(os.getenv("CODE_AGENT_RAG_TOP_K", "3"))
# CrossEncoder reranker 的**本地**路径（离线优先；不存在则降级为纯向量检索，不联网下载）
RERANKER_PATH = Path(
    os.getenv(
        "CODE_AGENT_RERANKER_PATH",
        PROJECT_ROOT.parent / "embedding-model" / "cross-encoder" / "ms-marco-MiniLM-L-6-v2",
    )
)
RERANK_ENABLED = os.getenv("CODE_AGENT_RERANK", "1").lower() in ("1", "true", "yes", "on")

# ── T4.4 自动注入 / 自动沉淀 ──
# 自动注入：任务开始时自动检索一次相关知识（不必等模型主动调 query_rag）
RAG_AUTO_INJECT = os.getenv("CODE_AGENT_RAG_AUTO_INJECT", "1").lower() in ("1", "true", "yes", "on")
# 自动沉淀：任务成功后由 LLM 判断"有没有值得长期记住的经验"
# ⚠️ 评估时必须关（否则知识库会被评测过程的临时经验污染，且改写了后续题目的检索结果）
RAG_AUTO_DEPOSIT = os.getenv("CODE_AGENT_RAG_AUTO_DEPOSIT", "1").lower() in (
    "1",
    "true",
    "yes",
    "on",
)

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
