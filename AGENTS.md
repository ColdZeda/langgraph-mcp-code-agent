# AI Agent Test — LangGraph + MCP Code Agent

> **给谁看**：**AI 编码助手**（Claude / Cursor / Copilot / 其他）。
> 人类读者看 [`README.md`](README.md)；接手人的上下文与待办看 [`docs/handover.md`](docs/handover.md)。
> 本文件只负责四件事：**约定 / 命令 / 已知坑 / 当前进度**（架构与功能描述不在这里）。

## 项目一句话

Python 3.13 的本地多 Agent 编程助手：LangGraph StateGraph（Planner → Executor → Verifier）
+ 6 个自建 MCP Server（stdio 子进程，25 个工具）+ FileManagementToolkit（7 个文件工具），
双入口（CLI `main.py` / Web UI `app/web/server.py`），配套 30 题评估体系。

## 必须遵守的约定

| # | 约定 |
|---|---|
| 1 | 包管理用 **uv**：`uv sync` / `uv add` / `uv run python`，**绝不用 pip** |
| 2 | **MCP server 的日志必须走 stderr**（stdout 是 JSON-RPC 通道，任何 print 都会污染协议） |
| 3 | 配置集中在 `app/code_agent/config.py`（`.env` + 默认值）；新增配置项要同步 `.env.example` |
| 4 | 行尾交给 `.gitattributes`（`* text=auto`）：**索引里已经是 LF**，不需要手动转换 |
| 5 | 用户可见字符串用**中文** |
| 6 | 改动**前后都要留证据**（测试输出 / 结果文件 / 复现命令） |
| 7 | **不许为了让测试通过而降低断言强度** |
| 8 | 文档与代码不符时，**以代码为准**，并在回复里明确指出 |
| 9 | 文档里的每个**行号 / 文件名 / 数字都必须现场核对**，禁止凭印象写 |
| 10 | 新增依赖必须 `uv add`，并把"装什么、为什么装"写进对应文档 |
| 11 | 改前端后必须 `npm run build` 并提交 `dist/`（含被删除的旧 hash 文件） |

## 常用命令

| 用途 | 命令 |
|---|---|
| 装依赖 | `uv sync` |
| 跑 Agent（REPL） | `uv run python main.py` |
| 指定会话 | `uv run python main.py --thread-id x` |
| 开新会话 | `uv run python main.py --new-session` |
| 起 Web UI | `uv run uvicorn app.web.server:app --port 8000` |
| 单元 + 工具级测试 | `uv run python -m pytest tests/ -v`（59 个） |
| 全量评估（30 题） | `uv run python evals/run_e2e.py --all --run-id <name>` |
| 单题评估 | `uv run python evals/run_e2e.py --task E011 --run-id <name>` |
| RAG 基准 | `uv run python evals/rag_bench.py` |
| 重建前端 | `cd app/web/frontend && npm run build` |

> ⚠️ `pytest` **不能**跑评估（不认 `--task`）；评估一律用 `evals/run_e2e.py`。
> ⚠️ CI（`.gitee.yml`）现在跑的是 `pytest tests/ evals/`（历史写法）；`evals/` 下已无测试文件，等价于只跑 `tests/`。

## 代码地图（精简）

```
main.py                          CLI 入口（argparse）
app/code_agent/
├── agent/multi_agent.py         ★ 状态图：planner_node / executor_node / verifier_node / decide_after_verify
│                                  + READONLY_TOOL_NAMES（Verifier 只读白名单）+ build_graph
├── agent/code_agent.py          REPL 循环 run_agent() + evals 入口 run_single_task()
├── agent/prompts.py             SYSTEM_PROMPT_TEMPLATE（Plan→Execute→Verify 三步法）+ PROMPT_CONTEXT
├── model/llm.py                 模块级单例：build_llm / get_llm / set_llm（热切换后需重建 agent）
├── config.py                    所有配置 + setup_logging（stderr）
├── mcp_servers/                 powershell(2) / browser(1) / mysql(10) / vm(4) / code_tools(4)
├── rag/rag.py                   RAG MCP Server（4 个工具；import 时建库 + 灌知识）
├── tools/file_tools.py          FileManagementToolkit(root_dir=WORKSPACE_DIR) → 7 个工具
└── utils/mcp.py                 load_mcp_tools / load_mcp_tools_managed（工厂）
app/web/server.py                FastAPI：WS /ws/chat + REST（sessions/settings）+ 静态托管 dist
evals/                           tasks.py(30 题) / verifiers.py(评分器) / run_e2e.py(脚本) / rag_bench.py / compare.py
tests/                           59 个测试（config / prompts / mysql_safe_ident / mysql_readonly / multi_agent / checkpoint / cleanup / tool_level）
```

## 已知坑（务必先看）

### 架构层面的关键事实（阶段 1 已修，别按老印象理解）

| 项 | 现状 |
|---|---|
| **跨轮记忆** | ✅ 已接线：`SqliteSaver`（`AsyncSqliteSaver`）+ `runtime/checkpoints.db`；`AgentState.messages` 用 `add_messages` reducer 累积；每轮由 `run_multi_agent` 在图跑完后追加一对 (任务, 回复) |
| **thread_id** | 三条路径**都必须传**：CLI（`run_agent`）、Web（`server.py`）、evals（`run_single_task` ← `run_e2e.py` 的 `eval-<id>`）。漏传会直接报错 |
| CLI 会话 ID | 默认取 `.env` 的 `CODE_AGENT_THREAD_ID`（默认 `default`）→ 关掉再打开会续上次对话；`--new-session` 开新会话 |
| **Verifier 打回** | ✅ 已修：`retry_count` 在 `executor_node` 里「是重跑才 +1」→ 最多打回 `MAX_RETRY`(2) 次，Executor 共跑 `MAX_RETRY+1` 次 |
| `file_saver.py` | ✅ **已删除**（连同 `tests/test_file_saver.py`）；它曾是全仓唯一非法 UTF-8 的 `.py` |

