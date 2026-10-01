# AI Agent Test — LangGraph + MCP Code Agent

> **给谁看**：**AI 编码助手**（Claude / Cursor / Copilot / 其他）。
> 人类读者看 [`README.md`](README.md)；**接班人的上下文与待办看仓库外的 `handover_交接文档.md`**
> （2026-09-27 起移出仓库，位置见「仓库整理」）。
> 本文件只负责四件事：**约定 / 命令 / 已知坑 / 当前进度**（架构与功能描述不在这里）。

## 项目一句话

Python 3.13 的本地多 Agent 编程助手：LangGraph StateGraph（Planner → Executor → Verifier）
+ 6 个自建 MCP Server（stdio 子进程，25 个工具）+ FileManagementToolkit（7 个文件工具），
双入口（CLI `main.py` / Web UI `app/web/server.py`）。
**定位：本机单用户**（2026-09-30 用户定）—— **不做公网部署、不做多用户账号**：它会执行 shell / 读写主机文件，公网开放等于给陌生人一个远程代码执行入口；部署与 CI/CD 留给**第二个项目**。对外展示 = **GitHub 主仓库 + Gitee 镜像**。
⚠️ **旧 30 题评估体系已于阶段 5 删除**（口径不可用）；**阶段 6 已从零重建并跑完正式两轮**（30 题 + 163 条强断言）：
**single 30/30、multi 30/30，平均分 1.0000**，结果归档在 `docs/evidence/v3-single.json` / `v3-multi.json`
—— 见下方「评估相关」。阶段 7（收尾包装）**待开工**。

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
| 12 | **命名是"有意两套"的，别去统一**：助手**自称** `novi`（`PROMPT_CONTEXT["name"]`）、**界面产品名** `Code Agent-novi`；而 `README.md` / 本文件（以及**仓库外**的 `handover_交接文档.md`）里的**项目名保持 `Code Agent`**（用户 2026-09-21 决定：只改界面） |

## 阶段收尾清单（**不做完不算阶段完成**）

> 为什么要有这一节：活文档（本文件 / `README.md` / **仓库外的 handover**）描述的是"现在是什么样"，
> 每改一次代码就可能失效一处。**过期比缺失更危险** —— 缺失可以读代码补，过期会让下一个会话照着错的做。
> （教训：阶段 2 改了容器与 CI，却只更新了 README，本节列出的 AGENTS.md 段落整段过期。）

| # | 收尾动作 |
|---|---|
| 1 | 刷新本文件「当前进度」：已完成阶段、已知遗留 |
| 2 | 核对并修改本阶段**影响到**的段落：环境与工具链 / 常用命令 / 代码地图 / 数字来源速查 |
| 3 | 刷新**仓库外** `handover_交接文档.md` 的「当前状态快照」与「当前进度」（它已不在本仓库，见「仓库整理」） |
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
| 一键起 Web UI（**前台**跑，日志就在这个窗口；`-Dev` 另开窗口跑热更新） | `.\scripts\run\start-app.ps1`（或双击 `scripts\run\start-app.cmd`；换端口 `-Port 8001`） |
| 单元 + 工具级测试 | `uv run python -m pytest tests/ -v`（**592 个**） |
| **真集成测试**（要 MySQL / WSL / Redis / SearXNG；**只能在 Windows 本机跑**，CI 没有 WSL；默认不跑 —— `addopts` 里带了 `-m "not integration"`） | `uv run python -m pytest -m integration -v`（5 条） |
| **跑评估前先预检**（容器 / WSL / `.env` key / 端口 / 知识库，**不修任何东西**） | `uv run python evals/preflight.py --run-id v3-single` |
| **复用同名 run-id 前必跑**（清库里的 eval 线程；默认只报告，`--yes` 才删） | `uv run python evals/reset_eval_threads.py --yes` |
| 逐题跑完后合并成一轮（缺题会拒绝写出） | `uv run python evals/merge_runs.py --prefix v3-single --mode single --archive` |
| 看评估题集（**不跑、不烧 token**） | `uv run python evals/run_e2e.py --list` |
| 跑评估（**逐题跑**，单/多 Agent 各一轮，**必须换 run-id / 前缀**） | `uv run python evals/run_e2e.py --task E001 --mode single --run-id v3-single-E001`（逐题循环见下方手册） |
| 试跑几道题 | `uv run python evals/run_e2e.py --task E001 --mode single` / `--limit 3` |
| 出评估报告（Markdown，数字全部现算） | `uv run python evals/report.py --single docs/evidence/v3-single.json --multi docs/evidence/v3-multi.json --rag docs/evidence/rag_ablation_20260924_053228.json --out docs/evidence/评估报告.md`（⚠️ 三个参数都是**单值**、**不吃通配符**；用 `docs/evidence/` 里入库的那份，别人 clone 下来也能复现） |
| 报告生成器自测（**用假数据**，不跑评估） | `uv run python evals/report.py --selftest` |
| MCP server 探针（排查"工具调不通"） | `uv run python scripts/probe_mcp_server.py rag query_rag --args '{"query":"MCP"}'` |
| RAG 基准（含分块/精排指标） | `uv run python evals/rag_bench.py` |
| **RAG 消融对照**（改造前后 + 全量召回对照组，**不用 LLM**） | `uv run python evals/rag_ablation.py --reps 10 --archive` |
| 重建前端 | `cd app/web/frontend && npm run build` |

> ✅ **评估体系已于阶段 6 重建并跑完正式两轮**：`single 30/30`、`multi 30/30`（平均分都是 1.0000，
> 163 条断言：状态 124 / 轨迹 29 / 文本 10），归档在 `docs/evidence/v3-single.json` / `v3-multi.json`。
> 跑法与口径见下方「**评估相关**」；**改造前那批旧分数一律不可比**（旧题集已删、口径已换）。
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
├── agent/prompts.py             SYSTEM_PROMPT_TEMPLATE（Plan→Execute→Verify 三步法）
│                                  + prompt_context(role)：**模型名运行期解析**（订正 #37，别用静态 PROMPT_CONTEXT）
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
config/models.json               内置模型注册表（**显示名 key + 实际调用名 model**）+ 角色分配 + 降级链。
                                 ⚠️ **默认是空的** = 四个角色走 .env 的 MODEL_NAME（这就是「使用配置默认」）；
                                 加内置预设的格式写在它的 `_readme` 里。**进版本控制**，不要放 runtime/；
                                 用户自定义模型不在这里（见 web-settings.json 的 custom_models）
app/web/server.py                FastAPI：WS /ws/chat（含权限确认协议）+ REST + 静态托管 dist
                                 + 阶段 7：`/api/sessions` 一组（列表 / 改标题 / 置顶 / 回收站 / 恢复 /
                                   彻底删除 / 清空系统线程）+ `_active_threads` 活会话登记（删会话前的保护）
app/web/sessions.py              ★ 阶段 7：会话元数据**侧车库**（`runtime/sessions.db`）——
                                 标题（新会话第一条用户消息；**用户改过就不再被覆盖**）/ 置顶 /
                                 软删除（回收站）/ 彻底删除（真删 `writes → checkpoints`）/
                                 系统线程前缀（`eval-` `probe-` `smoke` `nowrap-`）与一键清空
app/web/frontend/src/            Vue3 源码：App.vue（执行/权限两个下拉框 + **会话列表：标题、相对时间、
                                 📌 置顶、✏️ 重命名、🗑 回收站、系统线程折叠**）+ store.js + components/
                                 （ChatView / ResultCard / **MarkdownText**〔阶段 7：marked + DOMPurify〕
                                 / SettingsPanel / **PermissionDialog**〔阶段 5〕）
evals/                           ★ 阶段 6 重建的评估体系（11 个文件，口径见「评估相关」）：
                                 tasks.py（30 题题集）/ verifiers.py（43 个判定器工厂，四档强度）
                                 runner.py（执行引擎）/ run_e2e.py（命令行入口）
                                 preflight.py（跑前环境预检）/ report.py（报告生成器，含 STAR）
                                 rag_bench.py（RAG 检索基准）/ rag_ablation.py（RAG 消融对照）
                                 merge_runs.py（★ 逐题分片 → 合并成一轮）
                                 reset_eval_threads.py（★ 清库里的 eval 线程 —— **复用 run-id 前必跑**）
                                 env.py（★ 语料隔离：use_eval_corpus() 把评估指到自己的
                                   runtime/eval_knowledge + chroma_db_eval，**不碰产品库**）
                                 fixtures/knowledge/（只读夹具：4 篇正解 + 3 篇干扰，7 文件 / 35 条）
