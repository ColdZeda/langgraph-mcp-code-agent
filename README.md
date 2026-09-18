# Code Agent

基于 **LangGraph + MCP 协议** 的多 Agent 编程助手（**Planner → Executor → Verifier** 三阶段 StateGraph 协作，Verifier 只读验收 + 失败打回）。通过 MCP (Model Context Protocol) stdio 子进程统一编排 6 类工具：PowerShell 终端、Selenium 浏览器、MySQL 数据库、WSL2 虚拟机、本地 RAG 知识库、代码分析工具。提供**命令行**与**本地 Web UI** 两种使用方式。

## 架构

```
main.py (CLI REPL)                app/web/server.py (FastAPI Web UI, 端口 8000)
       └────────────┬──────────────────────┘
                      ▼
   多 Agent 协作图 (multi_agent.py, LangGraph StateGraph)
   Planner(纯 LLM 规划) → Executor(ReAct 全量工具) → Verifier(只读白名单验收, FAIL 打回 ≤2 轮)
                      ├── LLM: DeepSeek (OpenAI 兼容接口，Web UI 内可热切换)
                      ├── Memory: FileCheckpointSaver (自定义 Checkpoint 持久化)
                      └── Tools (MCP stdio 子进程)
                          ├── PowerShell 终端  → subprocess 调用 powershell -Command
                          ├── 浏览器           → Selenium Edge + SearXNG 私密搜索
                          ├── MySQL            → PyMySQL (Docker 沙盒)
                          ├── WSL2 虚拟机      → wsl.exe 桥接 Ubuntu + 沙箱安全
                          ├── RAG 知识库       → ChromaDB + sentence-transformers (CRUD 闭环)
                          └── 代码分析         → AST 解析 / diff 生成 / 项目结构扫描
```

## Web UI（推荐体验方式）

```bash
uv run uvicorn app.web.server:app --port 8000
# 浏览器打开 http://localhost:8000
```

- 聊天界面：任务结果结构化展示——Planner 计划、工具调用轨迹（可折叠）、Verifier 验收徽章（PASS/FAIL + 打回次数）、token/耗时统计
- 会话列表：自动展示历史 checkpoint 会话；新会话一键开启
- 模型设置：界面内切换模型 / API 地址 / Key（热切换，立即生效；设置只存本机 `runtime/web-settings.json`，不进仓库）

> 前端（Vue 3 + Vite）源码在 `app/web/frontend/`，构建产物 `dist/` 已入库——不装 Node 也能直接运行；
> 改前端后 `cd app/web/frontend && npm install && npm run build` 重新构建。

## 快速开始

### 环境要求

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) 包管理器
- Windows + Edge 浏览器 (浏览器工具)
- WSL2 Ubuntu (虚拟机工具，可选)
- Docker (MySQL 沙盒 + SearXNG 搜索，可选)

### 安装

```bash
git clone https://gitee.com/wdnmded/code_agent_raw.git
cd code_agent_raw
uv sync
```

### 配置

复制 `.env.example` 为 `.env`，填入 API Key：

```env
MODEL_API_KEY=你的API密钥
# 可选: MODEL_NAME=deepseek-v4-flash
# 可选: MODEL_BASE_URL=https://api.deepseek.com
```

### 运行（命令行）

```bash
# 默认启动（自动生成会话 ID）
uv run python main.py

# 指定会话 ID（下次用同一个 ID 继续对话）
uv run python main.py --thread-id my-session

# 调试模式（查看详细日志）
uv run python main.py --debug
```

退出: `exit` / `quit` / `q` / `退出` / `bye`

### 启动依赖服务（可选）

```bash
# SearXNG 私密搜索
docker start searxng

# MySQL 沙盒 (WSL 内)
wsl -d Ubuntu -- bash -c "cd /home/leprite/mysql && docker compose up -d"

# Nginx (WSL 内)
wsl -d Ubuntu -- bash -c "cd /home/leprite/nginx && docker compose up -d"
```

