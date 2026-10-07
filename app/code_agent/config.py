import json
import logging
import os
import sys
from dataclasses import dataclass
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
# 跨轮记忆用 **SQLite 单文件**（langgraph 的 AsyncSqliteSaver，按 thread_id 恢复）。
# ⚠️ 这里曾经还有一个 `CHECKPOINT_DIR = runtime/checkpoint/`（阶段 1 之前是"一个会话一个
#    JSON 文件"的目录方案）。阶段 1 改成 SQLite 后它就成了空目录，却**每次启动都被 mkdir 回来**，
#    还和 `runtime/checkpoints.db` 只差一个 s —— 2026-09-22 用户就被这个同名设计绕了一次。
#    已按用户决定**彻底移除**（连同 test_config.py 里那条断言）。
CHECKPOINT_DB = Path(os.getenv("CODE_AGENT_CHECKPOINT_DB", RUNTIME_DIR / "checkpoints.db"))
CHROMA_DIR = Path(os.getenv("CODE_AGENT_CHROMA_DIR", RUNTIME_DIR / "chroma_db"))
RUNS_DIR = Path(os.getenv("CODE_AGENT_RUNS_DIR", RUNTIME_DIR / "runs"))
# 阶段 4（T4.1）：超长工具结果外置到这里，context 里只留预览 + 路径
TOOL_RESULTS_DIR = Path(os.getenv("CODE_AGENT_TOOL_RESULTS_DIR", RUNTIME_DIR / "tool_results"))

# 运行时目录由配置层统一创建。
# 为什么放在这里：这些目录不属于版本控制（runtime/ 被 gitignore），全新 clone 下来并不存在；
# 而 tests/test_config.py 断言它们存在、运行期也需要它们。
# （实测：不创建时，全新 clone 下 `test_runtime_dirs_exist` 会失败。）
for _d in (RUNTIME_DIR, WORKSPACE_DIR, CHROMA_DIR, RUNS_DIR, TOOL_RESULTS_DIR):
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
VM_UPLOADS_DIR = os.getenv("CODE_AGENT_VM_UPLOADS_DIR", "/home/user/nginx/uploads")

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

# ═══════════════════════════════════════════════════════════════════
# 阶段 8 · P2（候选池 §十三①）：三项阈值**按模型窗口自动算**
# ═══════════════════════════════════════════════════════════════════
#
# **为什么**：这三个阈值以前是**全局写死**的，跟"当前用哪个模型"无关 ——
#   · 换个 32k 窗口的模型，`NODE_TOKEN_BUDGET` 若按老的写死值算可能直接撞窗口（一次调用就超）；
#   · 换个 1M 窗口的模型，压实阈值 6000 又过于保守（模型明明记得住，却一直在摘要）。
#
# **解析顺序**（高优先级在前）：
#   ① 显式环境变量（老名字不变）：`CODE_AGENT_COMPACT_THRESHOLD` / `CODE_AGENT_NODE_TOKEN_BUDGET`
#      / `CODE_AGENT_TASK_TOKEN_BUDGET` —— 设了就**一律**用它（这是"我说了算"的出口）；
#   ② 按**当前 executor 实际用的模型**的窗口算比例；
#   ③ 窗口本身也是三级：`CODE_AGENT_CONTEXT_WINDOW`（全局覆盖）> 注册表里该模型的
#      `context_window`（`config/models.json` 或 Web 面板里加的自定义模型）> 128000（默认）。
#
# **比例**（token 是**估算值** ⇒ 一律留足余量）：
#   压实 **25%** · 单次调用输入 **50%** · 任务累计 **max(50 万, 4 倍窗口)** 并封顶 200 万。
#   ⚠️ **顺序约束**：`compact` 必须**明显小于** `node` —— 两者都会动历史，但 compact 是
#      "用摘要换掉老历史"（信息还在），node 超限是"**直接砍掉**最老的消息"（信息没了）。
#      25% : 50% = 1:2，给摘要留出提前量（15%/35% 那版偏早，2026-10-07 调）。
#   ⚠️ **上限备忘**：`COMPACT_MAX` / `NODE_MAX` 是"理智闸门"。真用上 >500k 窗口的模型时，
#      要**连上限一起抬**（否则"按窗口算"会被上限卡回原地：1M 窗口也会退回 64k / 200k）。
# ⚠️ **任务级刻意不是窗口的小比例**：它是"**成本保险丝**"，不是"能不能塞进一次调用"的问题 ——
#    一个任务本来就可能跑好几轮满上下文。候选池 §十三① 当时写的是"TASK ≈ 60~70%（窗口）"，
#    但那是**比今天还紧**的（128k × 65% ≈ 8.3 万 < 现在的 20 万），与"预算放宽到 50 万~100 万"
#    的结论相反 ⇒ 2026-10-07 定：**任务级按"窗口倍数 + 下限"算**（见 `TASK_MIN`）。
DEFAULT_CONTEXT_WINDOW = int(os.getenv("CODE_AGENT_CONTEXT_WINDOW", "128000"))
# 压实后**原样保留**的最近消息条数（越近的信息越要逐字保留）
COMPACT_KEEP_MESSAGES = int(os.getenv("CODE_AGENT_COMPACT_KEEP", "8"))
COMPACT_RATIO = 0.25
NODE_RATIO = 0.50
TASK_WINDOW_MULTIPLE = 4
TASK_MIN = 500_000
TASK_MAX = 2_000_000
COMPACT_MIN, COMPACT_MAX = 6_000, 64_000
NODE_MIN, NODE_MAX = 4_000, 200_000