### 环境与工具链

- **MCP 工具的接口形状**：MCP 适配层生成的是 `StructuredTool(coroutine=…, response_format=…)`，
  **没有 `func`** → 想包装工具**不能用 `tool._run`**（对 32 个 MCP 工具都会 raise）；
  FileManagementToolkit 的 7 个工具则**只有同步 `_run`**。两条路径都要处理。
- **全新 clone 下 `pytest` 会失败**：`tests/test_config.py:40-41` 断言 `CHECKPOINT_DIR` / `CHROMA_DIR` 存在，
  但 `config.py` 不创建目录（本机通过只是因为有残留）→ 已在改造清单里修。
- **`git log` 查不到历史**：`master` 是**单提交重建**的基线；完整历史在 `refs/remotes/raw-origin/*`
  （旧仓库 master / phase1..phase5）→ 考古要用 **`git log --all -S '...'`**。
- **容器启动方式不一样**（实测）：`agent-mysql` / `my-nginx` 是 **WSL 里的 compose**；
  `searxng` / `redis-stack-server` 是 **`docker run`**（只能 `docker start`）。
  四个的 restart 策略都是 `no` → Docker Desktop 重启后**不会自动起**。
- **`my-nginx` 在 WSL compose 里的服务名是 `lima`**（不是 `nginx`）：不带服务名的
  `docker compose up -d` 正常；带服务名的子命令要用 `lima`。
- **Edge 调试端口**用 9333（9222 曾被 Windows 端口排除范围占用）。

### evals 相关

- **重跑前必须清残留**：`runtime/checkpoint/`、`runtime/chroma_db/`、`data/knowledge/` **根目录**
  （Agent 自学习写入的）、MySQL `agent_test` 表、WSL uploads（保留 `.gitkeep`）；
  知识库预置是 **35 条（7 个文件 × 每文件 5 条）**，`real_knowledge/` 4 个 + `distractors/` 3 个。
- 单题重跑用**新 run-id**（避免覆盖），并先删对应的 checkpoint。
- `runtime/runs/` 被 gitignore；**正式结果才复制到 `docs/evidence/`** 纳入版本控制。
- 计分口径偏软：`pass_rate` 把 `score >= 0.5` 记为通过，而部分 verifier 会给 0.5 部分分。
- 超时取消时的资源清理（`code_agent.py` 的 `finally`，已修）：
  实测校正 —— **单次取消不会漏关**（取消在进入清理块之前就投递完了，6/6 都能关掉）；
  危险的是「**清理期间又收到一次取消**」：只 `except Exception` 会让剩余 client 全部关不掉，
  而只改成 `except BaseException` 也不行（吞掉一次取消后，后续每个 await 都会立刻再抛，实测 0/6）。
  现在的写法是把清理放进**独立任务 + `asyncio.shield`**，取消只能打断"等待"、打不断"清理"。

### 仓库整理

- `runtime/` 与 `.temp/` 都是 gitignore 的运行时目录 → **做全仓扫描类操作必须排除**（否则扫到生成物）。
- `docs/` 结构（2026-08-31 整理后）：`handover.md` + `evidence/`（存档，只追加）+ `archive/`（历史素材）。

## 当前进度（2026-08-31 更新）

**已经走完的**：
```
prototype（教学原型）→ baseline（0.983）→ optimized（单 Agent 1.0 / 多 Agent 0.967）→ Web UI ✅
```

**当前阶段**：**改造期**。方案文档在**仓库外**：`E:\agentstart\上班\work-content\program-fix第八版\`
（**第八版 = 第七版 + 执行期实测订正**；第七版是冻结原档，第六版是原始底稿）。
阶段 0（文档清洗与仓库整理）已完成；阶段 1（修 P0 缺陷）进行中；
后续阶段按顺序执行，**每阶段做完停下汇报 + 提交推送**。

**已知遗留**：

1. **E013 偶发 timeout** —— 根因（`retry_count` 不自增 → 无限打回）**已在阶段 1 修复并验证**：
   把自增去掉后测试立刻以 `GraphRecursionError: Recursion limit of 100` 失败。
   evals 重做后该存档作废。
2. **评估体系待重做**（口径偏软：14/30 题没有产物级断言）→ 阶段 6。
3. **前端构建产物**：`app/web/frontend/dist/` 必须入库；⚠️ 根 `.gitignore` 曾有裸 `dist/`
   会把新构建的哈希资源一并吞掉（已改为 `/dist/`）。改前端后必须 `npm run build` 并提交
   **新增与删除**的资源文件。

## 数字来源速查

| 数字 | 值 | 命令 |
|---|---|---|
| 测试数 | 59 | `uv run python -m pytest tests/ -q` |
| 评估题数 | 30 | `uv run python -c "from evals.tasks import TASKS; print(len(TASKS))"` |
| MCP 工具数 | 25（+ 7 文件工具 = 32） | `Select-String -Path app/code_agent/mcp_servers/*.py,app/code_agent/rag/rag.py -Pattern "@mcp\.tool"` |
| 知识库条目 | 35（7 文件 × 5 条） | `Get-ChildItem data/knowledge -Recurse -File` |
| 评估指标 | 见 README 表格 | `docs/evidence/*.json` 的 `overall` / `pass_rate` |
| 跟踪文件数 | `git ls-files` 计数 | `git ls-files \| Measure-Object` |
