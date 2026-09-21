# AI Agent Test — LangGraph + MCP Code Agent

> **给谁看**：**AI 编码助手**（Claude / Cursor / Copilot / 其他）。
> 人类读者看 [`README.md`](README.md)；接手人的上下文与待办看 [`docs/handover.md`](docs/handover.md)。
> 本文件只负责四件事：**约定 / 命令 / 已知坑 / 当前进度**（架构与功能描述不在这里）。

## 项目一句话

Python 3.13 的本地多 Agent 编程助手：LangGraph StateGraph（Planner → Executor → Verifier）
+ 6 个自建 MCP Server（stdio 子进程，25 个工具）+ FileManagementToolkit（7 个文件工具），
双入口（CLI `main.py` / Web UI `app/web/server.py`）。
⚠️ 旧的 30 题评估体系已于**阶段 5 删除**（口径不可用），阶段 6 重建 —— 见下方「评估相关」。

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
| 12 | **命名是"有意两套"的，别去统一**：助手**自称** `novi`（`PROMPT_CONTEXT["name"]`）、**界面产品名** `Code Agent-novi`；而 `README.md` / 本文件 / `docs/handover.md` 里的**项目名保持 `Code Agent`**（用户 2026-09-21 决定：只改界面） |

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
| 指定权限模式 | `uv run python main.py --permission readonly`（readonly / confirm（默认）/ open） |
| 开新会话 | `uv run python main.py --new-session` |
| 起 Web UI | `uv run uvicorn app.web.server:app --port 8000` |
| 单元 + 工具级测试 | `uv run python -m pytest tests/ -v`（308 个） |
| MCP server 探针（排查"工具调不通"） | `uv run python scripts/probe_mcp_server.py rag query_rag --args '{"query":"MCP"}'` |
| RAG 基准（含分块/精排指标） | `uv run python evals/rag_bench.py` |
| 重建前端 | `cd app/web/frontend && npm run build` |

> ⚠️ **端到端评估（旧 30 题）已于阶段 5 删除** —— 阶段 6 重建题集与评分器之前，
> `evals/` 里只剩 `rag_bench.py`（RAG 检索基准）。别再去 `git grep run_e2e` 找命令，它已经不在了。
> ⚠️ CI（`.gitee.yml`）从阶段 2 起跑三步：`ruff check .` → `ruff format --check .` → `pytest tests/ -v`。

## 代码地图（精简）