@dataclass(frozen=True)
class TokenBudgets:
    """三项阈值的**一次解析结果**（附带窗口与来源，便于测试与排查）。"""

    compact: int  # 对话压实阈值
    node: int  # 单次调用输入上限（进模型前剪枝）
    task: int  # 单任务累计 token 上限（成本保险丝）
    context_window: int
    source: str  # "env"（至少一项来自显式环境变量）/ "window"（按窗口算的）

    def as_dict(self) -> dict:
        return {
            "compact": self.compact,
            "node": self.node,
            "task": self.task,
            "contextWindow": self.context_window,
            "source": self.source,
        }


def _env_int(name: str) -> int | None:
    """读一个可选的环境变量（空串/读不出数字都算"没设"）。"""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return None
    try:
        return int(float(str(raw).strip()))
    except ValueError:
        return None


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def budgets_for_window(
    window: int,
    *,
    compact_env: int | None = None,
    node_env: int | None = None,
    task_env: int | None = None,
) -> TokenBudgets:
    """纯函数：给一个窗口算出三项阈值（显式 env 优先）。**不读环境、不碰注册表**，好测。"""
    window = int(window) if window and window > 0 else DEFAULT_CONTEXT_WINDOW
    compact = _clamp(int(window * COMPACT_RATIO), COMPACT_MIN, COMPACT_MAX)
    node = _clamp(int(window * NODE_RATIO), NODE_MIN, NODE_MAX)
    task = _clamp(max(TASK_MIN, window * TASK_WINDOW_MULTIPLE), TASK_MIN, TASK_MAX)
    overridden = False
    if compact_env is not None:
        compact, overridden = compact_env, True
    if node_env is not None:
        node, overridden = node_env, True
    if task_env is not None:
        task, overridden = task_env, True
    return TokenBudgets(
        compact=compact,
        node=node,
        task=task,
        context_window=window,
        source="env" if overridden else "window",
    )


def _registry_context_window(role: str) -> int | None:
    """从模型注册表里取该角色当前模型的窗口声明（取不到就 None，绝不抛）。"""
    try:
        # 局部 import：`model/llm.py` 依赖本模块，模块级 import 会成环
        from app.code_agent.model.llm import registry

        return registry.context_window(role)
    except Exception:  # noqa: BLE001 —— 取窗口失败不能影响任何主流程
        return None


