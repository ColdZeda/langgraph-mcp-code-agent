# Code Agent

> **给谁看**：第一次接触这个项目的人（面试官 / 同行 / 想跑起来的人）。
> 想了解内部约定与已知坑 → [`AGENTS.md`](AGENTS.md)；接手人的上下文与待办 → [`docs/handover.md`](docs/handover.md)。

基于 **LangGraph + MCP** 的本地多 Agent 编程助手（Python 3.13）：**Planner → Executor → Verifier** 三阶段 StateGraph，
通过 MCP stdio 子进程统一编排 6 类工具，提供**命令行**与**本地 Web UI** 两种使用方式。

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

| 项 | 说明 |
|---|---|
| Python | 3.13+ |
| 包管理 | [uv](https://docs.astral.sh/uv/) |
| 操作系统 | Windows（PowerShell 工具与 WSL2 工具依赖宿主环境） |
| 可选 | WSL2 Ubuntu（虚拟机工具）、Docker（MySQL / SearXNG / Redis） |

### 安装

```bash
git clone https://gitee.com/wdnmded/langgraph-mcp-code-agent.git
cd langgraph-mcp-code-agent
uv sync
```

### 配置

复制 `.env.example` 为 `.env`，至少填入 API Key：

```env
MODEL_API_KEY=你的API密钥
# 可选: MODEL_NAME=deepseek-v4-flash
# 可选: MODEL_BASE_URL=https://api.deepseek.com
# 可选: CODE_AGENT_LLM_TIMEOUT=60      # 单次调用超时（秒）
```

**模型是按角色配的**（Planner / Executor / Verifier / Router），配置在 **`config/models.json`**（进版本控制）：

```jsonc
"roles":    { "planner": "ds-v4-flash", "executor": "ds-v4-flash",
              "verifier": "ds-v4-flash", "router": "ds-v4-flash" },
"fallback": { "executor": [] }   // 填备用模型即启用"主力报错/超时自动降级"
```

- 默认四个角色同一个模型（行为可预期）；可在 **Web UI 的模型设置面板**里分别选，改完热生效；
- 跑评估时可临时覆盖而不改配置：`--role-models "planner=x,executor=y"`；
- 每次运行的结果 JSON 会记录 `role_models`（哪个角色用了哪个模型）。

### 运行（命令行）

```bash
uv run python main.py                        # 默认启动（执行模式 auto）
uv run python main.py --thread-id my-session # 指定会话 ID
uv run python main.py --mode single          # 执行模式：auto（默认）/ single / multi
uv run python main.py --debug                # 调试模式（详细日志）
```

退出：`exit` / `quit` / `q` / `退出` / `bye`

> **跨轮记忆**存在 `runtime/checkpoints.db`：不传参数时用 `.env` 的 `CODE_AGENT_THREAD_ID`
> （默认 `default`）→ **关掉再打开会继续上一次的对话**；想开一个新会话用 `--new-session`。

### 运行（Web UI）

```bash
uv run uvicorn app.web.server:app --port 8000
# 浏览器打开 http://localhost:8000
```

- 聊天界面：任务完成后一次性推送结构化结果——Planner 计划、工具调用轨迹（可折叠）、
  Verifier 验收徽章、token / 耗时统计；**顶部有执行模式下拉框**（auto / single / multi）
- 模型设置：界面内热切换模型 / API 地址 / Key，**四个角色（Planner / Executor / Verifier / Router）
  分别选模型**（数据源是 `config/models.json`）；设置只存本机 `runtime/web-settings.json`，不进仓库
- 会话列表：读 `runtime/checkpoints.db`；**点击任一会话即可切换并回放历史**，之后的对话在原会话上续聊

> 前端（Vue 3 + Vite）源码在 `app/web/frontend/`，构建产物 `dist/` 已入库——不装 Node 也能直接运行；
> 改前端后 `cd app/web/frontend && npm install && npm run build` 重新构建。

### 启动依赖服务

```powershell
.\scripts\start-deps.ps1     # 一键起全部 4 个：mysql / searxng / redis / nginx
.\scripts\stop-deps.ps1      # 停止（保留容器，下次起得更快）
```

或手动分两步：

```powershell
docker compose up -d                                            # mysql / searxng / redis
wsl -d Ubuntu -- bash -lc "cd ~/nginx && docker compose up -d"  # nginx
```

| 服务 | 容器 | 端口 | 说明 |
|---|---|---|---|
| MySQL 沙盒 | `agent-mysql` | 3307→3306 | 数据存在**命名卷** `mysql-data`；首次初始化会执行 `scripts/mysql-init/*.sql` |
| 搜索 | `searxng` | 8888→8080 | 配置/缓存在 `E:\agentstart\work\searXNG\{config,data}` |
| 缓存 | `redis-stack-server` | 6379 | 纯缓存，**故意不挂卷**（数据可丢） |
| 静态发布 | `my-nginx` | 80 | 挂载源在 WSL（见下），由 WSL 里那份 compose 管理 |

**为什么 Agent 主体不容器化**：6 个 MCP 工具里，**PowerShell（`powershell.exe`）与 WSL2（`wsl.exe`）**
必须依赖 Windows 宿主环境，Linux 容器里跑不了。

**为什么 nginx 单独管理**：它的挂载源是 WSL 里的 `/home/leprite/nginx/*`（配合 `vm.py` 的"上传产物到 WSL"链路），
而主 compose 在 Windows 侧执行 —— 从 Windows 跑会把 Linux 路径解析到 docker-desktop 发行版，
导致**静默挂载空目录**（不报错，最难查）。所以两边分开管，`start-deps.ps1` 会把两边都拉起来。

> 4 个容器都带 `restart: unless-stopped` → **打开 Docker Desktop（= 启动 Docker 引擎）时会自动起来**。
> 例外：如果你**手动 stop** 过某个容器，引擎不会自动起它（这是 `unless-stopped` 的定义），
> 这时用 `scripts\start-deps.ps1` 即可。

## 功能

| 工具 | 能力 |
|---|---|
| 🖥️ PowerShell | 执行 Windows 命令、进程管理（危险命令黑名单拦截） |
| 🌐 搜索 | SearXNG 搜索（JSON API，**无需浏览器**；返回标题 / URL / 摘要 / 来源引擎 / 结果总数） |
| 🗄️ MySQL | 建库建表、CRUD、查询（参数化 + `_safe_ident` 标识符转义） |
| 🐧 WSL2 VM | Linux 命令执行、文件部署（危险命令拦截 + 超时） |
| 📚 RAG | 知识库 CRUD 闭环（ChromaDB 向量检索 + 本地 embedding） |
| 🔍 代码分析 | AST 解析、diff 生成、项目结构扫描、文件片段读取 |

> 📌 **关于浏览器**：搜索已改为直接调 SearXNG 的 JSON API，**移除了 Selenium + Edge 那一整套**
> （调试端口、msedgedriver 版本匹配、滚动懒加载、HTML 清洗），`browser_tools.py` 从 **228 行降到 70 行**，
> 环境要求也更简单。
> 若将来要做「**操作真实网页**」（Computer Use / Browser Agent：点击、填表、截图），
> 应另建 Playwright 工具 —— 那与「搜索取数」是两件事。

## 上下文工程与分层记忆

> **定位先说清楚**：这一层里的 RAG 是**语义记忆** —— 记录**使用过程中积累的经验/习惯**
> （Agent 自学习闭环的存储端），**不是企业知识库问答**。所以它只预置了 35 条知识，
> 也不追求"大而全"的检索指标：**够用即可**。

长任务最容易失控的不是"模型不够聪明"，而是**上下文管理**：一次任务读 20 个文件、
每步都要把全部历史重发一遍，token 随步数平方增长。这个项目做了四件事：

| 机制 | 做什么 | 关键参数（`.env` 可调） |
|---|---|---|
| **工具结果外置** | 过程性长输出落盘到 `runtime/tool_results/`，上下文里只留预览 + 路径 | `CODE_AGENT_EXTERNALIZE_THRESHOLD=6000`（字符）/ 150 行 |
| **对话压实** | 历史超阈值 → 最老的一段压成「目标 / 约束 / 已完成 / 未决」四段式摘要 | `CODE_AGENT_COMPACT_THRESHOLD=6000`（估算 token） |
| **token 预算** | 节点级超预算先剪枝（砍最老的工具结果）；任务级超限**主动终止并报告** | `*_NODE_TOKEN_BUDGET=30000` / `*_TASK_TOKEN_BUDGET=200000` |
| **分层记忆** | 语义记忆 = ChromaDB 知识库：**按语义块**建索引 + CrossEncoder 精排；任务开始时**自动注入**相关经验，任务成功后**自动沉淀**新经验 | `CODE_AGENT_RAG_*` |

> ⚠️ **`read_file_range` / `read_file` 故意不参与外置**：实测把"读全文"的结果藏起来，
> 模型会改用分段读绕过去 —— 同一道题的 token 反而从 17,361 涨到 127,071（7.3 倍）。
> 外置只用于**过程性输出**（命令输出 / 目录清单 / 搜索结果）。
> ⚠️ **自动沉淀在跑评估时关闭**（`run_single_task` 强制关）：否则评测过程产生的经验会写进知识库，
> 让后续题目的检索结果改变、同一批数据前后不可比。

**RAG 改造前 vs 改造后**（`uv run python evals/rag_bench.py`，同一批 10 个查询、同一套口径）：

> ⚠️ **下表是阶段 4 自测的临时数字，阶段 6 会用新口径重测**（评分器与题集都在阶段 6 重做），
> 所以请把它当作"改造方向对不对"的旁证，**不要当成正式结论**。

| 指标 | 改造前（整篇一个向量） | 改造后（分块 + 精排） |
|---|---|---|
| top-1 命中（文件粒度，同口径） | 0.6 | **0.9** |
| top-3 命中（文件粒度，同口径） | 1.0 | **1.0** |
| Python 主题召回 | 0.4 | **1.0** |
| 稳态查询延迟 | 13.2ms | **81ms**（精排 10 对约占 71ms） |

> 另外单列两个更严的指标（关键词命中分不清「推荐 f-string」和「别用 f-string」这类**故意写错的干扰项**）：
> top-1 来自正解文件 **0.6**、top-1 落在干扰项 **0.4** —— 测试集很小（35 块），这是**已知局限**。

## 评估体系

**30 题端到端评估**（`evals/`），覆盖 6 个能力维度：
`tool_selection`（工具选择）/ `task_completion`（任务完成）/ `multi_step`（多步推理）/
`cross_tool`（跨工具协作）/ `error_recovery`（错误恢复）/ `safety`（安全）。

> ⚠️ **下表是「改造前」的存档，文件现在已不在仓库里** —— `docs/evidence/` 的内容已移出仓库
> （备份在 `E:\agentstart\work\backup\1new\backup\old-data\docs\evidence\`，也能用
> `git show 1ea2687^:docs/evidence/<文件名>` 从历史取回）。
> 留着它们是为了说明"改造前长什么样"；**当前架构的成绩，要等评估体系重做（阶段 6）之后才有效**。

| 阶段 | 存档文件（已移出仓库） | overall | pass_rate | total_tokens | 平均延迟 |
|---|---|---|---|---|---|
| 改造前基线（单 Agent） | `baseline-final.json` | **0.983** | 1.0 | 978,865 | 34.0s |
| optimized 单 Agent | `evals-optimized-final.json` | **1.0** | 1.0 | 896,475 | 30.3s |
| 多 Agent（改造前那版） | `evals-multiagent-merged.json` | **0.967** | 0.967 | 1,257,397 | 53.4s |
| RAG 基准（独立基准） | `rag-bench-baseline.json` | top1 **0.6** / top3 1.0 / recall **0.4** | — | — | 13.4ms |

**⚠️ 读这些数字前必看的口径说明**（细节见存档里的 `README.md` 与 `evals-baseline-report.md`）：

1. 全部基于 `deepseek-v4-flash` 跑出，**换模型后不可比**；
2. 当时的评分器里**弱断言占比不小**（实测 **14/30 题没有任何"产物级"断言**），
   所以这些分数应理解为「**回归通过率**」，不是「**通用任务成功率**」；
3. `pass_rate` 把 `score >= 0.5` 记为通过，而部分 verifier 会给 0.5 的**部分分** → 偏乐观；
4. 项目正在做一轮系统性改造（见 `docs/handover.md`），**阶段 6 会重做评分器与题集，并归档新口径的结果**。

```bash
# 跑全量（30 题）
uv run python evals/run_e2e.py --all --run-id baseline

# 跑单题（用新 run-id，避免覆盖）
uv run python evals/run_e2e.py --task E001 --run-id single-test

# 指定执行模式（默认 auto）与临时角色模型
uv run python evals/run_e2e.py --all --mode multi --role-models "planner=ds-v41-flash"

# RAG 基准
uv run python evals/rag_bench.py
```

特性：每题含**明文对话存档**（失败可定位到具体一步）、token 用量统计、按 run-id 断点续跑。
结果先写 `runtime/runs/`（gitignore）；正式结果才复制到 `docs/evidence/` 纳入版本控制
（该目录当前为空，阶段 6 重做评估后会重新写入）。

## 技术栈

- **Agent 框架**：LangGraph（StateGraph + `create_react_agent`）
- **MCP 适配**：langchain-mcp-adapters + FastMCP（stdio 子进程）
- **LLM**：ChatOpenAI → DeepSeek（可切换任意 OpenAI 兼容接口）；
  **按角色可配**（`config/models.json`）+ 降级链 + 超时
- **向量数据库**：ChromaDB（本地持久化，按语义块索引）
- **Embedding**：sentence-transformers（all-MiniLM-L6-v2，本地运行）
- **Rerank**：sentence-transformers 的 CrossEncoder（ms-marco-MiniLM-L-6-v2，**本地路径加载，缺失则降级**）
- **缓存**：Redis（只读工具结果缓存，挂了自动降级）
- **搜索**：SearXNG（JSON API，经 httpx 调用，不经过浏览器）
- **数据库**：PyMySQL
- **Web**：FastAPI + WebSocket；前端 Vue 3 + Vite
- **包管理**：uv（Python 3.13）

## 项目结构

```
├── main.py                        # CLI 入口（argparse：--thread-id / --new-session / --mode / --debug）
├── app/
│   ├── code_agent/
│   │   ├── agent/
│   │   │   ├── multi_agent.py     # ★ 状态图（route_node / planner / executor / verifier + 条件边）
│   │   │   ├── context.py         # 上下文工程：结果外置 / 历史压实 / token 预算
│   │   │   ├── memory.py          # 分层记忆：自动注入 + 自动沉淀
│   │   │   ├── code_agent.py      # REPL 循环 + evals 非交互接口 run_single_task
│   │   │   └── prompts.py         # System / Planner / Verifier / Executor（计划版）提示词
│   │   ├── model/llm.py           # LLMRegistry：get_llm(role) / chain / invoke_with_fallback
│   │   ├── config.py              # 所有配置（从 .env 读）+ setup_logging（stderr）
│   │   ├── mcp_servers/           # 6 个 MCP Server（powershell / 搜索 / mysql / vm / code_tools）
│   │   │                          #   └ browser_tools.py = 搜索（JSON API）；文件名是历史遗留
│   │   ├── rag/                   # rag.py（MCP 薄壳）/ store.py（分块+精排）/ chunking.py（纯函数）
│   │   ├── tools/
│   │   │   └── file_tools.py      # FileManagementToolkit（限定在 workspace）
│   │   └── utils/                 # mcp.py（工具加载）/ tool_wrap.py（外置+缓存）/ tool_cache.py
│   └── web/
│       ├── server.py              # FastAPI（WS + REST + 静态托管）
│       └── frontend/              # Vue3 + Vite（dist 已入库）
├── config/models.json             # 模型注册表 + 角色分配 + 降级链（进版本控制）
├── data/knowledge/                # 知识库源文件：35 条（7 个文件 × 每文件 5 条）
├── scripts/                       # start-deps.ps1 / stop-deps.ps1 / mysql-init/*.sql
├── runtime/                       # ⚠️ gitignore：checkpoints.db + tool_results / chroma_db / workspace / runs
├── evals/                         # 评估：任务的题集 / 评分器 / runner / RAG 基准
├── tests/                         # 156 个测试（单元 + 工具级）
├── docs/
│   └── handover.md                # 交接文档（evidence/ 与 archive/ 的内容已移出仓库）
├── AGENTS.md                      # AI 助手约定与已知坑
├── docker-compose.yml             # mysql / searxng / redis 三个依赖服务（nginx 由 WSL 侧 compose 管）
└── .gitee.yml                     # CI（ruff check → ruff format --check → pytest）
```

## 数量与来源对照（每个数字都能复核）

| 数字 | 值 | 复核命令 |
|---|---|---|
| 测试数 | 156 | `uv run python -m pytest tests/ -q` |
| 评估题数 | 30 | `uv run python -c "from evals.tasks import TASKS; print(len(TASKS))"` |
| 知识库条目 | 35（7 文件 × 5 条）；分块后 = 35 块 | `Get-ChildItem data/knowledge -Recurse -File` |
| MCP 工具数 | 32（含 7 个文件工具） | 运行 `uv run python main.py`，看日志 `共加载 N 个工具` |
| 测试覆盖率 | **68%**（1451 语句 / 458 未覆盖） | `uv run python -m pytest tests/ -q`（addopts 自带 `--cov=app/code_agent`） |
| RAG 检索指标（**阶段 4 临时数**，阶段 6 重测） | top1(文件粒度) 0.9 / top3 1.0 / recall 1.0 / 稳态 81ms | `uv run python evals/rag_bench.py`（结果也写入 `runtime/runs/rag_bench_*.json`） |
| 评估指标（改造前旧口径） | 见上表 | 存档已移出仓库 → `git show 1ea2687^:docs/evidence/<文件名>` |

## License

MIT