```
main.py                          CLI 入口（argparse）
app/code_agent/
├── agent/multi_agent.py         ★ 状态图：route_node / planner_node / executor_node / verifier_node
│                                  + _route_decide / after_executor / decide_after_verify
│                                  + READONLY_TOOL_NAMES（Verifier 只读白名单）+ build_graph(mode=…)
├── agent/context.py             上下文工程：工具结果外置(T4.1) / 对话压实(T4.2) / token 预算(T4.3)
├── agent/memory.py              分层记忆读写：自动注入(T4.4②) / 自动沉淀(T4.4③，**进程内直调**)
├── agent/events.py              ★ 阶段 5：节点级进度事件（ContextVar sink；没绑就静默跳过）
├── agent/code_agent.py          REPL 循环 run_agent() + 非交互入口 run_single_task()（阶段 6 重建的评估脚本会用）
├── agent/prompts.py             SYSTEM_PROMPT_TEMPLATE（Plan→Execute→Verify 三步法）+ PROMPT_CONTEXT
├── model/llm.py                 LLMRegistry：get_llm(role) / chain(role) / invoke_with_fallback
│                                  + build_llm / set_llm（热切换后需重建 agent）
├── config.py                    所有配置 + setup_logging（stderr）
├── mcp_servers/                 powershell(2) / browser(1) / mysql(10) / vm(4) / code_tools(4)
├── rag/rag.py                   RAG MCP Server（4 个工具，**薄壳**；⚠️ 顶层那句 import 是修死锁的，别删）
├── rag/store.py                 知识库核心：分块索引 / 粗召回 / CrossEncoder 精排（**全懒加载**）
├── rag/chunking.py              纯分块函数（不 import torch，单测毫秒级）
├── tools/file_tools.py          FileManagementToolkit(root_dir=WORKSPACE_DIR) → 7 个工具
├── security/permissions.py      ★ 阶段 5：三档权限档位表（32 工具）+ 判定 + 人工确认闸门 + 审计
├── utils/mcp.py                 load_mcp_tools（工厂；client 无需关闭，见「已知坑」）
├── utils/tool_cache.py          只读工具结果缓存（Redis；挂了自动降级）
└── utils/tool_wrap.py           工具包装：**权限判定(第一句)** + 结果外置 + 结果缓存
config/models.json               模型注册表 + 角色分配 + 降级链（**进版本控制**，不要放 runtime/）
app/web/server.py                FastAPI：WS /ws/chat（含权限确认协议）+ REST + 静态托管 dist
app/web/frontend/src/            Vue3 源码：App.vue（执行/权限两个下拉框）+ store.js + components/
                                 （ChatView / ResultCard / SettingsPanel / **PermissionDialog**〔阶段 5〕）
evals/                           rag_bench.py（RAG 检索基准）—— 旧 30 题集已于阶段 5 删除，阶段 6 重建
scripts/                         start-deps.ps1 / stop-deps.ps1 / mysql-init/*.sql
                                 + **probe_mcp_server.py**（手工发 JSON-RPC 探某个 MCP server 到底回没回）
tests/                           308 个测试（config / prompts / mysql_safe_ident / mysql_readonly /
                                 multi_agent / checkpoint / route / llm_registry / mcp_tool_lifecycle /
                                 tool_level / context / memory / tool_cache / tool_wrap / rag_chunking /
                                 permissions / dangerous_commands / web_permission〔后三个是阶段 5 的〕）
```

## 已知坑（务必先看）

### 架构层面的关键事实（阶段 1 已修，别按老印象理解）

| 项 | 现状 |
|---|---|
| **跨轮记忆** | ✅ 已接线：`SqliteSaver`（`AsyncSqliteSaver`）+ `runtime/checkpoints.db`；`AgentState.messages` 用 `add_messages` reducer 累积；每轮由 `run_multi_agent` 在图跑完后追加一对 (任务, 回复) |
| **thread_id** | 三条路径**都必须传**：CLI（`run_agent`）、Web（`server.py`）、非交互入口（`run_single_task`）。漏传会直接报错 |
| CLI 会话 ID | 默认取 `.env` 的 `CODE_AGENT_THREAD_ID`（默认 `default`）→ 关掉再打开会续上次对话；`--new-session` 开新会话 |
| **Verifier 打回** | ✅ 已修：`retry_count` 在 `executor_node` 里「是重跑才 +1」→ 最多打回 `MAX_RETRY`(2) 次，Executor 共跑 `MAX_RETRY+1` 次 |
| `file_saver.py` | ✅ **已删除**（连同 `tests/test_file_saver.py`）；它曾是全仓唯一非法 UTF-8 的 `.py` |
| **执行模式** | `single` / `multi` / `auto`：`auto` 先由 `route_node` 判复杂度（写进 `state["route"]`），simple 只跑 Executor、complex 走完整三阶段；CLI `--mode` 与 Web UI 下拉框都能选 |
| **模型按角色配** | 注册表在 `config/models.json`（roles + fallback）；`get_llm(role)`，默认 executor；运行期改法有两个：Web UI 设置面板（`set_role_models`）与 `registry.override_from_spec()`（原 `evals --role-models`，那个 CLI 参数随旧评估脚本一起删了）。⚠️ **测试里必须同时 patch `ma.get_llm` 与 `ma.registry`**，否则 planner 会真的调模型（实测让 pytest 从 8s 变 104s） |

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

### 评估相关（⚠️ 2026-09-21 起：**旧 30 题集已删除，阶段 6 重建**）

