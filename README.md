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
   多 Agent 协作图（app/code_agent/agent/multi_agent.py, LangGraph StateGraph）
   Planner（纯 LLM 规划，输出结构化计划 JSON）
        ↓
   Executor（create_react_agent，全量工具，按计划执行）
        ↓
   Verifier（只读白名单 12 个工具，对照「需求 + 计划 + 执行轨迹」验收）
        ↓ FAIL → 带原因打回 Executor（⚠️ 打回上限当前未生效，见下）

   ├── LLM：DeepSeek（OpenAI 兼容接口；Web UI 内可热切换模型 / 地址 / Key）
   ├── Memory：SqliteSaver（`runtime/checkpoints.db`，按 thread_id 恢复跨轮对话，
   │            进程重启后仍记得；Web UI 的"历史会话"就是读它）
   └── Tools：6 个自建 MCP Server（stdio 子进程）+ FileManagementToolkit
       ├── powershell_tools.py   Windows 命令执行（危险命令黑名单）
       ├── browser_tools.py      Selenium Edge + SearXNG 搜索
       ├── mysql_tools.py        MySQL 增删改查（参数化 + 标识符转义）
       ├── vm.py                 WSL2 桥接（危险命令拦截 + 超时）
       ├── code_tools.py         AST 解析 / diff / 项目结构扫描 / 文件片段读取
       └── rag/rag.py            ChromaDB 知识库（CRUD 闭环 + 本地 embedding）
```

## 快速开始

### 环境要求

| 项 | 说明 |
|---|---|
| Python | 3.13+ |
| 包管理 | [uv](https://docs.astral.sh/uv/) |
| 操作系统 | Windows（PowerShell 工具、WSL2 工具、Edge 依赖宿主环境） |
| 可选 | WSL2 Ubuntu（虚拟机工具）、Docker（MySQL / SearXNG） |

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
```

### 运行（命令行）

```bash
uv run python main.py                        # 默认启动
uv run python main.py --thread-id my-session # 指定会话 ID
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
  Verifier 验收徽章、token / 耗时统计
- 模型设置：界面内热切换模型 / API 地址 / Key（设置只存本机 `runtime/web-settings.json`，不进仓库）
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
| 🌐 浏览器 | Selenium + SearXNG 网页搜索（实时信息） |
| 🗄️ MySQL | 建库建表、CRUD、查询（参数化 + `_safe_ident` 标识符转义） |
| 🐧 WSL2 VM | Linux 命令执行、文件部署（危险命令拦截 + 超时） |
| 📚 RAG | 知识库 CRUD 闭环（ChromaDB 向量检索 + 本地 embedding） |
| 🔍 代码分析 | AST 解析、diff 生成、项目结构扫描、文件片段读取 |

## 评估体系

**30 题端到端评估**（`evals/`），覆盖 6 个能力维度：
`tool_selection`（工具选择）/ `task_completion`（任务完成）/ `multi_step`（多步推理）/
`cross_tool`（跨工具协作）/ `error_recovery`（错误恢复）/ `safety`（安全）。

| 阶段 | 存档文件 | overall | pass_rate | total_tokens | 平均延迟 |
|---|---|---|---|---|---|
| 改造前基线（单 Agent） | `docs/evidence/baseline-final.json` | **0.983** | 1.0 | 978,865 | 34.0s |
| optimized 单 Agent | `docs/evidence/evals-optimized-final.json` | **1.0** | 1.0 | 896,475 | 30.3s |
| 多 Agent（当前架构） | `docs/evidence/evals-multiagent-merged.json` | **0.967** | 0.967 | 1,257,397 | 53.4s |
| RAG 基准（独立基准） | `docs/evidence/rag-bench-baseline.json` | top1 **0.6** / top3 1.0 / recall **0.4** | — | — | 13.4ms |

**⚠️ 读这些数字前必看的口径说明**（详见 `docs/evidence/README.md`）：

1. 全部基于 `deepseek-v4-flash` 跑出，**换模型后不可比**；
2. 当时的评分器里**弱断言占比不小**（实测 **14/30 题没有任何"产物级"断言**），
   所以这些分数应理解为「**回归通过率**」，不是「通用任务成功率」；
3. `pass_rate` 把 `score >= 0.5` 记为通过，而部分 verifier 会给 0.5 的**部分分** → 偏乐观；
4. 项目正在做一轮系统性改造（见 `docs/handover.md`），**完成后会重做评估体系并归档新结果**。

```bash
# 跑全量（30 题）
uv run python evals/run_e2e.py --all --run-id baseline

