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

## 阶段收尾清单（**不做完不算阶段完成**）

> 为什么要有这一节：活文档（本文件 / `docs/handover.md` / `README.md`）描述的是"现在是什么样"，
> 每改一次代码就可能失效一处。**过期比缺失更危险** —— 缺失可以读代码补，过期会让下一个会话照着错的做。
> （教训：阶段 2 改了容器与 CI，却只更新了 README，本节列出的 AGENTS.md 段落整段过期。）

| # | 收尾动作 |
|---|---|
| 1 | 刷新本文件「当前进度」：已完成阶段、已知遗留 |
| 2 | 核对并修改本阶段**影响到**的段落：环境与工具链 / 常用命令 / 代码地图 / 数字来源速查 |
| 3 | 刷新 `docs/handover.md` 的「当前状态快照」与「当前进度」 |
| 4 | 改掉 `README.md` 里受影响的数字与章节 |
| 5 | 在 `program-fix第八版/讨论结论汇总.md` 追加本阶段「执行期订正记录」（编号续上）+「验证记录」（正反两向证据） |
| 6 | **自查（机械可查，别靠自觉）**：`git grep` 本阶段改动的关键名词（旧工具名 / 旧容器名 / 旧服务名 / 端口 / 测试数 / 覆盖率 / 旧路径），命中的**活文档**必须改对；只追加类文件（提交信息 / 测试 / 订正记录 / 归档目录）不算 |
| 7 | `uv run python -m pytest tests/ -q` + `uv run ruff check .` + `uv run ruff format --check .` 全过 |
| 8 | `git add` → `commit`（信息里带验收数字）→ `git push origin master` |

## 常用命令

| 用途 | 命令 |
|---|---|
| 装依赖 | `uv sync` |
| 跑 Agent（REPL） | `uv run python main.py` |
| 指定会话 | `uv run python main.py --thread-id x` |
| 开新会话 | `uv run python main.py --new-session` |
| 起 Web UI | `uv run uvicorn app.web.server:app --port 8000` |
| 单元 + 工具级测试 | `uv run python -m pytest tests/ -v`（156 个） |
| 全量评估（30 题） | `uv run python evals/run_e2e.py --all --run-id <name>` |
| 单题评估 | `uv run python evals/run_e2e.py --task E011 --run-id <name>` |
| RAG 基准（含分块/精排指标） | `uv run python evals/rag_bench.py` |
| 重建前端 | `cd app/web/frontend && npm run build` |

> ⚠️ `pytest` **不能**跑评估（不认 `--task`）；评估一律用 `evals/run_e2e.py`。
> ⚠️ CI（`.gitee.yml`）从阶段 2 起跑三步：`ruff check .` → `ruff format --check .` → `pytest tests/ -v`。

## 代码地图（精简）