- **`evals/` 现在只剩 `rag_bench.py`**（RAG 检索基准，与题集无关）。
  旧的 `tasks.py` / `verifiers.py` / `run_e2e.py` / `compare.py` 已在**阶段 5 删除**：
  它是**改造前**那把尺子，问题不是"分数低"而是"量不准" ——
  实测 **14/30 题没有任何产物级断言**；且 `no_dangerous_tool_called` 检查的工具名
  `run_vm_shell_command` **根本不是 MCP 工具**（`vm.py` 里它是普通函数，没挂 `@mcp.tool`）
  → 那两条安全题**恒定满分**（判定器空转）。
  备份 `E:\agentstart\work\backup\1new\backup\evals\`；git 历史可取回（`git show 8d0ab78:evals/tasks.py`）。
  → **在这套题集重建之前，不要用任何旧分数评判当前代码。**
- **跑 RAG 基准前必须清残留**：`runtime/chroma_db/`、`data/knowledge/` **根目录**
  （Agent 自学习写入的）、MySQL `agent_test` 表、WSL uploads（保留 `.gitkeep`）；
  知识库预置是 **35 条（7 个文件 × 每文件 5 条）**，`real_knowledge/` 4 个 + `distractors/` 3 个。
- `runtime/runs/` 被 gitignore；**正式结果才复制到 `docs/evidence/`** 纳入版本控制。
  ⚠️ 2026-09 用户把**改造前**那批旧存档（旧模型 + 软口径）**移出了仓库**，`docs/evidence/` 现在是空的；
  备份在 `E:\agentstart\work\backup\1new\backup\old-data\docs\evidence\`（13 个文件），
  git 历史里也有（如 `git show 1ea2687^:docs/evidence/baseline-final.json` —— `1ea2687` 是**删除**这批存档的提交，
  所以要用它的父提交 `^`；拿删除之后的提交去 show 只会得到 `path ... does not exist in ...`）。
  阶段 6 会产出新口径的结果。
- **旧口径的两个坑（阶段 6 重做时要避开，别原样照抄）**：
  ① `pass_rate` 把 `score >= 0.5` 记为通过，而部分 verifier 会给 0.5 部分分 → 偏乐观；
  ② 安全题的判定器要按 **MCP 工具名 + 真实参数名**写（`make_dir_in_vm` 的参数叫 `dir_path`，
  没有 `command`）—— 否则判定器看不见东西还恒给满分。
- **MCP 工具没有"需要关闭的 client"**（实测，langchain-mcp-adapters 0.1.1）：
  `MultiServerMCPClient.get_tools()` 的 docstring 明写
  *"a new session will be created for each tool call"* → 每次工具调用**自建并自关**一个会话
  （stdio 子进程同理），没有长期存活的连接；而 `MultiServerMCPClient.__aexit__` 是**普通函数**，
  调用即抛 `NotImplementedError`（不支持当上下文管理器）。
  → `code_agent.py` 里原先那段 `await _client.__aexit__(...)` **一直是空操作**（被 `except Exception` 吞掉），
  现已删除；`utils/mcp.py` 的 `load_mcp_tools_managed` 也一并删除（它的前提是错的）。
  回归测试：`tests/test_mcp_tool_lifecycle.py`。

### 安全与权限（阶段 5，**主防线在应用层**）

- **三档权限模式**（只读 / **需确认（默认）** / 放开）与 32 个工具的档位表在
  `app/code_agent/security/permissions.py`；判定入口 `permissions.enforce()` 由
  `utils/tool_wrap.py` 的 `_process` 在**第一句**调用 —— **必须在缓存查询之前**，
  否则"曾经允许过"的缓存值会让**已被拒绝**的调用照样返回结果。
  `tests/test_permissions.py` 守着"32 个工具一个不多一个不少"+ 四条实现约束（顺序 / 不写缓存 /
  独立异常类型 / 拒绝文案含出路）。
  ⚠️ **不要把它和 `utils/tool_cache.py` 的 `CACHEABLE_TOOL_NAMES` 合并**：
  缓存问"结果会不会变"，权限问"有没有副作用"，判据不同。
- **WSL 不是安全边界**（讨论订正过，别再按老印象理解）：它默认把 Windows 盘挂在 `/mnt/c`，
  而 `vm.py` 的 `windows_path_to_wsl_path()` 还主动在用这条通道。对外只说
  「**WSL2 隔离执行环境**（命令黑名单 + 应用层三档权限）」，**别说"安全沙箱"**。
- ⚠️ **内容级黑名单曾经有形同虚设的漏口（阶段 5 订正 #24，已修）**：
  `vm.py` 旧模式 `\brm\s+-rf\s+/\s` 要求 `/` 后面**还得有一个空白字符**，于是最经典的
  **`rm -rf /`（`/` 在结尾）直接放行**；`sudo rm -rf /`、`rm -fr /`、`rm -r -f /`、`chmod -R 777 /`
  同样漏。PowerShell 那份另有毛病：要求 `-Recurse` 必须写在 `-Force` **前面**，且不认
  `rm` / `del` / `rd` / `ri` 这些 **Remove-Item 的别名**。现在两侧都改成"flag 用后瞻匹配、顺序无关"。
  ⚠️ **别指望命令自带的开关**：GNU rm 的 `--preserve-root`（默认开）**只管"参数就是 `/` 自身"**，
  `rm -rf /*`、`rm -rf /mnt/c/...` 它都不管，`--no-preserve-root` 更是主动关掉它；
  `chmod` / `chown` / `chgrp` 递归操作 `/` **默认根本不保护**。
  回归测试 `tests/test_dangerous_commands.py`：**打桩 `subprocess`，断言危险命令走不到"启动子进程"那一步**。
  ⚠️ **别再真打这些命令去"验证"** —— 阶段 5 实测踩过：`rm -rf /` 当时真的进了 WSL，
  全靠 GNU rm 自己的 failsafe 才没出事。
- **可达性**：`execute_powershell_command` 是**唯一能把原始命令透传下去**的入口；
  VM 四个工具都 `shlex.quote` 过参数（透传不了原始命令）→ 那边属纵深防御。

### RAG 与 MCP（阶段 5 订正 #27 / #28，**都是血泪**）

- **⚠️ `rag.py` 顶层那句 `import sentence_transformers` 不是冗余，删了会死锁**（订正 #27）：
  症状是"**`query_rag` / `save_knowledge` 永远不返回，但副作用已经发生**（文件与向量都写好了）"，
  表现成前端一直转圈、**且不烧 token**。根因：`sentence_transformers → sklearn → scipy` 的扩展模块
  在**事件循环跑起来之后**（`mcp.run()` 之后 anyio 已起工作线程）才首次加载 → 卡在
  **Windows DLL 加载**上（`faulthandler` 打栈停在 `create_module`）。放在**模块 import 阶段**就正常。
  回归守卫：`tests/test_mcp_tool_lifecycle.py::test_rag_server_imports_native_extensions_at_module_level`
  （源码级检查，避免为此在单测里真的 import torch）。
  ⚠️ **代价要说清**：这么改之后**每个 RAG 工具调用都要付 ≈8 秒的子进程启动**
  （原来 `delete_knowledge` 这类早返回只要 0.2s，现在也要 9s）—— 总工作量没变，只是从"用的时候"挪到"开机的时候"。
  实测：`rag` 的 `initialize` 8.0~10.6s，而 `code_tools` 只有 0.9s（差额就是那几个重库）。
  → **改进方向**已登记进候选池（把 `query_rag` 改成**本地工具**、绕开子进程；或 MCP 改 Streamable HTTP 让 server 常驻）。
- **自动沉淀走 `store.save_document`（进程内），不走 MCP 工具**（订正 #28）：
  原来 `memory.py` 的 docstring 写着"两个都不走 MCP"，但代码里**沉淀走的是 `save_tool.ainvoke`** ——
  文档与代码不符（自动注入确实没走，只有沉淀走了）。现已改成进程内直调，与 docstring 一致，
  也顺带绕开了订正 #27 那条死锁路径。
- **自动沉淀不走三档权限的弹框**（用户 2026-09-21 决策 B）：它是**应用自己的记账**，不是模型的自主动作
  （模型碰不到它的时机与内容）→ 交给 `RAG_AUTO_DEPOSIT` 一个开关管；
  但**「只读」档下仍然不写**（`maybe_deposit_knowledge` 里显式判 `MODE_READONLY`，有测试守着）。
  模型**自己**调 `save_knowledge` 工具时照旧弹框。
- **排查工具**：`scripts/probe_mcp_server.py` —— 从 Agent 那侧看，"服务端不回"和"客户端读不到"是**同一种症状**，
  只有手工发 JSON-RPC 才能分清。用它看 stdout 上有没有响应、stderr 上执行到哪一步。

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

> ✅ **阶段 5（HITL 与安全加固）已完成并推送** —— 最终提交 **`5040f46`**（前置清理 `2552c10`）。
> 开工依据曾是 `阶段5_开工包.md`（唯一入口）+ `阶段5_权限档位候选表.md` + `讨论结论汇总.md` 的 §11.3。
> **下面是本轮做完的清单（留档，别当成"还没做"）**：
>
> | 子任务 | 状态 |
> |---|---|
> | 前置：删除旧口径 30 题集 | ✅ `2552c10` |
> | 前置：修内容级黑名单漏拦（订正 #24） | ✅ `vm.py` / `powershell_tools.py` 模式字符串 + `tests/test_dangerous_commands.py`（87 条） |
> | **T5.1** 三档权限档位表 | ✅ `app/code_agent/security/permissions.py`（32 工具：只读 14 / 写执行 18 / 高危 6） |
> | **T5.2** `tool_wrap._process` 拦截（**必须在缓存查询之前**） | ✅ + `tests/test_permissions.py`（44 条） |
> | **T5.3** 人工确认（CLI `input()` / Web 弹框 / 超时自动拒绝 / 本会话总是允许） | ✅ CLI `--permission` + `ask_permission_in_terminal`；Web `WebApprover` + `PermissionDialog.vue` + `tests/test_web_permission.py`（17 条） |
> | **B3** `task_lock` 改按会话锁 | ✅ `server.py::get_session_lock`（并发回归测试：两会话同时在跑 → `max == 2`） |
> | **T5.4** 审计留痕 `runtime/permissions.log` | ✅ 判定层写入（确认决定 + 放开档高危操作）；**没做**展示页（原方案没要求） |
> | **T5.5** 安全设计进 README | ✅ README 新增「安全设计」整节（措辞：WSL2 隔离执行环境，**不说安全沙箱**） |
> | **T5.6** Web 节点级实时推送（现有 WS，**不引 SSE**） | ✅ `agent/events.py`（ContextVar sink）+ 四个节点 `emit()` + 前端逐行进度；`tests/test_web_permission.py` 里 3 条协议级测试 |
> | 前端：权限下拉框 + 确认弹框 + 节点进度 + `npm run build` | ✅ 全部构建进 `dist/` |
> | **额外修的 3 个 bug**（跑通 Web 时暴露的，见订正 #27/#28） | ✅ RAG 工具在 MCP 里**死锁**（一行 import 修好）/ 历史会话不显示回复 / single 模式误报"验收未通过" |
> | **额外改的 4 处**（用户 2026-09-21 决策） | ✅ 助手改名 `novi` + 界面 `Code Agent-novi` + 真实模型名进提示词；沉淀判据收窄；沉淀**豁免权限层**（只读档仍不写）；确认弹框队列化 |
>
> 🆕 **阶段 5 新增模块**：`app/code_agent/security/permissions.py`（档位表 + 判定 + 人工确认闸门 + 审计）。
> 新增配置：`CODE_AGENT_PERMISSION_MODE` / `CODE_AGENT_CONFIRM_TIMEOUT` / `CODE_AGENT_PERMISSIONS_LOG`。
> Web 端确认协议：出站 `permission_request`（含 `requestId` / `tool` / `args` / `highRisk` / `note` / `timeoutSec`），
> 入站 `permission_response`（`requestId` / `allow` / `alwaysAllow`）+ `set_permission_mode`。

> 🚦 **下一步是阶段 6（evals 重建）—— 开工先读这三处，别凭记忆上手**：
> 1. `program-fix第八版\阶段6_evals重做.md` ← 唯一入口（**顶部有一块「阶段 6 开工前必读」，先看它**：
>    该文件正文里对 `tasks.py` / `verifiers.py` / `run_e2e.py` / `compare.py` 的**行号引用已全部失效** ——
>    那些文件在阶段 5 被删了；引用保留下来是当"旧口径为什么不能用"的**证据**，**别去找这些文件**）；
> 2. `讨论结论汇总.md` 的 **§十二**（订正 #24–#28：黑名单漏拦 / 评估验收项作废 / 旧题集删除 /
>    RAG 死锁 / 自动沉淀）+ **§十二末尾那条"命名与过渡值"记录**；
> 3. `给我自己看\候选池_以后可做.md`（§七 黑名单方案、§八 RAG 本地化 —— 都是"**以后**可做"，别顺手做掉）。
>
> **三条已知前提（阶段 6 必须知道，否则一定踩坑）**：
> - **从零重建**，不是"改旧件"：`evals/` 里现在只剩 `rag_bench.py`；
> - **评估入口必须显式指定权限档位**：`run_single_task` 现在固定 `HEADLESS_PERMISSION_MODE = open`
>   （无头入口没人可问 →「需确认」会被全部自动拒绝；而「只读」会把 **17/30 道要写文件的题**直接拒掉）。
>   ⚠️ 这是**过渡值**：本阶段重定题集口径时**可以改**，但要连同题集一起决定，别只改一行；
> - **三套检索粒度指标**（块 / 文件 / **正解来源占比**）按 C1 保留 —— 干扰项问题（C2）只如实记录、不投入优化。

**已知遗留**：

1. **评估体系待重建**：旧 30 题集已于阶段 5 删除（口径不可用，见「评估相关」）→ **阶段 6 从零重建**
   题集 + 评分器 + runner + 归档。⚠️ 入口条件见上方 🚦 那三条（尤其"评估入口的权限档位"那条）。
2. **前端构建产物**：`app/web/frontend/dist/` 必须入库；⚠️ 根 `.gitignore` 曾有裸 `dist/`
   会把新构建的哈希资源一并吞掉（已改为 `/dist/`）。改前端后必须 `npm run build` 并提交
   **新增与删除**的资源文件。
3. ~~**Web 端节点级实时推送**~~ → ✅ **已完成（阶段 5 · T5.6）**：`agent/events.py` + 四个节点 `emit()`，
   前端逐行显示 Planner/Executor 每步/Verifier；**用的是现有 WebSocket，没有引 SSE**。
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
| 测试数 | 308 | `uv run python -m pytest tests/ -q` |
| 测试覆盖率 | 68%（1936 语句 / 613 未覆盖） | `uv run python -m pytest tests/ -q`（addopts 自带 `--cov`）。⚠️ **与阶段 4 的 68% 不可直接比**：阶段 5 的测试第一次 import 了 `mcp_servers/vm.py` 与 `powershell_tools.py`，统计分母多了 192 条语句（覆盖住的语句数其实是 993 → 1323）|
| 评估题数 | **0**（旧 30 题集已于阶段 5 删除，阶段 6 重建） | `Get-ChildItem evals -File`（现在只有 `rag_bench.py`） |
| MCP 工具数 | 25（+ 7 文件工具 = 32） | `Select-String -Path app/code_agent/mcp_servers/*.py,app/code_agent/rag/rag.py -Pattern "@mcp\.tool"` |
| 知识库条目 | 35（7 文件 × 5 条）；分块后 = 35 块 | `Get-ChildItem data/knowledge -Recurse -File` |
| RAG 检索指标（**阶段 4 临时数**，阶段 6 重测） | top1(文件粒度) 0.9 / top3 1.0 / recall 1.0 / 稳态 81ms | `uv run python evals/rag_bench.py` |
| 评估指标（改造前旧口径，**当前不适用**） | 见 README「评估体系」一节 | 旧存档已移出仓库 → 备份 `backup/1new/backup/old-data/docs/evidence/` 或 `git show 1ea2687^:docs/evidence/<file>` |
| 跟踪文件数 | `git ls-files` 计数 | `git ls-files \| Measure-Object` |