scripts/                         probe_mcp_server.py（手工发 JSON-RPC 探某个 MCP server 到底回没回）
                                 + mysql-init/*.sql（被 docker-compose 当**挂载目录**用，别挪）
scripts/run/                     ★ 启动/停止脚本（`README.md` 里有对照表）：
                                 start-app.cmd / start-app.ps1（起 Web UI，前台）
                                 start-deps.ps1 / stop-deps.ps1（起停 4 个依赖容器）
                                 ⚠️ `.ps1` 必须是 **UTF-8 with BOM**；脚本找仓库根要往上**两层**
tests/                           592 个测试（config / prompts / mysql_safe_ident / mysql_readonly /
                                 multi_agent / checkpoint / route / llm_registry / mcp_tool_lifecycle /
                                 tool_level / context / memory / tool_cache / tool_wrap / rag_chunking /
                                 permissions / dangerous_commands / **powershell_exec** /
                                 web_permission〔阶段 5 的三个〕/
                                 web_model_settings + evals_verifiers / evals_tasks / evals_runner /
                                 evals_report / evals_rag_ablation / evals_preflight / evals_env /
                                 **evals_merge_runs** / **evals_reset_threads** /
                                 prompt_model_name〔阶段 6 的十二个〕/
                                 **multi_retry_verdict**（D1/D2）/ **mysql_connection_error**（D3）/
                                 **evals_artifact_resolution**（D4）/ **evals_dangerous_executed**（D5）
                                 〔2026-09-25 加的四个，共 +19 条；同期 runner / reset_threads 也补了用例〕/
                                 **web_sessions**（侧车库：标题/置顶/回收站/系统线程，25 条）+
                                 **web_sessions_api**（那组接口与"活会话不许删"，9 条）
                                 〔阶段 7 · T7.5 加的两个，共 +34 条〕/
                                 **web_no_model**（T7.6：import 期不许建 LLM / 缺 key 不致命 /
                                   第一个模型接管四个角色，5 条）〔阶段 7 · T7.6〕/
                                 **web_test_roles**（`test-roles` 去重 + 按角色分组，3 条）/
                                 **integration_mcp**（真 MySQL / WSL / SearXNG / Redis / RAG，
                                   5 条，**默认不跑**，`-m integration`）
                                 〔以上 8 条 = 阶段 7 · T7.6 与 T7.4〕）
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
| **模型按角色配** | 注册表在 `config/models.json`；`get_llm(role)`，默认 executor；运行期改法有两个：Web UI 设置面板（`set_role_models`）与 `registry.override_from_spec()`（原 `evals --role-models`，那个 CLI 参数随旧评估脚本一起删了）。⚠️ **测试里必须同时 patch `ma.get_llm` 与 `ma.registry`**，否则 planner 会真的调模型（实测让 pytest 从 8s 变 104s） |
| **`models.json` 默认是空的（有意为之 · 2026-09-22 用户决定）** | `models: {}` + `roles: {}` → **四个角色都走 `.env` 的 `MODEL_NAME`**，这就是界面下拉框第一项「使用配置默认」。内置预设**不再暴露给用户**（用户要加模型就自己在面板里加）。要加内置预设的格式写在 `models.json` 的 `_readme` 里 |
| **用户自定义模型**（阶段 6 · 方案 A） | Web 面板「我的模型」→ 存 `web-settings.json` 的 `custom_models`，**每个模型自带 key/base_url**；`LLMRegistry.set_custom_models()` 加载，`_build_for_key` **优先用自带凭据** → 与内置/系统默认那组全局凭据互不污染。接口：`POST /api/settings/custom-model`、`DELETE /api/settings/custom-model/{id}`（**删除会顺带清掉角色里指向它的引用**） |
| **「当前生效模型」必须看得见**（阶段 6 · 用户实测后提的） | 模型是**全局配置、不随会话保存**，四个角色还可各不相同 —— 顶栏常驻显示 Executor 那个（`registry.effective_models()` 经 `/api/models` 给前端）；每条结果卡片另显示「**本轮实际使用的模型**」（`response_metadata.model_name` = 服务端真实回报的名字，与"配置里写的"区分开）。⚠️ 配置改动**从下一条消息生效**：正在跑的那条任务用的是它启动时的快照 |
| **三处模型配置的优先级**（2026-09-22 明确，别搞反） | `runtime/web-settings.json`（界面点出来的）**>** `config/models.json`（仓库默认）**>** `.env`（密钥 + 兜底模型名/地址）。⚠️ 两个后果：① 界面上存过一次模型选择后，改 `models.json` 的 `roles` **不生效**；② **evals / CLI 根本不读 `web-settings.json`**（`load_settings`/`apply_settings` 只存在于 `app/web/server.py`）→ 它们只用 models.json 的 roles + `.env` 的 key。**跑评估前必须确认 `.env` 的 key 是有效的**（界面里那个 key 帮不上忙） |
| **`web-settings.json` 含明文密钥** | 内置凭据 + 每个自定义模型的 key 都是明文写在这个文件里。它已 gitignore，但**不要分享**；界面只回显尾号；`/api/models` 与 `/api/settings` **都必须先剥掉 `api_key`** 再返回（`tests/test_web_model_settings.py` 守着"响应里不出现明文 key"）。⚠️ **改 `.env` 要重启进程才生效**（`load_dotenv` 只在 import 时跑一次），界面改的则热生效 |
| **提示词里的模型名必须运行期取**（订正 #37） | `prompts.PROMPT_CONTEXT` **故意不含 `model_name`** —— 它要运行期从注册表取（`effective_model_name(role)` → `resolve_model(role)["model"]`，取**实际调用名**），格式化一律走 `prompt_context(role)`。⚠️ 以前写的是 `.env` 的 `MODEL_NAME`（**import 时定死**）→ 用户在面板切了模型、`rebuild_agents()` 也重建了 agent，**提示词里还是旧名字**（用户实测：换成 mimo，模型却说自己运行在 deepseek-flash）。拿静态 `PROMPT_CONTEXT` 去 format 会 `KeyError: 'model_name'` —— **这是故意的**：宁可响亮失败，别静默报错名字 |
| **显示名 ≠ 调用名** | `models.json` 里键是**给用户看的显示名**，`model` 字段才是**实际发给 API 的名字**。这样官方改名/下线（如 2026-09-10 V4 Flash → V4.1 Flash，旧名"暂时路由"）时只动 `model` 一行。前端只在两者不同时才补一句「实际调用 xxx」 |

### 环境与工具链

- **MCP 工具的接口形状**：MCP 适配层生成的是 `StructuredTool(coroutine=…, response_format=…)`，
  **没有 `func`** → 想包装工具**不能用 `tool._run`**（对 32 个 MCP 工具都会 raise）；
  FileManagementToolkit 的 7 个工具则**只有同步 `_run`**。两条路径都要处理。
- **运行时目录**（阶段 1 已修）：`config.py` 现在会创建 `RUNTIME_DIR / WORKSPACE_DIR /
  CHROMA_DIR / RUNS_DIR / TOOL_RESULTS_DIR`。此前全新 clone 下 `tests/test_config.py`
  会因为目录不存在而失败（本机通过只是因为有残留）。
  ⚠️ 这里**曾经还有 `CHECKPOINT_DIR`（`runtime/checkpoint/`）**，是阶段 1 之前"一个会话一个 JSON 文件"
  的目录方案遗留 —— 它每次启动都被 mkdir 回来，却和真正在用的 `runtime/checkpoints.db`
  只差一个 s。**2026-09-22 已按用户决定彻底移除**（常量 + mkdir + 那条断言 + 目录本身），
  并留了一条"不存在"的断言防它被加回来（`tests/test_config.py::test_legacy_checkpoint_dir_is_gone`）。
- **改造前的历史不在 `master` 上**：`master` 的**地基** `f2f3bbb`（"init: 导入改造前基线"）是**单提交重建**的，
  它下面没有历史；改造期的提交都直接追加在它上面（`git log --oneline` 看得到）。
  要找**改造前**的东西必须去 `refs/remotes/raw-origin/*`（旧仓库 master / phase1..phase5）→ 考古要用 **`git log --all -S '...'`**。
- **依赖服务怎么起**（阶段 2 统一后，实测）：
  - `agent-mysql` / `searxng` / `redis-stack-server` 由**仓库根的 `docker-compose.yml`** 管理
    （`name: code-agent-deps`）；**`my-nginx` 仍归 WSL 里的 `~/nginx/docker-compose.yaml`**
    （它的挂载源是 WSL 路径，搬到 Windows 侧 compose 会**静默挂空目录**）；
  - 一键脚本：`./scripts/run/start-deps.ps1` / `./scripts/run/stop-deps.ps1`；
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
- **`scripts/run/start-deps.ps1` 会先探一次 `~/nginx`**：不存在就**打印明确提示并跳过** nginx 那步
  （而不是抛一段 WSL 报错）—— 因为 `~/nginx/` 只存在于 WSL，仓库里没有副本。
- **搜索已不依赖浏览器**（阶段 2）：`browser_tools.py` 只调 SearXNG 的 JSON API，
  文件名是历史遗留；**Selenium / Edge / msedgedriver / 调试端口都不再需要**。
- ⚠️ **`powershell_tools.py` 的三个缺陷（2026-09-24 修，订正 #39 —— 它们是 E016 事故的根因）**：
  ① **`shell=True` + 列表** ⇒ Windows 上实际执行 `cmd.exe /c powershell -Command "<整条命令>"` ⇒
  **多行命令在第一个换行处被截断**（且返回"成功但没有输出"，把人骗过去）；
  ② 同一个原因 ⇒ **命令里的 `&` 被 cmd 当分隔符**（`?a=1&b=2` 里的 `b=2` 变成独立命令，返回码 255）；
  ③ 固定 `encoding="gbk"` ⇒ PowerShell（GBK）与它启动的 **Python 子进程**（若继承
  `PYTHONIOENCODING=utf-8` 就输出 UTF-8）**混在同一路流里** ⇒ 固定任何一种都乱码。
  **修法**：`shell=False` + **逐行解码** + stderr 回显兜异常。
  ⚠️ 回归测试在 `tests/test_powershell_exec.py`，**其中 3 条必须真起子进程** ——
  缺陷 ①②的破坏发生在 **cmd.exe 的解析阶段**，纯打桩证明不了"它真的不再被截断"。
  🔴 **逐行解码有两个坑，第二个是后来才补上的（订正 #45）**：
  ① 第 1 版写的是"**先 UTF-8、失败退 GBK**" —— 但 **GBK 的字节序列有时恰好也是合法 UTF-8**
     （`目录` 的 GBK 字节 `C4 BF C2 BC` 解成 `Ŀ¼`）⇒ **永远轮不到 GBK 分支**，模型看到的还是乱码；
  ② 所以判据要再加一条：**解出来的字符若落在 `U+0080–U+02FF`（拉丁补充/扩展）就改用 GBK**
     —— 正常的中英文输出几乎不会用到那一段。
     已知假阳性（刻意接受）：真正的 UTF-8 拉丁文本（`café`）会被按 GBK 解成 `caf茅`。

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
  ⚠️ **2026-09-24 起，评估也默认关掉「自动注入」**（`auto_inject=False`，订正 #44）——
  因为 `inject_relevant_knowledge()` **没有任何相关性阈值**（每题都注入 top-3），
  而 8 个维度里**没有一个是"抵抗错误知识"**，开着等于给每题都加一个没打算测的变量。
  **例外：`E022`**（`TaskSpec.inject_knowledge=True`）—— 它是 `query_rag` 唯一的端到端覆盖。
  注入内容**会记进结果 JSON 的 `knowledge_injected`**（1 行改动，以前只返回不落盘）。
  ⚠️ **另外「关掉自动沉淀」只关了「应用自动沉淀」这一条路**（订正 #35，2026-09-23 smoke 实测）：
  **模型自己会调 `save_knowledge` 工具**（E007 就把"MySQL 经验"写进去了，872 字符 / 3 块），
  这条路**关不掉** —— 工具是产品真实存在的，为评测摘掉它等于改口径（32 → 31 个工具）。
  所以评估改成：**每题开跑前把知识库复位**（`runner._reset_knowledge()`：清根目录散文件 +
  `store.seed_knowledge_base()` 清掉"文件已删但块还在"的 source）。
  ⚠️ 复位**不能调 `ensure_seeded()`**（它带进程级 `_seeded` 标志，第二次调用直接跳过 = 永远不清）。
- **`import app.code_agent.rag.store` 不加载模型**（全懒加载）：单测里碰它不会去 load torch。
  测试环境由 `tests/conftest.py` 统一关掉自动注入 / 自动沉淀 / 工具缓存。

### 评估相关（**阶段 6 已完成：正式两轮都跑完并归档**）

**正式结果（2026-09-25）**：

| 轮次 | 通过 | 平均分 | token | 耗时 | 工具 / 步数 | 归档 |
|---|---|---|---|---|---|---|
| **single** | **30/30 = 100%** | 1.0000 | 958,832 | 332s | 194 / 362 | `docs/evidence/v3-single.json` |
| **multi** | **30/30 = 100%** | 1.0000 | 1,644,029 | 687s | 227 / 409 | `docs/evidence/v3-multi.json` |

断言两轮都是 **状态 124/124、轨迹 29/29、文本 10/10**（共 163 条）；8 个维度全 1.00；
**打回 0 次、击穿预算 0、超时 0、未测 0、异常 0**。
**成本画像**：multi/single 的 token **中位 1.58×**（总量 1.71×、时间 2.07×），其中 **Verifier 中位
10,988 token/题 = 总量的 25%**、Planner 1,110 = 2.4%。
⚠️ **两条口径不同**：**single** = 上限 200k（产品默认 `TASK_TOKEN_BUDGET`，**没显式关**）+ 每题
`timeout_sec`（**逐题记在结果里**：300/360/420/480 四档）；**multi** = `task_token_budget=0` +
`task_timeout_override=0`（**只计量、不拦截**，写在该轮 `env` 快照里自证）。
⚠️ **single 那轮跑得更早**，`env` 里**没有**这两项（字段是修 D 缺陷时才加的）—— 它的口径只能从
"逐题 `timeout_sec` 有值 + 默认 200k"读出来，**别当成"single 也记了"**。
另存一份**限额版 multi**（29/30）作对照：`docs/evidence/v3-multi-旧版(限额200k).json`
—— 那次 E015 被 token 上限掐断、烧掉 257k 仍失败；改成只计量后 30/30 且**总成本更低**（1.64M vs 1.78M）
⇒ **人为闸门会制造假失败**（这条是本次最有价值的口径教训）。
⚠️ **三条不能外推**：① 每轮每题只跑 1 次（E003 那类方差量化不出来）；② 任务集对当前模型已饱和
（两轮都满分 ⇒ 架构差异**不体现在分数上**，只能从成本侧看）；③ RAG 消融语料只有 35 条原子。

**`evals/` 现状**：**十一个 .py + 一个夹具目录** ——
`tasks.py`（30 题题集）/ `verifiers.py`（43 个判定器工厂）/ `runner.py`（执行引擎）/
`run_e2e.py`（命令行入口）/ `rag_bench.py`（RAG 检索基准）/ `rag_ablation.py`（RAG 消融对照）/
**`preflight.py`（跑前环境预检）** / **`report.py`（报告生成器，含 STAR 量化对比）** /
**`merge_runs.py`（逐题分片 → 合并成一轮）** / **`reset_eval_threads.py`（清 eval 线程）** /
**`env.py`（★ 语料隔离 `use_eval_corpus()`）** + `fixtures/knowledge/`（7 篇测试语料夹具）。
**跑正式轮期间修掉的 5 个平台缺陷**（都带回归测试 + 红绿证据，见「验证记录」与
`docs/evidence/阶段6_修复与口径记录.md`）：
**D1** multi 打回上限在"裁定无法解析"时失效（`_verdict_passed`/`_is_retry_round`）；
**D2** 重跑覆盖执行轨迹（轨迹跨轮合并 + 步数累加）；
**D3** MySQL 工具返回值形状不一致（真实错误被伪装成 `too many values to unpack`）；
**D4** 产物解析把**项目自己的 `main.py`** 当选手产物（裸文件名 + git 跟踪 = 项目自带 ⇒ 不回退仓库根）；
**D5** "从未执行"的调用也算执行（`result` 为空 = 没跑过）。
另有 **6 处题面修正**（E003/E011/E023/E025/E007/E014，**断言一字未改**）。

**先预检再跑**（预检会替你验 `.env` 的 key、容器、WSL、端口、知识库，**不修任何东西**）：

```bash
uv run python evals/reset_eval_threads.py --yes        # 复用 run-id 前必跑（只删 eval-* 线程）
uv run python evals/preflight.py --run-id v3-single    # 读法：❌ = 阻塞（退出码 1），⚠️ = 不阻塞
uv run python evals/run_e2e.py --list                  # 只看结构，不执行、不烧 token
uv run python evals/run_e2e.py --task E001 --mode single --run-id v3-single-E001   # 逐题跑
uv run python evals/merge_runs.py --prefix v3-single --mode single --archive       # 合并（缺题拒绝写）
uv run python evals/report.py --single runtime/runs/v3-single.json \
                              --multi  runtime/runs/v3-multi.json \
                              --rag    docs/evidence/rag_ablation_20260924_053228.json --out docs/evidence/评估报告.md
```

⚠️ **别再用 `--all` 一次跑一整轮**（结果 JSON 只在整轮结束写一次，被掐断就整轮白跑 —— E016 事故的教训）；
逐题跑 = 每题各落一份 JSON，天然的增量保存。

> 💡 **看输出的编码提示（2026-09-24 实测，纯显示问题、不影响数据）**：
> `code_agent.py` 在 **import 时**把 stdin/stdout/stderr 统一 reconfigure 成 UTF-8（第 47-49 行，
> 为的是 Windows 控制台默认 GBK）。
> 所以① **它 import 之前**打印的行（例如 `evals/env.py` 那两句 `[evals] 语料隔离…`）是 **GBK 字节**；
> ② **之后**的行才是 UTF-8。
> 控制台代码页是 65001 时①显示成 `���`，是 936 时②显示成乱码 —— **两种都只是终端显示**，
> 落盘的结果 JSON 与报告不受影响。在 DSH 里跑时是 65001，所以只有那两句会花。

- ⚠️ **两轮必须换 `run-id`**：thread_id 里带 run-id 与 mode，复用会让第二轮读到第一轮的 checkpoint。
  预检会检查 run-id 有没有重名（`--run-id`）。
- `--archive` 另存一份到 `docs/evidence/`（纳入版本控制）；不加只落 `runtime/runs/`（gitignore）。
- ⚠️ **每题开跑前会做两件复位**：清空 `runtime/workspace/`，**以及把知识库复位**
  （`runner._reset_knowledge()`，订正 #35 —— 模型会自己调 `save_knowledge` 写进去）。
  整轮前还会清**评估自己那份**知识库的根目录散文件（`runtime/eval_knowledge/`）、
  题集声明的 MySQL 库（`eval_shop`/`eval_lib`/`eval_metrics`/`eval_decoy`）、
  **以及 WSL 上传目录**（`/home/leprite/nginx/uploads/`，保留 `.gitkeep`）。
- ✅ **WSL 上传目录的清理已经修好了**（2026-09-22 自查发现、2026-09-23 修完，见订正 #33/#34）：
  原来 `prepare_run()` 的 `wsl_uploads` 默认是 `None`（= 不清），而 `run_all()` 又没传 →
  **整整一轮都不清**，E014 的两条 WSL 断言会被上一轮的残留**蒙过（假阳性）**。
  现在**默认值就是真实目录**（"默认不安全"的默认值本身就是要修的 bug），
  返回值也从 `0/1` 改成 `{attempted, ok, removed, left}`（旧的 `0` 把"没传路径/WSL 不可用/命令失败"
  三种情况混在一起，也是它一直没被发现的原因）。回归测试：`tests/test_evals_runner.py`。
- **CLI 与 evals 都不读 `runtime/web-settings.json`** → 它们只用 `config/models.json` + `.env`，
  所以**跑评估前必须确认 `.env` 的 `MODEL_API_KEY` 有效**（界面里填的 key 帮不上忙）。
  预检会打一次 `GET {base_url}/models` 验证（**不烧 token**），并核对 `MODEL_NAME` 是否还在服务端列表里。
  ⚠️ **探针必须带 `Authorization: Bearer`** —— `verifiers.http_reachable` 刻意不带任何头（只打本机接口），
  拿它去问模型 API 必然吃 401（订正 #34 就是这么踩的）。

**判定器四档强度**（`verifiers.py`，43 个工厂 → 30 题里实际用了 **163 条**）：

| 档 | 常量 | 实例数 | 能抓住什么 |
|---|---|---|---|
| ① 文本 | `TIER_TEXT` | 10 | 模型"说对话" |
| ② 轨迹 | `TIER_TRACE` | 29 | 调没调对工具 / 过程预算 |
| ③ **状态** | `TIER_STATE` | **124** | **真实产物**（文件 / 库表 / WSL / 接口） |
| ④ LLM 评分 | `TIER_JUDGE` | 0 | 质量维度（**本批题集没用它**，且它默认只是参考分、不参与通过判定） |

**四条口径（相对旧尺子的修正，别改回去）**：

1. **通过 = 满分**（旧口径 `score >= 0.5` 记为通过，而部分判定器会给 0.5 部分分 → 通过率虚高）；
   部分分单列 `partial` 并在报告里披露占比。
2. **`skip` ≠ 0 分**：环境不可用（MySQL/WSL 连不上）记 `ok=None`，既不算对也不算错；
   **全部 skip 的题标 `unavailable` 且不进分母**（旧口径把"没测"平均进总分 = 记成"做错了"）。
3. **超时/异常也跑判定器**（产物可能已经写出来了；旧口径只在 `status == completed` 时验分 → 超时题直接 0）。
4. **判定器按真实 MCP 工具名 + 真实参数名写**：`assert_known_tools()` 对着 `permissions.ALL_TOOLS` 校验，
   危险命令扫描**整个 args**。⚠️ 旧题集检查的 `run_vm_shell_command` 根本不是 MCP 工具、
   又只读 `args["command"]`（VM 工具的参数叫 `dir_path`）→ 那几条安全题**判定器空转、恒定满分**。

**题集结构**（`tests/test_evals_tasks.py` 机械守着，改题集时它会红）：
30 题 = **基础 10 / 长任务 12 / 对抗 8**；8 个维度各 ≥3（task_completion 5、safety 5、
multi_step 4、cross_tool 4、tool_selection 3、error_recovery 3、context_management 3、efficiency 3）；
**每题至少 1 条状态断言，且不能只有 LLM 评分**。
⚠️ **对抗题不许让 Agent 真去执行危险命令** —— 危险命令拦截由 `tests/test_dangerous_commands.py`
（打桩 subprocess）验证；评测里放真命令等于"防线一失效就把机器删了"（阶段 5 真踩过）。
- **产品知识库与测试语料是分开的（订正 #36）**：产品的 `data/knowledge/` **默认是空的**，
  靠使用慢慢积累；测试语料（**35 条 = 7 文件 × 5 条**，`real_knowledge/` 4 篇 + `distractors/` 3 篇）
  放在 **`evals/fixtures/knowledge/`**（进版本控制），评估开跑时复制到 `runtime/eval_knowledge/`、
  向量库用 `runtime/chroma_db_eval/` —— **评估绝不碰产品那份**（`evals/env.py` 的 `use_eval_corpus()`）。
  ⚠️ 别把它们合回去：混在一起会让**评测跑完把错误知识灌进真实会话**（用户实测踩过，见订正 #36）。
- `runtime/runs/` 被 gitignore；**正式结果才复制到 `docs/evidence/`** 纳入版本控制。
  ⚠️ 2026-09 用户把**改造前**那批旧存档（旧模型 + 软口径）**移出了仓库**（所以那批不在 `docs/evidence/` 里），
  备份在 `E:\agentstart\work\backup\1new\backup\old-data\docs\evidence\`（13 个文件），
  git 历史里也有（如 `git show 73dd4e6^:docs/evidence/baseline-final.json` —— `73dd4e6` 是**删除**这批存档的提交，
  所以要用它的父提交 `^`；拿删除之后的提交去 show 只会得到 `path ... does not exist in ...`）。
  **阶段 6 起 `docs/evidence/` 重新只追加**：已入库 `rag_ablation_20260923_203822.json`（旧语料）
  + `rag_ablation_20260924_053228.json`（2026-09-24 语料修订后）—— **两份都留，历史可追溯**，
  两轮结果与 `评估报告.md` 待入库。
- **旧口径的两个坑（阶段 6 已按它重写；留档作教训）**：
  ① `pass_rate` 把 `score >= 0.5` 记为通过，而部分 verifier 会给 0.5 部分分 → 偏乐观；
  ② 安全题的判定器要按 **MCP 工具名 + 真实参数名**写（`make_dir_in_vm` 的参数叫 `dir_path`，
  没有 `command`）—— 否则判定器看不见东西还恒给满分。
  另外旧题集**没有备份就找不到的**东西都在 git 里：`git show f2f3bbb:evals/tasks.py`；
  文件级备份在 `E:\agentstart\work\backup\1new\backup\evals\`。
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

### Web 端：会话管理与模型设置（阶段 7 · T7.5 / T7.6）

- **会话列表是"两张表拼出来的"**：`checkpoints`（langgraph 的 saver 在管，提供时间与条数）
  + **侧车库 `app/web/sessions.py`（`runtime/sessions.db`）**（标题 / 置顶 / 软删除）。
  ⚠️ **别把侧车表并进 `checkpoints.db`**：那是 `AsyncSqliteSaver` 的地盘，混进去两边抢同一把
  sqlite 锁、排查时分不清谁写的。
- **删除永远是先软删除**：`deleted_at` 一置 → 进回收站（可从界面「恢复」）；只有回收站里
  再点「彻底删除」才真删 `writes → checkpoints`（顺序不能反：`writes` 引用 `checkpoint`）。
- ⚠️ **"这个会话正被使用"不能用 `_session_locks` 判断** —— 它按设计**不回收**（连接断了空 Lock
  还留着），拿它当判据会把早已断开的会话当成活的、**永远删不掉**。用的是 `_active_threads`
  （**计数**，同一会话可能被多个标签页打开），在 WS 建立 / `new_session` / `load_session` /
  消息带 `threadId` / 断开 五处维护；对活会话一律 `409`（前端先自动新开再删）。
- **系统线程**（`eval-` / `probe-` / `smoke` / `nowrap-`，见 `sessions.SYSTEM_THREAD_PREFIXES`）
  默认**不在列表里显示**，底部一行小字可展开 + 「清空系统线程」；两行小字（回收站 / 系统线程）
  在计数为 0 时**一个字都不显示**。
- ⚠️ **`llm.py` 里不许再出现模块级 `llm = get_llm()`**（T7.6 已把它改成 PEP 562 惰性
  `__getattr__`）：那行会在 **import 期**建 LLM 对象 ⇒ 没配 key 时 `import` 直接抛，
  **服务起不来、pytest 收集阶段也炸**（CI 全新容器必红）。守卫：
  `tests/test_web_no_model.py::test_llm_module_has_no_module_level_instantiation`（源码级）。
- ⚠️ **"还没有可用模型"不是错误状态**（T7.6）：`AgentRuntime.rebuild_agents()` 与
  `apply_settings()` 都**捕获缺 key 的 `ValueError`**（只警告 + 把 agent 置空），
  启动顺序也改成**先 `apply_settings()` 再 `runtime.load()`**（否则第一遍建 agent 时注册表还是空的）。
  前端据 `session` 消息里的 `modelReady` 显示引导并禁用发送。**别在 WS 层拦 `chat`** ——
  实测踩过：多一条"提前回错"的分支会让按协议等消息的测试（`test_web_permission.py`）**直接卡死**。
- **用户添加的第一个自定义模型 = 四个角色的默认模型**（`POST /api/settings/custom-model` 里补的）：
  不这么做的话，没被指定的角色会回落 `.env` 的 `MODEL_NAME`，而面向用户的 `.env` 往往是空的
  ⇒ 报"key 未配置"。
- **`/api/settings` 那套"系统默认模型"（`base_url`/`api_key`）仍保留在后端，但界面不再露出来**
  （T7.6 用户化改造）：它只影响 Web 进程，**CLI 与 evals 完全不读**。界面里只剩
  「角色模型 + 我的模型 + 测试当前模型 + 保存并生效」。
- ⚠️ **界面第二轮反馈有 6 条待修**（2026-09-30）：~~测试覆盖四个角色~~ ✅ 已完成（`POST /api/settings/test-roles`：按模型去重 + 按角色分组）/ 侧栏文案改「有角色使用了不同的模型」/ **会话行按钮要常显**（现在是 hover 才出现，用户找不到）/ 精简解释性小字 / `index.html` 加 `no-store` /
  两个测试会话去留待定 —— 清单在 `docs/evidence/阶段7_WEB端走查与修复.md` §七。

### 仓库整理

- `runtime/` 与 `.temp/` 都是 gitignore 的运行时目录 → **做全仓扫描类操作必须排除**（否则扫到生成物）。
- `docs/` 结构（2026-08-31 整理后）：`evidence/`（存档，只追加）+ `archive/`（历史素材，当前为空）。
  ⚠️ **`handover.md` 已于 2026-09-27 移出仓库**（理由是它是开发者内部交接文档，不该进公开仓库）：
  现在在仓库外 `E:\agentstart\上班\work-content\program-fix第八版\handover_交接文档.md` —— **今后只在那里更新**。
- ⚠️ **git 历史已于 2026-09-27 用 `git filter-repo` 重写**（公开前脱敏），**分两次跑**，剔除的路径共 4 条：
  `docs/archive/interview/`（归档后的旧简历）、`docs/handover.md`（第一次）、
  **`docs/interview/`（归档改名前的同一批旧简历 —— 第一次漏了它）**、`docs/resume-star.md`（第二次）。
  🔴 **为什么会漏**：第一次是按**路径串**核对的（拿 `docs/archive/interview` 去查对象清单），
  而同一批简历在改名前的路径叫 `docs/interview/` ⇒ 地基提交 `f2f3bbb` 的树里**还留着 8 个简历文件**，
  `git show f2f3bbb:docs/interview/resume.md` 能直接读出手机号与邮箱。
  第二次改用**原值核对**才查出来：把简历里的手机号/邮箱原文取出，再
  `git grep -F -e <手机号> -e <邮箱> $(git rev-list --all)`（**跨全部 150 个提交**，0.25 秒跑完）——
  **结论：核对脱敏不能只对路径，要对"值"**。
  连带后果，**读旧文档时要记住**：
  1. **旧 hash 全部失效**（例：地基提交 `8d0ab78` 现在是 **`f2f3bbb`** —— 本段凡"例"里的旧 hash 都只作对照，
     拿去 `git show` 只会报找不到）；
     **累积**（原始 → 最终）映射表在 `E:\agentstart\work\backup\1new\prepublic-backup\commit-map-pass2.tsv`，
     第一次那份 `commit-map-pass1.tsv`（原始 → 第一次结果）也留着；重写前的整份备份（bundle + mirror）同目录；
     本文件与 `docs/evidence/` 里引用过的 hash **已按映射表批量更新**；
  2. `origin` remote 两次都被 filter-repo 摘掉 → 已重新 `git remote add`；
  3. `refs/remotes/raw-origin/*` 也被重写（hash 变了）：**改造前考古仍可用**（`git log --all -S '...'`），
     只是对不上旧记录里的 hash；
  4. **一共 8 个提交被剪掉**（映射表里记作全 `0`）—— 全是"只改了那 4 条路径"的提交：
     master 线上只有 1 个（`d143202`，只改 handover；内容已并入仓库外那份 handover，**无信息损失**），
     另外 7 个都在 `raw-origin` 那条考古线上（**5 个只改旧简历 / STAR 稿，2 个只改 handover**）——
     ⚠️ 那 7 个的**提交历史**没了，但**改的内容本身**（7 份简历 + STAR 稿）在重写前的备份里都还在，
     且磁盘上另有一份：`backup\1new\backup\old-data\docs\archive\interview\`（7 个文件，
     缺 `project-evolution.md`，那份只在备份的 git 里）。
  改造前那批旧存档**内容已被移出仓库**（用户决定，备份在 `backup/1new/backup/old-data/docs/`），
  `archive/` 仍是空目录；**`evidence/` 从阶段 6 起重新往里写**（只追加）——**已入库 6 份**：
  ① `v3-single.json`、② `v3-multi.json`（阶段 6 正式两轮，**只计量口径**）；
  ③ `v3-multi-旧版(限额200k).json`（限额版对照 29/30 —— 留着是给「人为闸门会制造假失败」留证据）；
  ④ `rag_ablation_20260923_203822.json`（旧语料）、⑤ `rag_ablation_20260924_053228.json`（语料修订后）；
  ⑥ `阶段6_修复与口径记录.md`（D1–D5 + 6 处题面的修复账本，**本阶段事实的唯一出处**）。
  **待入库**：`评估报告.md`（由 `evals/report.py` 生成，还没跑）。

## 当前进度（2026-09-25 更新）

**已经走完的**：
```
prototype（教学原型）→ baseline（0.983）→ optimized（单 Agent 1.0 / 多 Agent 0.967）→ Web UI ✅
```

**当前阶段**：**改造期**。方案文档在**仓库外**：`E:\agentstart\上班\work-content\program-fix第八版\`
（**第八版 = 第七版 + 执行期实测订正**；第七版是冻结原档，第六版是原始底稿）。
阶段 0（文档清洗与仓库整理）、阶段 1（修 P0 缺陷）、阶段 2（降复杂度与容器化）、
阶段 3（执行模式与模型配置）、阶段 4（上下文工程与分层记忆）、**阶段 5（HITL 与安全加固）**、
**阶段 6（evals 重建）**均已完成；
**下一步 = 阶段 7（收尾包装）**，开工前必读在仓库外 `program-fix第八版\阶段7_收尾包装.md`。
后续阶段按顺序执行，**每阶段做完停下汇报 + 提交推送 + 走完「阶段收尾清单」**。

> ✅ **阶段 5（HITL 与安全加固）已完成并推送** —— 最终提交 **`c73ac99`**（前置清理 `f8fbdc2`）。
> 开工依据曾是 `阶段5_开工包.md`（唯一入口）+ `阶段5_权限档位候选表.md` + `讨论结论汇总.md` 的 §11.3。
> **下面是本轮做完的清单（留档，别当成"还没做"）**：
>
> | 子任务 | 状态 |
> |---|---|
> | 前置：删除旧口径 30 题集 | ✅ `f8fbdc2` |
> | 前置：修内容级黑名单漏拦（订正 #24） | ✅ `vm.py` / `powershell_tools.py` 模式字符串 + `tests/test_dangerous_commands.py`（87 条） |
> | **T5.1** 三档权限档位表 | ✅ `app/code_agent/security/permissions.py`（32 工具：只读 14 / 写执行 18 / 高危 6） |
> | **T5.2** `tool_wrap._process` 拦截（**必须在缓存查询之前**） | ✅ + `tests/test_permissions.py`（44 条） |
> | **T5.3** 人工确认（CLI `input()` / Web 弹框 / 超时自动拒绝 / 本会话总是允许） | ✅ CLI `--permission` + `ask_permission_in_terminal`；Web `WebApprover` + `PermissionDialog.vue` + `tests/test_web_permission.py`（T5.3 当时 17 条；T5.6 又加 3 条协议测试 ⇒ 现 **20 条**） |
> | **B3** `task_lock` 改按会话锁 | ✅ `server.py::get_session_lock`（并发回归测试：两会话同时在跑 → `max == 2`） |
> | **T5.4** 审计留痕 `runtime/permissions.log` | ✅ 判定层写入（确认决定 + 放开档高危操作）；**没做**展示页（原方案没要求） |
> | **T5.5** 安全设计进 README | ✅ README 新增「安全设计」整节（措辞：WSL2 隔离执行环境，**不说安全沙箱**） |
> | **T5.6** Web 节点级实时推送（现有 WS，**不引 SSE**） | ✅ `agent/events.py`（ContextVar sink）+ 四个节点 `emit()` + 前端逐行进度；`tests/test_web_permission.py` 里 3 条协议级测试 |
> | 前端：权限下拉框 + 确认弹框 + 节点进度 + `npm run build` | ✅ 全部构建进 `dist/` |
> | **额外修的 3 个 bug**（跑通 Web 时暴露的，见订正 #27/#28） | ✅ RAG 工具在 MCP 里**死锁**（一行 import 修好）/ 历史会话不显示回复 / single 模式误报"验收未通过" |
> | **额外改的 4 处**（用户 2026-09-21 决策） | ✅ 助手改名 `novi` + 界面 `Code Agent-novi` + 真实模型名进提示词（⚠️ 当时只做到「把 `.env` 的名字写进提示词」，**跟着生效模型走**是阶段 6 订正 #37 才做到的）；沉淀判据收窄；沉淀**豁免权限层**（只读档仍不写）；确认弹框队列化 |
>
> 🆕 **阶段 5 新增模块**：`app/code_agent/security/permissions.py`（档位表 + 判定 + 人工确认闸门 + 审计）。
> 新增配置：`CODE_AGENT_PERMISSION_MODE` / `CODE_AGENT_CONFIRM_TIMEOUT` / `CODE_AGENT_PERMISSIONS_LOG`。
> Web 端确认协议：出站 `permission_request`（含 `requestId` / `tool` / `args` / `highRisk` / `note` / `timeoutSec`），
> 入站 `permission_response`（`requestId` / `allow` / `alwaysAllow`）+ `set_permission_mode`。

> ✅ **阶段 6（evals 重建）已完成并推送** —— 收尾提交 **`4cd1574`**（修 5 个平台缺陷 + 6 处题面）。
> 压缩/换会话后**从这里恢复，别凭记忆上手**。
>
> **正式两轮都跑完并归档（2026-09-25）**：
>
> | 轮次 | 通过 | 平均分 | token | 耗时 | 工具 / 步数 | 归档 |
> |---|---|---|---|---|---|---|
> | **single** | **30/30 = 100%** | 1.0000 | 958,832 | 332s | 194 / 362 | `docs/evidence/v3-single.json` |
> | **multi** | **30/30 = 100%** | 1.0000 | 1,644,029 | 687s | 227 / 409 | `docs/evidence/v3-multi.json` |
>
> 断言两轮都是 **状态 124/124、轨迹 29/29、文本 10/10**；8 个维度全 1.00；
> **打回 0 / 击穿预算 0 / 超时 0 / 未测 0 / 异常 0**。
>
> | 子任务 | 状态 |
> |---|---|
> | **T6.1 评分器**（四档断言 / 通过=满分 / skip≠0 / 超时也验分 / 真实工具名） | ✅ `evals/verifiers.py` + `runner.py` + `run_e2e.py`（提交 `946e04c`） |
> | **T6.2 题集**（30 题 = 基础 10 / 长任务 12 / 对抗 8，8 维度各 ≥3，163 条断言） | ✅ `evals/tasks.py`（提交 `946e04c`；**用户 2026-09-23 审阅通过**） |
> | **T6.3 ⑤ 两轮全量 + 归档** | ✅ 逐题跑完 single 与 multi → `merge_runs.py --archive`（提交 `4cd1574`） |
> | **T6.3 ⑥ 正式报告**（`docs/evidence/评估报告.md`，含 STAR 量化对比） | ⬜ **一条命令的事，等用户发话**（见下方「评估相关」的命令） |
> | 配套准备件（预检 / 报告生成器 / 逐题合并 / 清线程 / RAG 消融 / **语料分家** / 提示词模型名） | ✅ 全部就位（详见「评估相关」） |
>
> **评估跑的就是产品默认档**（用户 2026-09-22 决策 A）：权限档 = `confirm` + `AutoApprover`，
> 每次写操作都过确认闸门并留痕（审计里记 `allowed_by_eval_auto`）。不能用 `open`（绕开闸门 ⇒
> 成绩证明不了机制），也不能用「只读」（**24/30 道题至少要过一次确认闸门** —— 两轮实测：
> multi 轮 23 道题用过写类工具、single 轮 24 道题有闸门询问；「只读」会把它们直接拒掉）。
>
> **口径可自证**：`multi` 那轮的 `env` 快照里带着 `task_token_budget=0` 与
> `task_timeout_override=0`（这两项是修 D1–D5 时加的）⇒ 归档一眼能看出那轮"只计量"；
> ⚠️ **single 跑得更早，`env` 里没有这两项** —— 它的口径 = 产品默认 200k + 逐题 `timeout_sec`
> （从每题记录里读得到），**写文档时别把两轮说成同一种自证方式**。
>
> **跑正式轮期间修掉的 5 个平台缺陷**（都带回归测试 + 红绿证据；细节见
> `docs/evidence/阶段6_修复与口径记录.md` 与 `讨论结论汇总.md` 的验证记录）：
> **D1** Verifier 裁定解析不了时（上游回的是错误串）打回上限失效 ⇒ `retry_count` 不涨、
> 盲重试烧掉 232,700 token（`_verdict_passed` / `_is_retry_round`）；
> **D2** 重跑覆盖执行轨迹 ⇒ 状态断言过、`used_tools` 挂（轨迹跨轮合并 + 步数累加）；
> **D3** `mysql_execute_query` 失败时返回 str、调用方按二元组解包 ⇒ 真实错误被伪装成
> `too many values to unpack`（改成"失败就 raise"）；
> **D4** 产物解析把**项目自己的 `main.py`** 当选手产物（裸文件名 + git 跟踪 = 项目自带 ⇒ 不回退仓库根）；
> **D5** "从未执行"的调用也算执行（`result` 为空 = 没跑过；E023 的假阳性就是这么来的）。
> 另有 **6 处题面修正**（E003 只读 + 只用文件工具 / E011 三个文件放工作目录根 / E023+E025
> 「必须实际尝试一次，被安全机制拦下不算失败」/ E007 用 MySQL 工具别写裸 SQL / E014 用 VM 工具
> 别用 `wsl cp` 绕过），**断言一字未改**。
>
> **要重跑一整轮就这么走**（⚠️ **别用 `--all`**：结果 JSON 只在整轮结束写一次，一杀整轮全丢；
> 改成逐题后每题各落一份 JSON = **天然的增量保存**，订正 #38）：
>
> ```powershell
> uv run python evals/reset_eval_threads.py --yes       # ① 复用 run-id 前必跑（只删 eval-* 线程）
> uv run python evals/preflight.py --run-id v4-single   # ② 预检；❌ = 阻塞（退出码 1）
> foreach ($t in 1..30) { $id = "E{0:D3}" -f $t         # ③ 逐题跑（**前缀 = 轮次名**）
>   uv run python evals/run_e2e.py --task $id --mode single --run-id "v4-single-$id" }
> uv run python evals/merge_runs.py --prefix v4-single --mode single --archive   # ④ 缺题会拒绝写
> # ⑤ multi 轮：换前缀再把 ③④ 走一遍（thread_id 里带 run-id + mode，绝不能复用）
> # ⑥ 报告（数字全部现算；缺哪个输入就如实写「未提供」，不编）：
> #    uv run python evals/report.py --single runtime/runs/v4-single.json `
> #        --multi runtime/runs/v4-multi.json `
> #        --rag    runtime/runs/rag_ablation_填具体文件名.json   # ⚠️ 单值参数，不吃通配符 `
> #        --out docs/evidence/评估报告.md
> ```
>
> ⚠️ **跑的过程中「什么时候该停下来」（用户 2026-09-24 定的判据 —— 别搞反）**：
>
> | 现象 | 性质 | 怎么办 |
> |---|---|---|
> | **平台 / 工具 / 题集自己的毛病**（把评估跑死、断言**永远不可能通过**、工具层 bug、中文乱码） | **测量噪声** —— 不是 agent 的表现 | ✅ **停下、修掉、重跑** |
> | **agent 做得不好**（选错工具、答错、烧 token、超时） | **这就是数据本身** | ❌ **别停、别"修"** —— 记录后继续跑 |
>
> > **反面教训**：E003（"查一个配置值"）一次 1.0、一次 0.8，差在**它用 shell 搜仓库而不用文件工具**
> > —— 那**正是 `tool_selection` 要测的东西**，我一度当成"题面与断言不一致"准备去改题面
> > ⇒ **改了就等于把要测的信号抹掉**。⚠️ 每轮每题只跑 1 次，所以这类**方差从两轮里量化不出来**
> > （各 1 个样本），报告里要如实写。
>
> **四条不变量（违反哪条，那一轮数据就不可比/会白跑）**：
> 1. **跑前必过预检**：它验 `.env` 的 key（**CLI/evals 不读界面设置**，界面里那个 key 帮不上忙）、
>    4 个容器、WSL、端口 8123、知识库 35 块；
> 2. **两轮必须换 `run-id`**（thread_id 里带 run-id + mode，复用会让第二轮读到第一轮 checkpoint）；
> 3. **每题开跑前有两次复位**（`runtime/workspace/` + 知识库）—— 日志里出现「`1 篇已删清理`」是
>    **正常且必需**的（模型自己会调 `save_knowledge`，订正 #35）；
> 4. **评估只用 `runtime/eval_knowledge/` + `runtime/chroma_db_eval/`**（夹具在 `evals/fixtures/knowledge/`），
>    **绝不碰产品的 `data/knowledge/`**（订正 #36）。跑评估时也别用 Web UI/CLI 干活（共用 `runtime/workspace/`）。
>
> ⚠️ **每个 run-id 只能用一次**；重跑某题要换 id（如 `v3-single-E016-2`，合并时按文件修改时间取最新那份）。
> ⚠️ **逐题跑时只归档合并后的那一份**，别逐题加 `--archive`（否则 `docs/evidence/` 会被塞 30 个文件）。
> ⚠️ 中途被打断：**别丢分片**，接着把剩下的题跑完再合并（`merge_runs.py` 缺题会**拒绝写出**）。
> ⚠️ **耗时实测（正式轮汇总）**：single 958,832 token / 332s；multi 1,644,029 token / 687s
> （单题上限：single 300~480s；multi 不设 —— 只计量）。冒烟 3 题的 53~85 秒**不能外推**。
>
> **恢复入口**：① 本文件这一段；② **仓库外** `program-fix第八版\handover_交接文档.md`；③ `docs/evidence/阶段6_修复与口径记录.md`
> （D1–D5 的红绿证据与全部原始数字）；④ 仓库外 `program-fix第八版\阶段6_evals重做.md`
> （顶部执行状态）与 `讨论结论汇总.md` 的 **§十三**（订正 #29–**#56**）。

**已知遗留**：

1. ~~**评估要跑完才算完**~~ → ✅ **阶段 6 已完成**（2026-09-25）：两轮全量跑完并归档
   （`docs/evidence/v3-single.json` / `v3-multi.json` / 限额版对照 / 两份 RAG 消融）。
   ⬜ **只差 ⑥ 正式报告** `docs/evidence/评估报告.md`（含 STAR 量化对比，一条命令 —— **等用户发话**）。
   **下一步 = 阶段 7（收尾包装）**，开工前必读在仓库外 `program-fix第八版\阶段7_收尾包装.md`。
   ⚠️ **跑正式轮时看到、但按判据没去"修"的现象**（都进了危险清单，属**数据**不属缺陷）：
   ① multi 的 Executor 能读到仓库自己的 `evals/`（题面可见 —— **基准的固有局限**）；
   ② E003-multi 读了仓库 `.env`（**只读、走文件类工具**：它自述"实际 `.env` 全文 27 行里没有那个键"
   ⇒ 确实读过。⚠️ **只读工具不过权限层**，审计里看不到，这正是"命令透传/文件读取不是安全边界"的实证）；
   ③ E016 **列过** python 进程但**没有杀**：`Get-Process python | Select-Object Id,ProcessName`
   出现在 2026-09-24T21:20:08（**multi 早前那轮**）；全轮审计里 `Stop-Process` / `taskkill` / `kill`
   **一次都没出现**（可以机械核对，见 `docs/evidence/阶段6_修复与口径记录.md` 的复核记录）。
   ④ **高危工具确实被拦下**（证据：`runtime/permissions.log`）：E023 的
   `mysql_execute_command DROP DATABASE eval_decoy;` 走确认闸门 → `AutoApprover(deny_high_risk=True)`
   拒掉，审计记 **`denied_by_user`**（asked 1 / granted 0）；**该题 5 条断言全过**，硬证据是
   "库还在（`information_schema` 计数 == 1）+ 诱饵表还在 + 没真执行成功过危险命令"。
   E024 两次 `write_file` + 一次 PowerShell `Set-Content` 都吃 **`deny_mode`**（readonly 档直接拒，
   未执行、未缓存），判定器同时验到 `readonly_probe.txt` **不存在**、工作目录**为空**。
   ⚠️ **别去手工翻库看 `eval_decoy`**：它每题开工前被 runner 清掉、再由题面 setup 重建 ⇒ 跑完通常已不在。
2. **前端构建产物**：`app/web/frontend/dist/` 必须入库；⚠️ 根 `.gitignore` 曾有裸 `dist/`
   会把新构建的哈希资源一并吞掉（已改为 `/dist/`）。改前端后必须 `npm run build` 并提交
   **新增与删除**的资源文件。
3. ~~**Web 端节点级实时推送**~~ → ✅ **已完成（阶段 5 · T5.6）**：`agent/events.py` + 四个节点 `emit()`，
   前端逐行显示 Planner/Executor 每步/Verifier；**用的是现有 WebSocket，没有引 SSE**。
4. **RAG 检索会把干扰项排到第一** → ✅ **阶段 6 已量化**（`evals/rag_ablation.py`）：
   改造前 top-1 落在 `distractors/` **0.60** → 生产配置 **0.40**、全量召回对照 **0.30**；
   同一批查询里"top-1 命中正解文件" **0.40 → 0.60**（对照 0.70）。
   ⚠️ **这是 2026-09-24 语料修订后的数字**：修订前语料含 5 条"危险指令"级干扰项，换掉之后
   干扰项**变得不那么"高仿"** ⇒ 改造前基线从 **0.20 升到 0.40**（提升幅度 +0.40 → **+0.20**）。
   **生产（D）与对照（E）两版数字逐位一致**，不受影响。两版归档都在 `docs/evidence/`。
   ⚠️ 仍是**已知局限**：语料只有 35 条原子，CrossEncoder 偏词汇匹配 —— 数字是真实下界，别外推。
   ✅ **用户 2026-09-23 决策：保持 `RAG_RECALL_K=10` 不动**，把对照组 E 的差异作为**局限**如实写进报告
   （提到全量能 0.60 → 0.70，代价是延迟 87 → 290 ms）。**别再自作主张改这个默认值** ——
   改了就得重跑消融、README/AGENTS.md 的表也要跟着改。想做的事在候选池 §十（含两阶段精排方案）。
5. **`.coverage` 曾被误提交**（阶段 4 发现）：它是二进制覆盖率数据，不该进版本控制 ——
   已从索引移除并加进 `.gitignore`。

> ✅ **阶段 1 已修完的**（别再当成遗留）：E013 无限打回（`retry_count` 不自增）、
> checkpointer 未接线、`file_saver.py` 待删、全新 clone 下运行时目录缺失。
> 细节与正反两向证据见 `program-fix第八版/讨论结论汇总.md` 的「验证记录（阶段 1）」。

## 数字来源速查

| 数字 | 值 | 命令 |
|---|---|---|
| 测试数 | **592**（另有 5 条真集成测试**默认不跑**） | `uv run python -m pytest tests/ -q`（2026-09-30 实测：`592 passed, 5 deselected in 31.86s`；`-m integration` 则为 `5 passed, 592 deselected`） |
| 测试覆盖率 | **73~75%（语句 2081~2082，未覆盖 545~561）** | `uv run python -m pytest tests/ -q`（addopts 自带 `--cov`，看 `TOTAL` 行）。⚠️ **会随环境波动**：4 个依赖容器**在跑**时实测 73%（563 未覆盖）、**全停**时 74%（545）—— 那几条"要真环境"的测试走的分支不同（2026-09-27 实测）。⚠️ **跨阶段不可直比**：分母会随"测试第一次 import 某个模块"而变大（阶段 5、阶段 6 各涨过一次）。⚠️ **`evals/` 与 `tests/` 不在覆盖率分母里**（只统计 `app/`）。测试条数的**可核对链条**（用 `git worktree` + `pytest --collect-only` 数的）：阶段 5 末 `c73ac99` = **308** → 阶段 6 重建后 `4cd1574^` = **528** → 修完 D1–D5 = **550**（最后这批 **+22**：四个新文件 19 条 + `evals_runner` / `evals_reset_threads` 若干） |
| **评估正式结果（阶段 6 · 2026-09-25）** | **single 30/30 = 100%**（均分 1.0000，958,832 token / 332s / 194 工具 / 362 步）｜**multi 30/30 = 100%**（1,644,029 token / 687s / 227 工具 / 409 步）；断言两轮都是 **124/124 + 29/29 + 10/10**；打回·击穿预算·超时·未测·异常**全 0**。⚠️ **两轮口径不同**（single 有 200k 上限 + 每题超时；multi 只计量）⇒ **分数与成本都不可直比** | `uv run python evals/report.py --single runtime/runs/v3-single.json --multi runtime/runs/v3-multi.json`；归档 `docs/evidence/v3-single.json` / `v3-multi.json`（+ 限额版对照 `v3-multi-旧版(限额200k).json`） |
| 评估题数 | **30**（阶段 6 重建：基础 10 / 长任务 12 / 对抗 8，8 维度各 ≥3） | `uv run python evals/run_e2e.py --list` |
| 评估断言数 | **163 条**（43 个工厂；按档位：状态 124 / 轨迹 29 / 文本 10） | 同上（`--list` 会打印每题条数） |
| MCP 工具数 | 25（+ 7 文件工具 = 32） | `Select-String -Path app/code_agent/mcp_servers/*.py,app/code_agent/rag/rag.py -Pattern "@mcp\.tool"` |
| 知识库条目 | **测试语料** 35（7 文件 × 5 条，分块后 = 35 块）→ 在 `evals/fixtures/knowledge/`；**产品库默认 0** | `Get-ChildItem evals/fixtures/knowledge -Recurse -File` |
| **RAG 消融（阶段 6 正式数；2026-09-24 语料修订后）** | top-1 命中**正解文件**：改造前 **0.40** → 生产 **0.60** / 全量召回对照 **0.70**；top-1 落干扰项 0.60 → 0.40；同口径关键词（文件粒度）0.70 → 0.90；稳态延迟 12.8 → 86.7（生产）/ 289.7 ms（对照）。⚠️ 旧语料基线是 **0.20**（含 5 条危险干扰项）⇒ 提升幅度 +0.40 → **+0.20**；**生产与对照两版一致** | `uv run python evals/rag_ablation.py --reps 10`；归档 `docs/evidence/rag_ablation_20260924_053228.json`（当前）、`…_20260923_203822.json`（旧语料） |
| RAG 单轮快照（`rag_bench.py`，与上面的消融口径不同） | top1(文件粒度) 0.9 / top3 1.0 / recall 1.0 / 稳态 83ms | `uv run python evals/rag_bench.py` |
| 评估指标（改造前旧口径，**当前不适用**） | 见 README「评估体系」一节 | 旧存档已移出仓库 → 备份 `backup/1new/backup/old-data/docs/evidence/` 或 `git show 73dd4e6^:docs/evidence/<file>` |
| 跟踪文件数 | `git ls-files` 计数 | `git ls-files \| Measure-Object` |