def token_budgets(role: str = "executor") -> TokenBudgets:
    """**运行期**解析三项阈值（换模型后不用重启也生效）。"""
    env_window = _env_int("CODE_AGENT_CONTEXT_WINDOW")
    window = env_window or _registry_context_window(role) or DEFAULT_CONTEXT_WINDOW
    return budgets_for_window(
        window,
        compact_env=_env_int("CODE_AGENT_COMPACT_THRESHOLD"),
        node_env=_env_int("CODE_AGENT_NODE_TOKEN_BUDGET"),
        task_env=_env_int("CODE_AGENT_TASK_TOKEN_BUDGET"),
    )


# ⚠️ 下面三个常量是**默认窗口下的静态快照**（给 import 期就要用值的读者 + 兼容老代码）。
#    运行期请用 `token_budgets()` —— 否则换了模型它不会跟着变。
_DEFAULT_BUDGETS = budgets_for_window(DEFAULT_CONTEXT_WINDOW)
COMPACT_THRESHOLD_TOKENS = _DEFAULT_BUDGETS.compact  # 默认窗口 25%（128k → 32000）
NODE_TOKEN_BUDGET = _DEFAULT_BUDGETS.node  # 默认窗口 50%（128k → 64000）
TASK_TOKEN_BUDGET = _DEFAULT_BUDGETS.task  # max(50 万, 4×窗口)（128k → 512000）

# ── T8.1 任务级墙钟（阶段 8 · P0）──
# 单个任务最长**执行**时间（秒）；到点自动停 —— 与用户点「停止」走**同一条**路径
# （`agent/cancel.py` 的 CancelToken：协作式检查点）。
# ⚠️ **人工确认（权限弹框）期间不计时**：人看弹框的时间不是任务时间
#    （探索测试第 2 题的现场样本：24 分钟一次工具调用都没有）。
# ⚠️ 取 900 秒（15 分钟）的依据：正常任务实测 1~5 分钟；第 2 题那种失控烧了 30 分钟 / 20.8 万 token。
# 0 或负数 = 不限制（仅用户点停止能停）。
TASK_WALL_CLOCK = float(os.getenv("CODE_AGENT_TASK_WALL_CLOCK", "900"))

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

# ═══════════════════════════════════════════════════════════════════
# 阶段 5 · 三档权限模式（T5.1）+ 人工确认超时（T5.3）+ 审计（T5.4）
# ═══════════════════════════════════════════════════════════════════

# 权限模式的**兜底默认值**（D4「③ 混合」结论）：
#   · CLI 默认「需确认」；Web 端把「只读 / 需确认」持久化到 runtime/web-settings.json；
#   · **「放开」永不持久化** —— 新会话一律回落「需确认」（配置文件被手改成 "open" 也当「需确认」加载，
#     理由见 security/permissions.py 的 normalize_mode 与 server.py 的 load_permission_mode）；
#   · 评估等无人值守入口必须**显式**指定档位（不显式指定时，「需确认」没人可问 → 全部自动拒绝）。
# ⚠️ 合法取值 readonly / confirm / open，档位表在 app/code_agent/security/permissions.py。
PERMISSION_MODE = os.getenv("CODE_AGENT_PERMISSION_MODE", "confirm").strip().lower()

# 人工确认的超时（秒）：**超时 / 无人应答 → 自动拒绝**（B2，安全侧）。
# 只作用于 Web（终端里的 CLI 由人当场回答，见 T5.3 的说明）。
CONFIRM_TIMEOUT = float(os.getenv("CODE_AGENT_CONFIRM_TIMEOUT", "120"))

# 审计留痕：高危操作 + 所有确认决定，一行一条 JSON，追加式。
# 落在 runtime/ 下 → 已被 gitignore（不进版本控制）。
PERMISSIONS_LOG = Path(os.getenv("CODE_AGENT_PERMISSIONS_LOG", RUNTIME_DIR / "permissions.log"))

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