# 跑单题（用新 run-id，避免覆盖）
uv run python evals/run_e2e.py --task E001 --run-id single-test

# RAG 基准
uv run python evals/rag_bench.py
```

特性：每题含**明文对话存档**（失败可定位到具体一步）、token 用量统计、断点续跑（按 run-id）。
结果先写 `runtime/runs/`（gitignore），正式结果复制到 `docs/evidence/` 纳入版本控制。

## 技术栈

- **Agent 框架**：LangGraph（StateGraph + `create_react_agent`）
- **MCP 适配**：langchain-mcp-adapters + FastMCP（stdio 子进程）
- **LLM**：ChatOpenAI → DeepSeek（可切换任意 OpenAI 兼容接口）
- **向量数据库**：ChromaDB（本地持久化）
- **Embedding**：sentence-transformers（all-MiniLM-L6-v2，本地运行）
- **浏览器**：Selenium + BeautifulSoup4 + SearXNG
- **数据库**：PyMySQL
- **Web**：FastAPI + WebSocket；前端 Vue 3 + Vite
- **包管理**：uv（Python 3.13）

## 项目结构

```
├── main.py                        # CLI 入口（argparse）
├── app/
│   ├── code_agent/
│   │   ├── agent/
│   │   │   ├── multi_agent.py     # ★ Planner → Executor → Verifier 状态图
│   │   │   ├── code_agent.py      # REPL 循环 + evals 非交互接口 run_single_task
│   │   │   └── prompts.py         # System / Planner / Verifier 提示词
│   │   ├── model/llm.py           # ChatOpenAI 工厂（build_llm / get_llm / set_llm）
│   │   ├── config.py              # 所有配置（从 .env 读）
│   │   ├── mcp_servers/           # 6 个 MCP Server（powershell / browser / mysql / vm / code_tools）
│   │   ├── rag/rag.py             # RAG MCP Server（ChromaDB）
│   │   ├── tools/
│   │   │   ├── file_tools.py      # FileManagementToolkit（限定在 workspace）
│   │   └── utils/mcp.py           # MCP 工具加载工厂
│   └── web/
│       ├── server.py              # FastAPI（WS + REST + 静态托管）
│       └── frontend/              # Vue3 + Vite（dist 已入库）
├── data/knowledge/                # 知识库源文件：35 条（7 个文件 × 每文件 5 条）
├── runtime/                       # ⚠️ gitignore：checkpoint / chroma_db / workspace / runs
├── evals/                         # 评估：任务的题集 / 评分器 / runner / RAG 基准
├── tests/                         # 36 个测试（单元 + 工具级）
├── docs/
│   ├── evidence/                  # 评估结果存档（只追加，含 README 说明口径）
│   ├── archive/                   # 历史归档（interview 素材 / optimized 计划 / zcode 计划）
│   └── handover.md                # 交接文档
├── AGENTS.md                      # AI 助手约定与已知坑
└── .gitee.yml                     # CI（跑 pytest）
```

## 数量与来源对照（每个数字都能复核）

| 数字 | 值 | 复核命令 |
|---|---|---|
| 测试数 | 59 | `uv run python -m pytest tests/ -q` |
| 评估题数 | 30 | `uv run python -c "from evals.tasks import TASKS; print(len(TASKS))"` |
| 知识库条目 | 35（7 文件 × 5 条） | `Get-ChildItem data/knowledge -Recurse -File` |
| MCP 工具数 | 32（含 7 个文件工具） | 运行 `uv run python main.py`，看日志 `共加载 N 个工具` |
| 评估指标 | 见上表 | `docs/evidence/*.json` 的 `overall` / `pass_rate` 字段 |

## License

MIT