```
main.py                          CLI 入口（argparse）
app/code_agent/
├── agent/multi_agent.py         ★ 状态图：route_node / planner_node / executor_node / verifier_node
│                                  + _route_decide / after_executor / decide_after_verify
│                                  + READONLY_TOOL_NAMES（Verifier 只读白名单）+ build_graph(mode=…)
├── agent/context.py             上下文工程：工具结果外置(T4.1) / 对话压实(T4.2) / token 预算(T4.3)
├── agent/memory.py              分层记忆读写：自动注入(T4.4②) / 自动沉淀(T4.4③)
├── agent/code_agent.py          REPL 循环 run_agent() + evals 入口 run_single_task()
├── agent/prompts.py             SYSTEM_PROMPT_TEMPLATE（Plan→Execute→Verify 三步法）+ PROMPT_CONTEXT
├── model/llm.py                 LLMRegistry：get_llm(role) / chain(role) / invoke_with_fallback
│                                  + build_llm / set_llm（热切换后需重建 agent）
├── config.py                    所有配置 + setup_logging（stderr）
├── mcp_servers/                 powershell(2) / browser(1) / mysql(10) / vm(4) / code_tools(4)
├── rag/rag.py                   RAG MCP Server（4 个工具，**薄壳**）
├── rag/store.py                 知识库核心：分块索引 / 粗召回 / CrossEncoder 精排（全懒加载）
├── rag/chunking.py              纯分块函数（不 import torch，单测毫秒级）
├── tools/file_tools.py          FileManagementToolkit(root_dir=WORKSPACE_DIR) → 7 个工具
├── utils/mcp.py                 load_mcp_tools（工厂；client 无需关闭，见「已知坑」）
├── utils/tool_cache.py          只读工具结果缓存（Redis；挂了自动降级）
└── utils/tool_wrap.py           工具包装：结果外置 + 结果缓存（MCP 就地改 / 同步工具换代理）
config/models.json               模型注册表 + 角色分配 + 降级链（**进版本控制**，不要放 runtime/）
app/web/server.py                FastAPI：WS /ws/chat + REST（sessions/settings/models）+ 静态托管 dist
evals/                           tasks.py(30 题) / verifiers.py(评分器) / run_e2e.py(脚本) / rag_bench.py / compare.py
tests/                           156 个测试（config / prompts / mysql_safe_ident / mysql_readonly /
                                 multi_agent / checkpoint / route / llm_registry / mcp_tool_lifecycle /
                                 tool_level / context / memory / tool_cache / tool_wrap / rag_chunking）
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
| **执行模式** | `single` / `multi` / `auto`：`auto` 先由 `route_node` 判复杂度（写进 `state["route"]`），simple 只跑 Executor、complex 走完整三阶段；CLI `--mode`、evals `--mode`、Web UI 下拉框都能选 |
| **模型按角色配** | 注册表在 `config/models.json`（roles + fallback）；`get_llm(role)`，默认 executor；`--role-models "executor=x"` 可临时覆盖；结果 JSON 记录 `role_models`。⚠️ **测试里必须同时 patch `ma.get_llm` 与 `ma.registry`**，否则 planner 会真的调模型（实测让 pytest 从 8s 变 104s） |

### 环境与工具链

- **MCP 工具的接口形状**：MCP 适配层生成的是 `StructuredTool(coroutine=…, response_format=…)`，
  **没有 `func`** → 想包装工具**不能用 `tool._run`**（对 32 个 MCP 工具都会 raise）；
  FileManagementToolkit 的 7 个工具则**只有同步 `_run`**。两条路径都要处理。
- **运行时目录**（阶段 1 已修）：`config.py` 现在会创建 `RUNTIME_DIR / WORKSPACE_DIR /
  CHECKPOINT_DIR / CHROMA_DIR / RUNS_DIR`。此前全新 clone 下 `tests/test_config.py`
  会因为目录不存在而失败（本机通过只是因为有残留）。
- **改造前的历史不在 `master` 上**：`master` 的**地基** `8d0ab78`（"init: 导入改造前基线"）是**单提交重建**的，
  它下面没有历史；改造期的提交都直接追加在它上面（`git log --oneline` 看得到）。
  要找**改造前**的东西必须去 `refs/remotes/raw-origin/*`（旧仓库 master / phase1..phase5）→ 考古要用 **`git log --all -S '...'`**。
- **依赖服务怎么起**（阶段 2 统一后，实测）：
  - `agent-mysql` / `searxng` / `redis-stack-server` 由**仓库根的 `docker-compose.yml`** 管理
    （`name: code-agent-deps`）；**`my-nginx` 仍归 WSL 里的 `~/nginx/docker-compose.yaml`**
    （它的挂载源是 WSL 路径，搬到 Windows 侧 compose 会**静默挂空目录**）；
  - 一键脚本：`./scripts/start-deps.ps1` / `./scripts/stop-deps.ps1`；
  - **4 个容器都是 `restart: unless-stopped`** → 打开 Docker Desktop（= 启动引擎）会自动拉起；
    被手动 stop 过的除外，那时用一键脚本；
  - MySQL 数据在**命名卷** `mysql-data`；首次初始化会执行 `scripts/mysql-init/*.sql`
    （里面建只读账号 `agent_readonly`）。
- ⚠️ **WSL 那份 nginx compose 里不要用「单文件挂载」**（2026-09-19 实测踩坑）：
  `./conf/nginx.conf:/etc/nginx/nginx.conf` 这类**单文件跨发行版挂载**在 Docker Desktop 上会以
  `error mounting ".../docker-desktop-bind-mounts/Ubuntu/<hash>" ... not a directory:
  Are you trying to mount a directory onto a file (or vice-versa)?` 失败，容器起不来。
  **故障还会固化**：暂存位按源路径 hash 命名，失败一次就会在虚拟机里留下一个目录占位，
  之后每次启动都复用它（**重启 Docker、重启容器都没用**）→ 必须
  `docker compose up -d --force-recreate` 重建容器才能绕开。
  **解法：只挂目录**；自定义配置写进 `conf/conf.d/*.conf`
  （镜像自带的 `nginx.conf` 里本来就有 `include /etc/nginx/conf.d/*.conf;`）。
  实测：改完 `my-nginx` 正常 Up、`curl -I http://localhost/` 返回 200。
- **`scripts/start-deps.ps1` 会先探一次 `~/nginx`**：不存在就**打印明确提示并跳过** nginx 那步
  （而不是抛一段 WSL 报错）—— 因为 `~/nginx/` 只存在于 WSL，仓库里没有副本。
- **搜索已不依赖浏览器**（阶段 2）：`browser_tools.py` 只调 SearXNG 的 JSON API，
  文件名是历史遗留；**Selenium / Edge / msedgedriver / 调试端口都不再需要**。

### 上下文工程与分层记忆（阶段 4，四条都是实测结论）

- **工具结果外置**（`utils/tool_wrap.py` + `agent/context.py`）：
  阈值 `EXTERNALIZE_THRESHOLD=6000` 字符或 150 行 → 落盘 `runtime/tool_results/`，上下文里只留预览 + 路径。
  ⚠️ **`read_file_range` / `read_file` 在豁免名单里，不许外置** ——
  实测同一道"读全文并总结"的题：外置后模型看不见内容，改用分段读绕过去，
  **12 次工具调用 / 25 步，token 从 17,361 涨到 127,071（7.3 倍）**。
  另外**读 `tool_results/` 里的文件时绝不再外置**（同内容同 hash → 又指向同一个文件 = 死循环）。
- **包装工具的两条路径**（踩过两次，别重蹈）：
  MCP 工具的 `coroutine` 是 **StructuredTool 声明过的字段**，可以就地替换；
  但 FileManagementToolkit 的工具是 **pydantic 模型**，往实例上塞 `coroutine` 会直接报
  `ValueError: "CopyFileTool" object has no field "coroutine"` → 这类工具要**换成同名 StructuredTool 代理**。
  所以 `wrap_tools()` **必须接返回值**（`tools = wrap_tools(tools, cache)`），不是原地改。
- **缓存命中要补回二元组**：`content_and_artifact` 的工具返回裸字符串会被 LangChain 拒绝，
  所以命中缓存时返回 `(文本, None)`。
- **只读工具缓存**（`utils/tool_cache.py`）：只缓存只读工具，**且默认不收 `mysql_execute_query`**
  （它只读，但返回的是会变的数据 —— "先写再查"的题会拿到过期结果）。
  任何写操作执行后**清空本会话缓存**；Redis 不可用则静默降级。
  评估时每个任务一个独立 scope，避免跨任务串味。
- **对话压实 / token 预算**（`agent/context.py`）：
  历史估算超 `COMPACT_THRESHOLD=6000` token → 最老的一段压成四段式摘要（**摘要失败就原样保留**，
  省 token 不能把历史弄丢）；`NODE_TOKEN_BUDGET=30000` 剪枝、`TASK_TOKEN_BUDGET=200000` 硬终止。
  ⚠️ 任务级上限在 **executor 的 ReAct 循环内部逐步判**（不只在节点入口判）——
  只在入口判的话，一次"读大文件 + 反复重读"能在**单个节点调用**里烧掉十几万 token 而不触发。
  粒度是"每步一判"，所以实际可能在"上限 + 一步的成本"处才停下（实测设为 12000 时在 26,456 停下并报告）。
- **RAG 分块 + 精排**：collection 换名 `terminal_knowledge_v2`（**新旧粒度不能混在一个 collection**，
  否则检索更差）；id 是 `f"{source}#{块序号}"`，元数据 `{source, chunk, mtime}`，增量按 `where={"source":…}` 删旧。
  reranker 从**本地路径**加载（`CODE_AGENT_RERANKER_PATH`），**路径不存在就降级为纯向量召回，不联网**。
  实测：top-1（文件粒度）0.6 → **0.9**、recall 0.4 → **1.0**、稳态延迟 13.2ms → **81ms**。
  ⚠️ **这些是阶段 4 自测的临时数字**：正式三指标 / 归档 / 前后对比表归**阶段 6**（订正 #23）。
  ⚠️ **本机的 reranker 是从 hf-mirror 下的**（HuggingFace 直连超时、ModelScope 没有这个模型），
  位置 `../embedding-model/cross-encoder/ms-marco-MiniLM-L-6-v2`，**在仓库外、不进版本控制**。
- **RAG 的定位（别加戏）**：它是**语义记忆** —— 记录**使用过程中积累的经验/习惯**（自学习闭环的存储端），
  **不是企业知识库问答**。不要为了"指标好看"去扩知识库或换模型（用户已记进 `与定位冲突的事.md` 第 9 条）。
- **三条入口都要接工具包装层**（阶段 4 收尾补漏）：CLI（`code_agent.run_agent`）、
  evals（`run_single_task`）、**Web（`app/web/server.py` 的 `AgentRuntime.load()`）**。
  漏掉任何一条，那条入口就既不做结果外置、也不走缓存 —— 阶段 5 的 HITL/权限层同理。
  测试守着：`tests/test_tool_wrap.py` 的 `test_web_runtime_wraps_tools`。
- **自动注入 / 自动沉淀**（`agent/memory.py`）：在 **Agent 进程内**直接调 `rag/store.py`，**不走 MCP** ——
  MCP 工具每次调用都要新起 python 子进程重新 import chromadb + torch，延迟从毫秒级变秒级。
  ⚠️ **评估时 `run_single_task` 强制关掉自动沉淀**（`auto_deposit=False`），
  否则评测过程产生的经验会写进知识库、改写后续题目的检索结果。
- **`import app.code_agent.rag.store` 不加载模型**（全懒加载）：单测里碰它不会去 load torch。
  测试环境由 `tests/conftest.py` 统一关掉自动注入 / 自动沉淀 / 工具缓存。

### evals 相关

- **重跑前必须清残留**：`runtime/checkpoints.db`（阶段 1 起的 SQLite 记忆）、`runtime/chroma_db/`、`data/knowledge/` **根目录**
  （Agent 自学习写入的）、MySQL `agent_test` 表、WSL uploads（保留 `.gitkeep`）；
  知识库预置是 **35 条（7 个文件 × 每文件 5 条）**，`real_knowledge/` 4 个 + `distractors/` 3 个。
- 单题重跑用**新 run-id**（避免覆盖），并先删对应的 checkpoint。
- `runtime/runs/` 被 gitignore；**正式结果才复制到 `docs/evidence/`** 纳入版本控制。
  ⚠️ 2026-09 用户把**改造前**那批旧存档（旧模型 + 软口径）**移出了仓库**，`docs/evidence/` 现在是空的；
  备份在 `E:\agentstart\work\backup\1new\backup\old-data\docs\evidence\`（13 个文件），
  git 历史里也有（如 `git show 1ea2687^:docs/evidence/baseline-final.json` —— `1ea2687` 是**删除**这批存档的提交，
  所以要用它的父提交 `^`；拿删除之后的提交去 show 只会得到 `path ... does not exist in ...`）。
  阶段 6 会产出新口径的结果。
- 计分口径偏软：`pass_rate` 把 `score >= 0.5` 记为通过，而部分 verifier 会给 0.5 部分分。
- **MCP 工具没有"需要关闭的 client"**（实测，langchain-mcp-adapters 0.1.1）：
  `MultiServerMCPClient.get_tools()` 的 docstring 明写
  *"a new session will be created for each tool call"* → 每次工具调用**自建并自关**一个会话
  （stdio 子进程同理），没有长期存活的连接；而 `MultiServerMCPClient.__aexit__` 是**普通函数**，
  调用即抛 `NotImplementedError`（不支持当上下文管理器）。
  → `code_agent.py` 里原先那段 `await _client.__aexit__(...)` **一直是空操作**（被 `except Exception` 吞掉），
  现已删除；`utils/mcp.py` 的 `load_mcp_tools_managed` 也一并删除（它的前提是错的）。
  回归测试：`tests/test_mcp_tool_lifecycle.py`。

### 仓库整理

- `runtime/` 与 `.temp/` 都是 gitignore 的运行时目录 → **做全仓扫描类操作必须排除**（否则扫到生成物）。
- `docs/` 结构（2026-08-31 整理后）：`handover.md` + `evidence/`（存档，只追加）+ `archive/`（历史素材）。
  当前 `evidence/` 与 `archive/` **内容已被移出仓库**（用户决定，备份在 `backup/1new/backup/old-data/docs/`），
  只剩空目录；阶段 6 重做评估后会重新往里写结果。

## 当前进度（2026-09-21 更新）

**已经走完的**：
```
prototype（教学原型）→ baseline（0.983）→ optimized（单 Agent 1.0 / 多 Agent 0.967）→ Web UI ✅
```

**当前阶段**：**改造期**。方案文档在**仓库外**：`E:\agentstart\上班\work-content\program-fix第八版\`
（**第八版 = 第七版 + 执行期实测订正**；第七版是冻结原档，第六版是原始底稿）。
阶段 0（文档清洗与仓库整理）、阶段 1（修 P0 缺陷）、阶段 2（降复杂度与容器化）、
阶段 3（执行模式与模型配置）、**阶段 4（上下文工程与分层记忆）**已完成；
后续阶段按顺序执行，**每阶段做完停下汇报 + 提交推送**。

> 🚦 **下一步是阶段 5（HITL 与安全加固）—— 开工先读这三份，别凭记忆上手**：
> 1. `program-fix第八版\阶段5_开工包.md` ← **唯一入口**（子任务 / 硬约束 / 代码锚点 / 验收清单）
> 2. `program-fix第八版\阶段5_权限档位候选表.md`（32 个工具的档位归类，**已审核通过**）
> 3. `program-fix第八版\讨论结论汇总.md` 的 **§11.3**（B1–B7 / C1 / C2 / D1–D4 决策与理由）
>
> 设计决策**已全部定完**，开工时只剩 **D4** 要问用户：权限模式的默认值存哪（全局 settings / 每会话）。

**已知遗留**：

1. **评估体系待重做**（口径偏软：14/30 题没有产物级断言）→ 阶段 6。
2. **前端构建产物**：`app/web/frontend/dist/` 必须入库；⚠️ 根 `.gitignore` 曾有裸 `dist/`
   会把新构建的哈希资源一并吞掉（已改为 `/dist/`）。改前端后必须 `npm run build` 并提交
   **新增与删除**的资源文件。
3. **Web 端节点级实时推送**（现在只在任务完成后一次性推送）→ 阶段 5 的 T5.6（用现有 WS，不引 SSE）。
4. **RAG 检索会把干扰项排到第一**：实测 10 道题里有 **4 道**的 top-1 落在 `distractors/`（故意写错的知识），
   正解来源 top-1 只有 0.6。测试集本身很小（35 块）+ CrossEncoder 偏词汇匹配，属于**已知局限**；
   阶段 6 重做评估时应把它作为"检索质量"的真实指标之一（别只看关键词命中）。
5. **`.coverage` 曾被误提交**（阶段 4 发现）：它是二进制覆盖率数据，不该进版本控制 ——
   已从索引移除并加进 `.gitignore`。

> ✅ **阶段 1 已修完的**（别再当成遗留）：E013 无限打回（`retry_count` 不自增）、
> checkpointer 未接线、`file_saver.py` 待删、全新 clone 下运行时目录缺失。
> 细节与正反两向证据见 `program-fix第八版/讨论结论汇总.md` 的「验证记录（阶段 1）」。

## 数字来源速查

| 数字 | 值 | 命令 |
|---|---|---|
| 测试数 | 156 | `uv run python -m pytest tests/ -q` |
| 测试覆盖率 | 68%（1451 语句 / 458 未覆盖） | `uv run python -m pytest tests/ -q`（addopts 自带 `--cov`） |
| 评估题数 | 30 | `uv run python -c "from evals.tasks import TASKS; print(len(TASKS))"` |
| MCP 工具数 | 25（+ 7 文件工具 = 32） | `Select-String -Path app/code_agent/mcp_servers/*.py,app/code_agent/rag/rag.py -Pattern "@mcp\.tool"` |
| 知识库条目 | 35（7 文件 × 5 条）；分块后 = 35 块 | `Get-ChildItem data/knowledge -Recurse -File` |
| RAG 检索指标（**阶段 4 临时数**，阶段 6 重测） | top1(文件粒度) 0.9 / top3 1.0 / recall 1.0 / 稳态 81ms | `uv run python evals/rag_bench.py` |
| 评估指标（改造前旧口径） | 见 README 表格 | 旧存档已移出仓库 → 备份 `backup/1new/backup/old-data/docs/evidence/` 或 `git show 1ea2687^:docs/evidence/<file>` |
| 跟踪文件数 | `git ls-files` 计数 | `git ls-files \| Measure-Object` |
