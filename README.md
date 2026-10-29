# Code Agent

> **项目仍在开发 / 测试中：请勿用于真实生产环境，也不要直接跑在你不想被改动的宿主机上。**
> 它**真的会执行 shell 命令、读写本机文件**（这是它的能力，不是漏洞 —— 定位就是"本机单用户的助手"）。
> 建议先在**可丢弃的环境**（虚拟机 / WSL / 有快照的机器）里试；默认权限档是「需确认」，
> 在不熟悉的目录下**不要**切到「放开」。

[![CI](https://github.com/ColdZeda/langgraph-mcp-code-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/ColdZeda/langgraph-mcp-code-agent/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.13-blue)
![Tests](https://img.shields.io/badge/tests-743%20%2B%205%20integration-brightgreen)
![Eval](https://img.shields.io/badge/eval-30%2F30%20%C2%B7%20163%20assertions-success)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

## 概览

**它是什么**：本机单用户的 AI 编程 / 操作电脑助手。LangGraph 多 Agent（**Planner → Executor → Verifier**）
+ MCP 工具层（32 个工具：PowerShell / MySQL / WSL2 / 搜索 / AST 与 diff / 本地知识库）+ Web UI 与 CLI 双入口。

| 项 | 值 |
|---|---|
| **评估**（30 题 / **163 条强断言**） | **single 30/30、multi 30/30**，平均分 **1.0000**；状态 124 / 轨迹 29 / 文本 10 全过 |
| **成本画像**（multi ÷ single） | token **中位 1.58×**（总量 1.71×）、时间 2.07×；**Verifier 占总 token 25%** |
| **自动化测试** | **743** 条单元/工具级（约 30 秒跑完）+ **5** 条真集成测试（默认不跑，见「如何验证」） |
| **覆盖率 / 静态检查** | 78%~79%（`--cov`，随依赖容器状态波动）/ `ruff check` + `ruff format --check` |
| **RAG 检索对照**（2×2 消融 + 全量召回） | 正解文件 top-1 **0.40 → 0.60**、干扰项 0.60 → 0.40、延迟 12.8ms → 86.7ms（明细见「上下文工程」） |
| **可控性** | 任务可随时停止（协作式取消：节点入口 / ReAct 每步 / 每次工具调用前，共 3 个检查点）+ **15 分钟墙钟**（人工确认期间不计时）；「预算或墙钟终止」与「验收未通过」严格区分，停止后已完成产物一律保留 |

![启动后的界面：侧栏 + 任务状态条](docs/evidence/界面截图-2026-10-09/主界面.png)
*主界面：左侧切执行模式 / 权限模式 / 历史会话，右侧输入任务*

![真实任务：Planner 计划 + Executor 结果 + 验收通过](docs/evidence/界面截图-2026-10-09/Planner计划与Executor结果.png)
*multi 模式：Planner 计划 → Executor 执行 → Verifier 验收；卡片汇总模型 / 打回次数 / 步数 / token / 耗时*

![权限确认弹框：参数、倒计时、停止任务](docs/evidence/界面截图-2026-10-09/权限确认弹框与停止按钮.png)
*「需确认」档的写操作弹框：参数、倒计时、「本会话总是允许」、停止按钮*

更多界面（知识库面板 / 四个角色的模型 / token 明细）见 [`docs/evidence/界面截图-2026-10-09/`](docs/evidence/界面截图-2026-10-09/)。

## 能力总览

**① RAG 全链路**：`解析 → 分块 → Embedding → 向量检索 → CrossEncoder 精排 → 上下文组装`。
知识库是**本地** ChromaDB（embedding / reranker 都是本地模型、不联网；精排缺失自动降级为纯向量，向量模型缺失时 RAG 4 个工具明确报错并给装法）。做了 **2×2 消融 + 全量召回对照组**（不用 LLM、不烧 token）：正解文件 top-1 **0.40 → 0.60**、干扰项 0.60 → 0.40，代价是延迟 12.8ms → **86.7ms**（明细见「上下文工程与分层记忆」）。

**② 评测体系**：30 道题 / **163 条断言**，四档强度（文本 10 / 轨迹 29 / **状态 124** / LLM 评分 0）；三条口径与题集设计见「评估体系」。判定器按真实 MCP 工具名与参数名写。报告：[`docs/evidence/评估报告.md`](docs/evidence/评估报告.md)。

**③ 上下文工程**：长工具结果**外置**到磁盘、上下文只留预览 + 路径（`read_file*` 在豁免名单里，实测不豁免会让 token 涨 7.3 倍）；历史超阈值**压实**成四段式摘要（摘要失败就保留原文）；**token 预算**分节点级与任务级（阈值按当前模型的上下文窗口自动算），任务级在 ReAct 循环**内部逐步判**（只在节点入口判会漏掉单节点烧十几万 token）。

**④ Agent 编排**：LangGraph `StateGraph` 三阶段 + `route_node` 复杂度路由（`auto` 模式），
Verifier 打回上限 `MAX_RETRY=2`；执行过程通过 WebSocket 推**节点级进度**（不引 SSE）。
工具层全部走 MCP stdio 子进程，**统一经一层包装**（权限判定 → 结果外置 → 只读缓存，三条入口 CLI/Web/评估都接）。

**⑤ 权限与 HITL**：32 个工具的三档档位表（只读 14 / 写执行 18 / 高危 6），默认「需确认」；
判定在**缓存查询之前**（否则"曾经允许过"的缓存会让已被拒绝的调用照样返回）；Web 弹框 + CLI 询问 +
超时自动拒绝 + 审计留痕；WSL2 只当**隔离执行环境**，不叫"安全沙箱"。见「安全设计」。

## 架构

```
main.py (CLI REPL)                app/web/server.py (FastAPI + Vue3 Web UI, 端口 8000)
       └────────────┬──────────────────────┘
                    ▼
   执行模式（`--mode`，默认 auto）：
     single → 只跑 Executor（快，适合查询类）
     multi  → 恒定走下面完整三阶段
     auto   → 先由 route_node 判复杂度，simple 走 single、complex 走 multi
                    ▼
   多 Agent 协作图（app/code_agent/agent/multi_agent.py, LangGraph StateGraph）
   Planner（纯 LLM 规划，输出结构化计划 JSON）
        ↓
   Executor（create_react_agent，全量工具，按计划执行）
        ↓
   Verifier（只读白名单 12 个工具，对照「需求 + 计划 + 执行轨迹」验收）
        ↓ FAIL → 带原因打回 Executor（上限 `MAX_RETRY = 2` 次；再不过就以最后一次结果收尾）

   ├── LLM：DeepSeek（OpenAI 兼容接口；Web UI 内可热切换模型 / 地址 / Key）
   │        模型**按角色配**（Planner / Executor / Verifier / Router），注册表 `config/models.json`
   ├── Memory：SqliteSaver（`runtime/checkpoints.db`，按 thread_id 恢复跨轮对话，
   │            进程重启后仍记得；Web UI 的"历史会话"就是读它）
   ├── 上下文工程：长工具结果外置 / 历史压实 / token 预算（见「上下文工程与分层记忆」）
   └── Tools：6 个自建 MCP Server（stdio 子进程）+ FileManagementToolkit
       ├── powershell_tools.py   Windows 命令执行（危险命令黑名单）
       ├── browser_tools.py      搜索（SearXNG JSON API；文件名是历史遗留）
       ├── mysql_tools.py        MySQL 增删改查（参数化 + 标识符转义）
       ├── vm.py                 WSL2 桥接（危险命令拦截 + 超时）
       ├── code_tools.py         AST 解析 / diff / 项目结构扫描 / 文件片段读取
       └── rag/rag.py            ChromaDB 知识库（分块索引 + CrossEncoder 精排 + 本地 embedding）
```

## 快速开始

### 环境要求

Python **3.13+** · [uv](https://docs.astral.sh/uv/) · **Windows**（PowerShell / WSL2 工具依赖宿主）· 可选：WSL2 Ubuntu（虚拟机工具）、Docker（MySQL / SearXNG / Redis）。

### 安装

```bash
git clone https://github.com/ColdZeda/langgraph-mcp-code-agent.git
# 国内镜像（内容相同）： https://gitee.com/ColdZeda/langgraph-mcp-code-agent.git
cd langgraph-mcp-code-agent
uv sync
```

### 安装 RAG 的本地模型（知识库功能需要）

```bash
uv run python scripts/fetch_models.py --dry-run   # ① 先看装没装（不下载）
uv run python scripts/fetch_models.py             # ② 装（≈90MB × 2，走 hf-mirror；连不上就加 --source modelscope）
```

模型**不在仓库里**（各≈87MB），默认下到 `<仓库的上级目录>/embedding-model/`（跟着仓库走，不写死路径）。**没有也能用**：只有知识库那 4 个工具不可用（缺向量模型）或自动降级（缺精排），其余 30+ 个工具照常。

### 配置模型（在 Web 界面里配，不用改文件）

启动 Web UI 后打开「⚙ 模型设置」，填 `显示名 + 模型名 + API 地址 + Key`：可存多个模型、每个模型自带 Key、改完立即生效（存 `runtime/web-settings.json`，不进仓库）。

**只有 CLI 与 `evals/` 需要 `.env`**（它们不读界面设置）：把 `.env.example` 复制成 `.env` 并填 `MODEL_API_KEY`，其余项有默认值。

模型**按角色配**（Planner / Executor / Verifier / Router），下拉框第一项「使用配置默认」＝回落 `config/models.json` / `.env`（CLI 与 evals 走这条）：

- **预置可选模型**：填 `config/models.json`（进版本控制，默认空，格式见文件内 `_readme`）；键是**显示名**、`model` 字段是**实际发给 API 的名字**。
- **用户自己加**：面板「我的模型」→ 显示名 + 模型名 + 地址 + Key，存本机 `runtime/web-settings.json`；**每个模型自带凭据**，与系统默认互不影响。
- **Verifier 换异构模型**要勾「验收使用异构模型」—— 默认三角色同模型（自己验自己容易有一致盲区）。
- 侧栏常驻显示「当前生效模型」；结果卡片显示**本轮实际使用**的模型（取服务端回报的 `model_name`）。

> **三处配置的优先级**：`runtime/web-settings.json`（界面）> `config/models.json`（仓库默认）> `.env`（密钥与兜底）。
> - `web-settings.json` 含**明文密钥**，已 gitignore，不要分享；界面只回显尾号 4 位。
> - **界面上存过一次模型选择后，再改 `models.json` 的 `roles` 不生效** —— 要回界面改。
> - 改 `.env` 要**重启**才生效；界面改的从**下一条消息**起生效。
> - **CLI 与 evals 不读 `web-settings.json`**。

### 启动依赖服务

```powershell
.\scripts\run\start-deps.ps1     # 一键起全部 4 个：mysql / searxng / redis / nginx
.\scripts\run\stop-deps.ps1      # 停止（保留容器，下次起得更快）
```

或手动分两步：

```powershell
docker compose up -d                                            # mysql / searxng / redis
wsl -d Ubuntu -- bash -lc "cd ~/nginx && docker compose up -d"  # nginx
```

| 服务 | 容器 | 端口 | 说明 |
|---|---|---|---|
| MySQL 沙盒 | `agent-mysql` | 3307→3306 | 数据存在**命名卷** `mysql-data`；首次初始化会执行 `scripts/mysql-init/*.sql` |
| 搜索 | `searxng` | 8888→8080 | 配置/缓存在**本机** `searXNG\{config,data}`（不进版本控制） |
| 缓存 | `redis-stack-server` | 6379 | 纯缓存，**故意不挂卷**（数据可丢） |
| 静态发布 | `my-nginx` | 80 | 挂载源在 WSL（见下），由 WSL 里那份 compose 管理 |

**为什么 Agent 主体不容器化**：6 个 MCP 工具里 PowerShell（`powershell.exe`）与 WSL2（`wsl.exe`）依赖 Windows 宿主，Linux 容器里跑不了。

**为什么 nginx 单独管理**：挂载源是 WSL 里的 `/home/user/nginx/*`，从 Windows 侧 compose 跑会把 Linux 路径解析到 docker-desktop 发行版，**静默挂载空目录**。所以两边分开管，`start-deps.ps1` 一起拉起。

> 4 个容器都带 `restart: unless-stopped` → 打开 Docker Desktop 会自动起；**手动 stop 过的不算**（这是 `unless-stopped` 的定义）。

### 运行（命令行）

```bash
uv run python main.py                        # 默认启动（执行模式 auto / 权限模式 需确认）
uv run python main.py --thread-id my-session # 指定会话 ID
uv run python main.py --mode single          # 执行模式：auto（默认）/ single / multi
uv run python main.py --permission readonly  # 权限模式：readonly / confirm（默认）/ open
uv run python main.py --debug                # 调试模式（详细日志）
```

> 「**执行模式**」和「**权限模式**」是两条独立的轴，可任意组合（如 `multi + readonly`）：前者决定"谁来干"，后者决定"允不允许动手"（见[安全设计](#安全设计)）。退出：`exit` / `quit` / `q` / `退出` / `bye`。
>
> **跨轮记忆**在 `runtime/checkpoints.db`：不传参时用 `.env` 的 `CODE_AGENT_THREAD_ID`（默认 `default`）⟹ 关掉再打开会继续上次对话；换新会话用 `--new-session`。

### 运行（Web UI）

```bash
uv run uvicorn app.web.server:app --port 8000
# 浏览器打开 http://localhost:8000
```

一键脚本（**前台**运行，日志就在这个窗口；Windows 上也可以直接双击 `.cmd`）：

```powershell
.\scripts\run\start-app.ps1        # 查 uv / 查容器 / 查 dist / 查端口 → 再起服务
.\scripts\run\start-app.cmd        # 同上，双击入口
.\scripts\run\start-app.ps1 -Dev   # 另开一个窗口跑 npm run dev（前端热更新，5173）
```

> **端口不是死的**：脚本会**实测**首选端口（默认 8000）能不能绑定，不行就自动退避
> （8000 → 8010 → 8020 → 8300 → 8310 → 9000 → 9010 → 9200，每个都实测），并告诉你改用了哪个。
> 这很有用：Windows 上 Hyper-V/WSL 会把 `7927–8126` 这类区间**保留**掉（报 `winerror 10013`），
> 表现是"端口没人占用但绑不上"。想关掉自动换端口用 `-NoFallback`；细节见
> [`scripts/run/README.md`](scripts/run/README.md)。

> 运行期**只有一个进程**：前端 `dist/` 由 FastAPI 用 `app.mount("/", StaticFiles(...))` 直接托管。
> 只有在改前端源码时才需要第二个窗口跑 `npm run dev`，那时请打开 **http://127.0.0.1:5173**
> （Vite 把 `/api` 与 `/ws` 转发给 8000）。

- 聊天界面：**执行过程实时可见** —— 逐行显示 `路由 → Planner 规划 → Executor 第 N 步调用了哪个工具 → Verifier 验收`；结束时给结构化结果（计划、可折叠的工具轨迹、验收徽章、token / 耗时）
- **人工确认**：「需确认」档下，写 / 执行类工具会弹框 —— 工具名、关键参数、风险等级（高危附影响面）；可「允许执行」或「拒绝」，可勾「**本会话内对该工具总是允许**」（默认不勾，切档位或换会话即失效）；**没人应答会倒计时自动拒绝**
- 会话列表：读 `runtime/checkpoints.db`；点任一会话即切换并回放历史，之后的对话在原会话上续聊
- 模型设置、按角色选模型、「我的模型」见上一节；**权限模式的持久化取舍**见[安全设计](#安全设计)。

## 功能

| 工具 | 能力 |
|---|---|
| 🖥️ PowerShell | 执行 Windows 命令、进程管理（危险命令黑名单拦截） |
| 🌐 搜索 | SearXNG 搜索（JSON API，**无需浏览器**；返回标题 / URL / 摘要 / 来源引擎 / 结果总数） |
| 🗄️ MySQL | 建库建表、CRUD、查询（参数化 + `_safe_ident` 标识符转义） |
| 🐧 WSL2 VM | Linux 命令执行、文件部署（危险命令拦截 + 超时） |
| 📚 RAG | 知识库 CRUD 闭环（ChromaDB 向量检索 + 本地 embedding） |
| 🔍 代码分析 | AST 解析、diff 生成、项目结构扫描、文件片段读取 |

> **关于浏览器**：搜索直接调 SearXNG 的 JSON API，**移除了 Selenium + Edge 整套**（调试端口、driver 版本匹配、滚动懒加载、HTML 清洗），`browser_tools.py` 从 **228 行降到 70 行**。要做「操作真实网页」（Computer Use / Browser Agent）应另建 Playwright 工具，与「搜索取数」是两件事。

## 上下文工程与分层记忆

> **定位先说清楚**：这一层里的 RAG 是**语义记忆** —— 记录**使用过程中积累的经验/习惯**
> （Agent 自学习闭环的存储端），**不是企业知识库问答**。所以它只带一份 35 条的**测试语料**
> （在 `evals/fixtures/`，只在评估时用；**产品库默认是空的**），
> 也不追求"大而全"的检索指标：**够用即可**。

长任务的 token 会随步数增长（每步重发全部历史）。五条机制管住它 —— 前四条管上下文，第五条管停止：

| 机制 | 做什么 | 关键参数（`.env` 可调） |
|---|---|---|
| **工具结果外置** | 过程性长输出落盘到 `runtime/tool_results/`，上下文里只留预览 + 路径 | `CODE_AGENT_EXTERNALIZE_THRESHOLD=6000`（字符）/ 150 行 |
| **对话压实** | 历史超阈值 → 最老的一段压成「目标 / 约束 / 已完成 / 未决」四段式摘要 | **按模型窗口自动算**（窗口的 25%；128k → 32000） |
| **token 预算** | 节点级（单次调用输入）超预算先剪枝（砍最老的工具结果）；任务级（单任务累计）超限**主动终止并报告** | 同上，窗口的 **50%** / `max(50 万, 4×窗口)`（128k → 64000 / 512000） |
| **分层记忆** | 语义记忆 = ChromaDB 知识库：**按语义块**建索引 + CrossEncoder 精排；任务开始时**自动注入**相关经验，任务成功后**自动沉淀**新经验 | `CODE_AGENT_RAG_*` |
| **可控性**（停止 / 墙钟） | **协作式停止**：只在三个检查点生效（节点入口 · ReAct 每一步 · 每次工具调用之前）——正在飞的那次模型调用不打断，但它之后的步骤一定不再开始；**墙钟**到点走同一条停止路径（原因码不同）；权限弹框期间**暂停计时** | `CODE_AGENT_TASK_WALL_CLOCK=900`（秒，0 = 不限） |

> **`read_file_range` / `read_file` 故意不参与外置**：实测把"读全文"的结果藏起来，模型会改用分段读绕过去 —— 同一道题的 token 反而从 17,361 涨到 127,071（7.3 倍）。外置只用于**过程性输出**（命令输出 / 目录清单 / 搜索结果）。
> **跑评估时「自动沉淀」「自动注入」默认关闭**（唯一例外 `E022`，它专门测 `query_rag`），且**每题开跑前把知识库复位**（日志里出现「1 篇已删清理」是正常且必需的）；注入内容记进结果 JSON 的 `knowledge_injected`。

**RAG 改造前 vs 改造后**（`uv run python evals/rag_ablation.py`；**2×2 消融 + 全量召回对照组**，
同一批 10 个查询、同一份知识库、同一个 embedding 模型 → 谁贡献了多少一目了然）：

| 组合 | 索引 | 精排 | 候选集 | top-1 命中**正解文件** | top-1 命中关键词（文件粒度） | top-1 落干扰项 | 稳态延迟 |
|---|---|---|---|---|---|---|---|
| **A** 改造前 | 整篇 | 否 | 全部 7 篇 | 0.40 | 0.70 | 0.60 | 13 ms |
| **B** | 整篇 | 是 | 全部 7 篇 | 0.70 | 1.00 | 0.30 | 189 ms |
| **C** | 分块 | 否 | 前 10 块 | 0.50 | 0.80 | 0.50 | 14 ms |
| **D 现状（生产）** | 分块 | 是 | 前 10 块 | **0.60** | **0.90** | **0.40** | 86 ms |
| **E** 对照 | 分块 | 是 | 全量 35 块 | **0.70** | **1.00** | **0.30** | 290 ms |

- **`correct_source_top1` 是主指标**：top-1 的来源*是不是这道题该去的那个文件*。只报"关键词命中"会高估质量 —— `with` 这种词哪个文件里都有，而"推荐 f-string"和"别用 f-string"都命中关键词。
- **A → D 是这次改造的真实变化**：正解文件 **0.40 → 0.60**、干扰项落第一 **0.60 → 0.40**；同口径的关键词命中 0.70 → 0.90。
- **局限**：知识库只有 4 篇正解 + 3 篇干扰（35 条原子，每篇 140~370 字符），语料小会把"整篇 vs 分块"的差距压小 ⟹ 上表是**真实下界，不可外推**；B 组（整篇 + 精排）属分布外输入；延迟是本机单进程稳态数字。原始结果：`docs/evidence/rag_ablation_20260924_053228.json`。

## 安全设计

> **定位**：本工具是**本地单用户**的，安全目标**不是防外人，而是防 Agent 自己（或误操作）把本机搞坏**。
> 所以边界画在**应用层**，而不是假装有 OS 级沙箱。

### 三层防线（判据不同，**故意不合并**）

| 层 | 在哪 | 管什么 | 随权限模式变吗 |
|---|---|---|---|
| ① **工具级档位表**（主防线） | `app/code_agent/security/permissions.py` | 这个工具在当前档位下**该不该**执行 | ✅ 就是它 |
| ② **内容级危险命令黑名单** | `mcp_servers/vm.py` / `powershell_tools.py` | 同一个工具，**这次参数**危不危险 | ❌ 不变（选「放开」也照样拦） |
| ③ MySQL 语句白名单 + 只读账号 | `mysql_tools.py` / `scripts/mysql-init/*.sql` | 只读工具被拿来写数据 | ❌ 不变 |

> ① 与 ② 不合并是因为**判据不同**：档位表问"有没有副作用"，黑名单问"这次参数危不危险"；和缓存名单（`CACHEABLE_TOOL_NAMES`）也是两张表，后者问"结果会不会变"。
>
> **权限真的拦得住（实测）**：E023 让 Agent 删废弃库，它发出 `DROP DATABASE`，被高危档拒绝；E024 只读档下改走 `Set-Content` 也被拒。两题都带状态断言（库还在 / 探针文件不存在），全过。

### 三档权限模式（默认「需确认」）

32 个工具分 A 类只读 **14** 个 / B 类写·执行 **18** 个（其中 6 个高危）：

| 档位 | A 类只读 | B 类写 / 执行 |
|---|---|---|
| **只读** | ✅ 放行 | 🚫 直接拒绝（不弹框，直接告诉模型"为什么不行 + 怎么改"） |
| **需确认**（默认） | ✅ 放行 | ⏸ 弹确认框（高危显示影响面） |
| **放开** | ✅ 放行 | ✅ 直接执行（危险命令黑名单仍生效；高危仍强制留痕） |

**未归类的工具在任何档位都拒绝** —— 白名单语义：以后新增工具忘了归类＝默认拒绝（有一条测试盯着"32 个工具一个不多一个不少"）。

**粒度是工具级、不是参数级**：B 类工具参数再无害也会弹（`write_file` 写个 `hello.py` 也弹）。代价是同一工具内参数危险差异被抹平，好处是规则简单可预测、不会因参数解析出错而漏放。

### 四个实现细节（都有测试守着：`tests/test_permissions.py`）

1. **权限判定在缓存查询之前** —— 否则"曾经允许过"的缓存会让**已被拒绝**的调用照样返回；
2. **被拒绝 / 否决的调用绝不写缓存**；
3. **拒绝用独立异常类型** —— 不会被"写失败 → 清缓存"那条分支当成执行失败；
4. **拒绝信息带"原因 + 出路"**，否则模型会反复重试同一件事、白烧 token。

### 权限模式的持久化

- 「只读 / 需确认」落盘（`runtime/web-settings.json`）；**「放开」永不持久化**，新会话回落「需确认」——手改配置塞 `"open"` 也只按「需确认」加载（"我上次设过、这次忘了"最容易埋雷）。
- CLI 默认「需确认」；无人值守入口必须显式指定档位（否则"没人应答 → 自动拒绝"，每个写工具都被拒）。

### 留痕（`runtime/permissions.log`）

每条**确认决定**（允许 / 拒绝 / 超时 / 无人值守被拒）和放开档下的高危操作追加一行 JSON：

```json
{"ts":"2026-09-21T13:03:27","scope":"manual-test","tool":"write_file_to_vm","mode":"readonly",
 "tier":"write","high_risk":true,"decision":"deny_mode","args":"{\"file_path\": \"/tmp/x\", ...}"}
```

该文件在 `runtime/` 下（**gitignore**），审计写入失败**只记日志、绝不影响工具调用**。

### WSL2 不是安全沙箱

WSL2 常被当沙箱，但它不是安全边界：

- 默认把 Windows 盘挂在 `/mnt/c`、`/mnt/e`…，而 `vm.py` 还主动在用这条通道（`windows_path_to_wsl_path()` 就是把 `E:\...` 转成 `/mnt/e/...`）；
- 命令以当前用户身份运行，没有内核级隔离；
- 内容级黑名单是字符串匹配，列不全：`& (Get-Command Remove-Item) -Recurse -Force C:\` 这类写法绕得过去。

所以对外一律说「**WSL2 隔离执行环境**」（命令黑名单 + 应用层三档权限）。真正的边界要 OS 级机制（Landlock + seccomp / Seatbelt / restricted token，或一次性容器），本项目**明确不做**。

### 也没有「Agent 只能在工作区内操作」这个保证

真实语义是"**文件工具**被关在工作区里"，不是"Agent 的活动范围被限制了"：

| | 限制情况 |
|---|---|
| **7 个文件工具**（`read_file` / `write_file` / `list_directory` / `file_search` / `move_file` / `copy_file` / `file_delete`） | ✅ 被 `FileManagementToolkit(root_dir=WORKSPACE_DIR)` 关在工作区 |
| **`execute_powershell_command`** | ❌ 完全不受限 —— cwd 是仓库根，能读写本机任何它有权限的路径 |
| VM（WSL）四个工具 | ❌ 接受任意 `dir_path`（所以都标了高危） |

⇒ **shell 的能力是文件工具的超集**：读、写、列、搜、删、移动复制，全都有一条 shell 路径。

**实测**：评估里 E003 用 `Get-ChildItem -Recurse` 扫了整个仓库（命中 `.env`、`runtime/` 下的日志与 checkpoint），E016 用 shell 把所有 python 进程杀了（连评估程序自己）。**这两件事都不算"越权"，因为本来就没有围栏。** 要真的围栏得新加一层（解析写工具的**路径参数**），但 shell 那条路只能做启发式，做不到严谨。

### 为什么"结构化工具"和 shell 并存

两条路有重叠（上一节），但**结构化工具对模型明显更友好**，所以有意都留着：

| | 结构化工具（`read_file_range` / `write_file` …） | shell（`execute_powershell_command`） |
|---|---|---|
| **参数** | 有 schema（名字 / 类型 / 必填 / 枚举），结构化传递，没有 quoting / 转义问题 | 要模型自己拼语法 —— 引号与转义是它最容易翻车的地方（E016 就是被引号和 `&` 坑死的） |
| **返回** | 可控：JSON、可带行号、可截断 | 一大坨裸文本 |
| **策略与权限** | 看得见 `file_path` 这个参数名，能按参数拦；能按工具名精确分档 | 只有一整条字符串，只能启发式、只能整体定档 |
| **缓存与 trace** | `(工具名, 参数)` 就是稳定 key，记录也可读 | 同一条命令有无数种写法，缓存不了，要解析命令行 |

**结论**：结构化工具是"能力收窄但可控"的通道，shell 是"能力最大但不可控"的逃生口。两者并存，并且**明确承认 shell 是不可控的那条**（见上一节）。

### 走查一条真实链路（实测）

```
[只读档] list_files_in_vm('/tmp')   → 放行（真跑，返回 677 字符）
[只读档] make_dir_in_vm             → ✅ 拒绝 decision=deny_mode（未执行、未缓存）
[只读档] write_file_to_vm           → ✅ 拒绝（high_risk=true 已写审计）
[放开档] make_dir_in_vm             → 放行（真实执行；非高危 → 不记审计，避免刷屏）
[需确认档] write_file              → ⏸ 前端弹框 → 点允许 → 执行；点拒绝 / 超时 → 不执行
```

内容级黑名单那层另有 87 条测试，包括**正反两向**：漏拦清单里的写法全部拦住、
正常操作（`rm -rf ./build`、`Remove-Item ./temp.txt`…）**不被误拦**，
并且用"打桩 `subprocess`"断言**危险命令根本走不到启动子进程那一步**（`tests/test_dangerous_commands.py`）。

## 评估体系

> 题集与评分器已从零重建，正式两轮已跑完并归档（2026-09-25）。
> 结果文件在 `docs/evidence/`：`v3-single.json`（单 Agent）/ `v3-multi.json`（多 Agent）。

### 正式结果（30 题 / 163 条断言）

| 轮次 | 通过 | 平均分 | 总 token | 总耗时 | 工具调用 | 步数 |
|---|---|---|---|---|---|---|
| **single**（单 Agent） | **30/30 = 100%** | **1.0000** | 958,832 | 332s | 194 | 362 |
| **multi**（Planner → Executor → Verifier） | **30/30 = 100%** | **1.0000** | 1,644,029 | 687s | 227 | 409 |

- **163 条断言两轮全过**：状态 **124/124**（真去查文件 / 库表 / WSL / 本机接口）、轨迹 **29/29**、文本 **10/10**；
- **8 个维度两轮全 1.00**：task_completion 5、safety 5、multi_step 4、cross_tool 4、
  tool_selection 3、error_recovery 3、context_management 3、efficiency 3（每题至少 1 条状态断言）；
- 两轮的口径：**single** = token 上限 200k + 每题墙钟 300~480s（逐题记在结果里）；
  **multi** = 两者都设成 **0（只计量、不拦截）**（写在该轮结果 JSON 的 `env` 快照里自证）。
  single 那轮跑得更早，快照里**没有**这两个字段 —— 别把两轮当成同一种自证方式。

### 双模式对照（single vs multi）

| 指标 | single | multi | 倍数 |
|---|---|---|---|
| token 总量 | 958,832 | 1,644,029 | **1.71×** |
| token 逐题中位 | — | — | **1.58×** |
| 总耗时 | 332s | 687s | 2.07× |
| Planner 固定开销 | — | 1,110 token/题（2.4%） | — |
| **Verifier 固定开销** | — | **中位 10,988 token/题（总量的 25%）** | — |
| **Verifier 打回次数** | —（single 无 Verifier） | **0** | — |

**怎么读这份对照**：这个任务集对当前模型已经饱和（两轮都 100%），所以**架构差异不体现在分数上**，只能从成本侧看 —— Planner + Verifier 在本题集上**没有提高分数**，代价是**中位 1.58× token**，其中**四分之一**花在 Verifier 的独立验收上。结论是**适用边界**，不是"多 Agent 更强"。

**一条口径实证（人为闸门会制造假失败）**：multi 第一次带 **200k token 上限**跑，结果 **29/30** —— E015 被上限掐断（烧掉 257k 仍未完成）；改成**只计量、不拦截**后同一题集 **30/30**，而且总成本更低（1.64M vs 1.78M）⟹ 那道题不是做不出来，是**被人为闸门判死的**。限额版留在 `docs/evidence/v3-multi-limit200k.json`。

> **适用范围（别外推）**：每轮每题只跑 1 次，是单样本；任务集已饱和 ⟹ 分数只说明"在这 30 题上稳定做对"，**不能**据此说它比单 Agent 强或弱；RAG 消融的语料只有 35 条原子、CrossEncoder 偏词汇匹配，那些数字是**真实下界**。

**旧题集已整体删除**：14/30 题没有"产物级"断言、部分安全题判定器空转且恒定满分 ⟹ 判据不成立，所以删掉而不是标注。细节见 `docs/records/2026-09-25_评估口径与缺陷账.md`。

**新尺子（`evals/`，11 个文件 + 夹具目录）**：

| 文件 | 职责 |
|---|---|
| `tasks.py` | **30 题题集**：基础 10 / 长任务 12 / 对抗 8；8 个维度各 ≥3 |
| `verifiers.py` | **43 个判定器工厂**，四档强度；30 题共用 **163 条**断言（状态 124 / 轨迹 29 / 文本 10） |
| `runner.py` / `run_e2e.py` | 执行引擎（清残留 → 跑题，超时也验分 → 汇总落盘）与命令行入口 |
| `preflight.py` | **跑前环境预检**（容器 / WSL / `.env` key / 端口 / 知识库）—— 缺一条那一轮数据就废了，还不报错 |
| `report.py` | 把结果 JSON 变成能进 `docs/evidence/` 的 Markdown（含 STAR 量化对比） |
| `merge_runs.py` | 逐题分片 → 合并成一轮（缺题**拒绝写出**） |
| `reset_eval_threads.py` | 清库里的 eval 线程 —— **复用同名 run-id 前必跑** |
| `rag_bench.py` / `rag_ablation.py` | RAG 基准与消融对照（不需要 LLM） |
| `env.py` | **语料隔离**：`use_eval_corpus()` 指到 `runtime/eval_knowledge/` + `chroma_db_eval/`，**不碰产品的 `data/knowledge/`** |

**评分口径**：**通过 = 满分**（不给部分分留后门）、**`skip` ≠ 0 分**（环境不可用记 `unavailable`、不进分母）、**超时 / 异常也跑判定器**；判定器按真实 MCP 工具名与参数名写，且**每道题至少 1 条状态断言**（去查真实文件 / 库表 / WSL / 接口），由测试机械守着。

```bash
uv run python evals/reset_eval_threads.py --yes        # ① 复位 eval 线程（复用 run-id 前必跑）
uv run python evals/preflight.py --run-id v3-single    # ② 预检，修完阻塞项再跑
uv run python evals/run_e2e.py --list                  # 只看题集结构（不执行、不烧 token）
uv run python evals/run_e2e.py --task E001 --mode single --run-id v3-single-E001   # ③ 逐题跑，E001..E030
uv run python evals/merge_runs.py --prefix v3-single --mode single --archive       #    合并 + 归档（缺题拒绝写出）
uv run python evals/merge_runs.py --prefix v3-multi  --mode multi  --archive       # ④ multi 轮换前缀
uv run python evals/report.py --single runtime/runs/v3-single.json \
                              --multi runtime/runs/v3-multi.json \
                              --rag docs/evidence/rag_ablation_20260924_053228.json \
                              --out docs/evidence/评估报告.md                      # ⑤ 出报告
```

> **为什么逐题跑**：一个进程跑一整轮时**结果 JSON 只在整轮结束写一次**，中途被掐断就整轮白跑 ⇒
> 逐题跑之后"被掐断"不再是灾难（但**合并必须等 30 题都齐**，缺题拒绝写出）。
> 每次跑**必须换 `run-id`**（复用会读到上一轮的 checkpoint）；`--archive` 只归档**合并后的那一份**。
> 跑之前确认 `.env` 的 key 有效 —— **CLI 与评估都不读界面设置**（预检会替你验一次，不烧 token）。

**RAG 检索基准与消融对照**（与题集无关，**不需要 LLM**）：

```bash
uv run python evals/rag_bench.py                        # 现状快照：延迟 / 准确率 / 召回 / 排序
uv run python evals/rag_ablation.py --reps 10 --archive # 改造前后消融（结果归档进 docs/evidence/）
```

## 如何验证这个项目

一条命令能跑的都列在这儿（本机 Windows + 已 `uv sync`）：

| 做什么 | 命令 | 说明 |
|---|---|---|
| 单元 + 工具级测试 | `uv run python -m pytest tests/ -q` | **743 条，约 30 秒**；其中 5 条集成测试默认**不跑** |
| 静态检查 | `uv run ruff check .` + `uv run ruff format --check .` | CI 里跑的就是这两条 |
| **真集成测试**（要真依赖） | `uv run python -m pytest -m integration -v` | **5 条**：真 MySQL / 真 WSL / 真 SearXNG / 真 Redis / 真 RAG；环境不可用**自动跳过**。只能 Windows 本机跑（Linux runner 没有 WSL），所以**不在 CI 里** |
| **RAG 本地模型装没装** | `uv run python scripts/fetch_models.py --dry-run` | 只看状态、**不下载**；缺模型时直接给装法 |
| 改完前端重建产物 | `cd app/web/frontend && npm run build` | `dist/` **入库**（clone 下来不装 Node 也能开界面）；改前端后**必须重建并提交产物** |

> 评估相关的命令（预检 / 看题集 / 出报告 / RAG 基准与消融）见「评估体系」一节。CI（GitHub Actions + Gitee）跑 `ruff check` → `ruff format --check` → `pytest tests/ -v`，**不需要 `.env`**（`tests/conftest.py` 会兜一个假 key）。

## 技术栈

| 层 | 选型 |
|---|---|
| Agent 框架 | LangGraph（StateGraph + `create_react_agent`） |
| MCP 适配 | langchain-mcp-adapters + FastMCP（stdio 子进程） |
| LLM | ChatOpenAI → DeepSeek（任意 OpenAI 兼容接口；按角色配 + 降级链 + 超时） |
| 向量库 / Embedding / Rerank | ChromaDB（本地持久化） / all-MiniLM-L6-v2 / CrossEncoder ms-marco-MiniLM-L-6-v2（本地加载，缺失降级） |
| 缓存 / 搜索 / 数据库 | Redis（只读工具结果缓存，挂了降级） / SearXNG（JSON API） / PyMySQL |
| Web / 打包 | FastAPI + WebSocket；前端 Vue 3 + Vite；uv（Python 3.13） |

## 项目结构

```
├── main.py                    # CLI 入口（argparse：thread-id / new-session / mode / permission / debug）
├── app/
│   ├── code_agent/
│   │   ├── agent/             # multi_agent.py（★ 状态图）/ cancel.py（停止·墙钟）
│   │   │                      #   context.py（结果外置·压实·预算）/ memory.py（自动注入·沉淀）
│   │   │                      #   usage.py（token 计量）/ events.py（节点进度）/ code_agent.py（REPL + run_single_task）/ prompts.py
│   │   ├── security/permissions.py  # ★ 三档档位表（32 工具）+ 判定 + 人工确认 + 审计
│   │   ├── model/llm.py       # LLMRegistry：按角色取模型 + 降级链
│   │   ├── mcp_servers/       # 5 个 MCP Server（powershell 2 / browser-搜索 1 / mysql 10 / vm 4 / code_tools 4 = 21）
│   │   ├── rag/               # rag.py（MCP 薄壳，4 工具，第 6 个 Server）/ store.py（分块+精排）/ chunking.py
│   │   ├── tools/file_tools.py  # FileManagementToolkit(root_dir=WORKSPACE_DIR) → 7 个文件工具
│   │   └── utils/             # mcp.py（加载）/ tool_wrap.py（权限+外置+缓存）/ tool_cache.py / placeholder_guard.py
│   └── web/                   # server.py（FastAPI + WS + 权限确认协议）/ sessions.py / frontend/（Vue3 + Vite，dist 入库）
├── config/models.json         # 模型注册表（默认空 = 走 .env）+ 角色分配 + 降级链
├── data/knowledge/            # 产品知识库：默认空（靠使用积累）；测试语料在 evals/fixtures/
├── scripts/                   # fetch_models.py（装 RAG 两个本地模型）/ mysql-init/*.sql / run/（起停脚本）
├── runtime/                   # gitignore：checkpoints · sessions · web-settings · permissions.log · tool_results 等
├── evals/                     # 30 题 + 43 判定器 + 预检 + 合并 + 语料隔离（详见「评估体系」）
├── tests/                     # 743 个测试（含默认不跑的集成测试 test_integration_mcp.py）
├── docs/                      # architecture.md（6 条 ADR）· evidence/（原始结果）· records/（过程账）· archive/
├── AGENTS.md                  # AI 助手约定与已知坑
├── docker-compose.yml         # mysql / searxng / redis（nginx 由 WSL 侧 compose 管）
└── .gitee.yml                 # CI（ruff check → ruff format --check → pytest）
```

## 关键数字与复核命令

数值见上面「[概览](#概览)」与「[评估体系](#评估体系)」；这里只给怎么自己核。

| 数字 | 复核命令 |
|---|---|
| 测试覆盖率 | `uv run python -m pytest tests/ -q`（`addopts` 自带 `--cov`，看 `TOTAL` 行）。随环境与提交波动（分母会随首次 import 新模块变大）⇒ 别跨提交直比 |
| 知识库条目 | `Get-ChildItem evals/fixtures/knowledge -Recurse -File`（测试语料 35 条 = 7 文件 × 5 条；产品库默认空） |
| MCP 工具数 | 启动后看日志 `共加载 N 个工具` |
| 评估结果与成本画像 | 读 `docs/evidence/v3-single.json` / `v3-multi.json` 的 `totals` / `node_timings`（限额版对照 `v3-multi-limit200k.json`） |

## 文档地图

| 文档 | 是什么 | 性质 |
|---|---|---|
| `README.md` | 怎么装、怎么跑、能力总览、评估结果、已知边界 | **对外入口**（本文件） |
| `AGENTS.md` | 约定 / 常用命令 / 已知坑 / **当前状态** | AI 操作手册（活文档） |
| `docs/architecture.md` | 设计取舍与代价（6 条 ADR） | 活文档 |
| `docs/records/` | 过程账：某个坑**怎么修的、证据在哪** | 活文档（可编辑） |
| `docs/evidence/` | 评估**原始结果** + 报告 + 截图 | **只追加、不改写** |
| `docs/archive/` | 历史冻结快照（改造前的交接 / 历史版本） | **不再更新** |

## 已知问题与后续规划

**边界**

- **本机单用户**：没有多用户 / 账号 / 部署形态。它会执行 shell、读写主机文件，定位就是"跑在你自己机器上"。
- **可观测性有限**：只有节点级进度事件（WebSocket）与权限审计日志（`runtime/permissions.log`），没有 metrics / tracing。
- **多标签页可以各跑一个任务**（后端是「每条连接一个任务」）⟹ **别让两个任务同时改同一个工作区**，会互相覆盖。同一标签页内是串行的：任务执行中切会话 / 新建会话会被拦住并提示。

**已知技术债**

- RAG 的 4 个工具走 MCP 子进程，每次调用付 ≈8 秒启动开销（换来"不在事件循环里加载原生扩展"）。改进方向：`query_rag` 改进程内工具，或让 MCP server 常驻。
- Verifier 打回上限 2 次；到上限以最后一次结果收尾（不会无限重试，但可能带着未解决的问题结束）。
- 评估每轮每题只跑 1 次，方差量化不出来。

**后续规划**（按价值排序）

1. RAG 工具本地化 / MCP 常驻（去掉那 8 秒）；
2. 评估加重复轮次与方差统计；
3. 更多"工具级"回归（现在真集成测试只有 5 条）。

## License

本项目采用 **MIT 许可**（见 [`LICENSE`](LICENSE)）—— 可自由使用、修改、分发，保留版权声明即可。

Copyright (c) 2026 ColdZeda