## 功能

| 工具 | 能力 |
|------|------|
| 🖥️ PowerShell | subprocess 执行 Windows 命令、进程管理（危险命令拦截） |
| 🌐 浏览器 | Selenium + SearXNG 网页搜索 |
| 🗄️ MySQL | 建表、CRUD、查询（防 SQL 注入：参数化 + 标识符转义） |
| 🐧 WSL2 VM | Linux 命令执行、文件部署（危险命令拦截 + 超时） |
| 📚 RAG | 知识库 CRUD 闭环（ChromaDB 向量检索 + 本地 embedding） |
| 🔍 代码分析 | AST 解析、diff 生成、项目结构扫描、文件片段读取 |

## 评估体系

30 题端到端评估（`evals/`），覆盖 6 个能力维度：

- **tool_selection**（工具选择）/ **task_completion**（任务完成）/ **multi_step**（多步推理）
- **cross_tool**（跨工具协作）/ **error_recovery**（错误恢复）/ **safety**（安全）

特性：每题含明文对话存档（失败可定位到具体一步）、token 用量统计、断点续跑（按 run-id）。
指标演进：baseline **0.983**（30/30 全过）→ optimized 单 Agent **1.0** → 多 Agent 架构 **0.967**，
详见 `docs/evidence/evals-baseline-report.md` 与 `docs/evidence/` 各存档 JSON。

```bash
# 运行全部 30 题
uv run python evals/run_e2e.py --all --run-id baseline

# 运行单题（新 run-id，避免覆盖已有结果）
uv run python evals/run_e2e.py --task E001 --run-id single-test
```

## 技术栈

- **Agent 框架**: LangGraph (ReAct Agent)
- **MCP 适配**: langchain-mcp-adapters + FastMCP
- **LLM**: ChatOpenAI → DeepSeek (可通过 .env 切换到百炼/通义千问等兼容 API)
- **向量数据库**: ChromaDB (本地持久化)
- **Embedding**: sentence-transformers (all-MiniLM-L6-v2, 本地运行)
- **浏览器**: Selenium + BeautifulSoup4 + SearXNG
- **数据库**: PyMySQL
- **包管理**: uv (Python 3.13)

## 项目结构

```
├── main.py                  # CLI 入口 (argparse)
├── app/code_agent/
│   ├── agent/               # 多 Agent 核心 (multi_agent.py: Planner→Executor→Verifier) + Prompt 管理
│   ├── config.py            # 配置 (环境变量 + 默认值)
│   ├── model/llm.py         # LLM 工厂 (build_llm / get_llm / set_llm，支持运行期热切换)
│   ├── mcp_servers/         # MCP Server (工具实现)
│   │   ├── powershell_tools.py
│   │   ├── browser_tools.py
│   │   ├── mysql_tools.py
│   │   ├── vm.py
│   │   └── code_tools.py
│   ├── tools/               # 工具辅助
│   │   ├── file_tools.py    # FileManagementToolkit
│   │   └── file_saver.py    # FileCheckpointSaver
│   ├── rag/rag.py           # RAG 知识库 MCP Server
│   ├── utils/mcp.py         # MCP 工具加载工厂
│   └── web/                 # FastAPI Web UI (server.py + frontend/ Vue3)
├── data/knowledge/          # 知识库原文 (txt, real_knowledge 真知识 + distractors 干扰项)
├── runtime/                 # checkpoint / chroma / workspace / web-settings.json
├── evals/                   # 端到端评估 (30 题 / 6 能力维度 + 对话存档 + rag_bench)
├── tests/                   # 单元测试 (24 个) + 工具级测试 (12 个)
├── docs/                    # 项目文档 (handover / optimized 计划 / evidence 存档 / interview 素材)
└── .gitee.yml               # CI 流水线
```

## License

MIT
