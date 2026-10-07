# AI Agent Test — LangGraph + MCP Code Agent

> **给谁看**：**AI 编码助手**（Claude / Cursor / Copilot / 其他）。
> 人类读者看 [`README.md`](README.md)；**设计取舍与代价**看 [`docs/architecture.md`](docs/architecture.md)；
> **历史过程（冻结）**看 [`docs/archive/`](docs/archive/)。
> 本文件是**操作手册**：约定 / 命令 / 已知坑 / 当前状态。**只写"现在该怎么做"，不写叙事。**

## 接活先看

**当前状态（2026-10-07）**：

| 项 | 值 |
|---|---|
| 阶段 | **阶段 8（探索测试驱动的稳健性修复）进行中** —— 阶段 0–7 已完成收口；**P0 已落地并做完真机验收**（停止 + 墙钟 + 停止按钮 + 任务状态条；真机撞出的 F1/F2 也已修）· **P1 已落地**（终止文案统一 + §十五A 三条提问原则 + 顺手修的 F3） |
| 最近提交 | `git log --oneline -1`（别在文档里写死 hash —— 提交一次就过期）|
| 远端 | `origin` = Gitee（镜像）· `github` = GitHub（主仓）；**tag `v1.0.0` 两边都有** |
| CI | **passing**（`.github/workflows/ci.yml`）；跑 `ruff check` → `ruff format --check` → `pytest tests/ -v`，**不需要 `.env`** |
| 测试 | **665** 条通过（另有 5 条真集成测试**默认不跑**）；覆盖率 **77%** |
| 评估 | **single 30/30 · multi 30/30**（平均分 1.0000，163 条断言）→ 报告 `docs/evidence/评估报告.md` |
| 仓库可见性 | **public**（2026-10-03 定：作为对招聘方展示的入口） |

**待办（当前真正未做的）**：

1. **阶段 8 修复（进行中）** —— **P0 ✅ 已落地 + 真机验收通过**（协作式停止 + 墙钟 15 分钟 + 界面停止按钮 + 任务状态条；
   现场撞出的 **F1**「弹框遮罩挡住停止按钮」与 **F2**「卡片步数跨轮累积」也都已修，见修复账「真机验收」一节）·
   **P1 ✅ 已落地**（预算终止的固定中文说明 + R6 的"见上方轨迹"已删 + §十五A 三条提问原则；顺手修了 F3「终止那轮的重跑不计次」与"重跑撞预算被包装成验收失败"）·
   **P1.5** 入口"模板未渲染"检测 · **P2** token 预算放宽 + 按模型窗口自动算阈值。
   账本（现象→证据→根因→待修）：`docs/records/2026-10-07_探索测试第1-2轮与问题账.md`；
   修复账（做了什么→证据）：`docs/records/2026-10-07_阶段8修复账.md`。
   **计划书**（要做什么 / 验收标准 / 执行顺序）在**仓库外**的项目档案目录（开发者本地维护）里的 `阶段8_探索测试与稳健性修复.md`。
2. **探索测试 12 题**：**第 1 题 ✅ 通过**；**第 2 题 ⚠️ 产物正确但过程失控**（30 分钟 / 56 步 / 20.8 万 token / 15 次弹框，4 个问题见上行）；
   **第 3~12 题待修完再跑**（P0 之后要先清产物、重跑第 1、2 题做端到端人工验收）。
3. 本机 `.temp/` 有约 100 MB 临时产物（**不入库**）—— 待清理。
4. 后续不在本项目内：JD 分析 → 知识补课 → 面试追问演练；**项目 #2**（部署 / CI-CD 那条线）。

> 已收口（2026-10-06）：**CI 截图存档** → `docs/evidence/CI运行成功-2026-10-06.png`；
> 两份过程账的**口吻轻整理**（称谓层面，6 处）；`docs/evidence/README.md` 已写明本目录规则。

> 历史待办（`handover` 的 5 条：E013 无限打回 / checkpointer 未接线 / `file_saver.py` 待删 / 全新 clone 缺运行时目录 / 评估体系重做）**已全部关闭**，证据见 `docs/archive/handover-2026-10-03.md`。

### 文档导航

| 文档 | 管什么 | 什么时候读 |
|---|---|---|
| [`README.md`](README.md) | **怎么用**（安装 / 跑起来 / 评估结果 / 已知边界） | 使用者、面试官先读 |
| [`docs/architecture.md`](docs/architecture.md) | **设计取舍与代价**（6 条 ADR） | 改架构、或要回答"为什么这么选" |
| 本文件 | **操作手册**（约定 / 命令 / 坑 / 当前状态） | 动手前必读 |
| [`docs/archive/`](docs/archive/) | **历史冻结快照**（不更新） | 想知道"当时怎么做的" |
| [`docs/records/`](docs/records/) | **过程账**（修复/缺陷账，可编辑） | 想知道"某个坑怎么修的、证据在哪" |

### 考古与备份

- **改造前的历史不在 `master` 上**：地基提交是**单提交重建**的 ⇒ 要用 `git log --all -S '<关键词>'` 去 `refs/remotes/raw-origin/*` 挖。
- **git 历史 2026-09-27 用 `filter-repo` 重写两次**（公开前脱敏）⇒ **旧 hash 全部失效**；映射表与重写前的整份备份都在**本机备份（未入库）**。
- 🔴 **`raw-origin` 对应的远端是私有仓**（改造前的历史，**含已失效的旧 key**）⇒ **不要把它公开，也绝不要把它的 refs 推给主仓**（例如 `git push github 'refs/remotes/raw-origin/*'` 这类操作禁止）。
  2026-10-06 实测：主仓（GitHub/Gitee）**不含**这些提交（查那个 SHA 返回 422）、`code_agent_raw` 匿名访问返回 **403** ✓。
- 项目过程档案（阶段方案 / 讨论结论汇总 / 交接文档）在**仓库外的项目档案目录（开发者本地维护）**；仓库内只保留冻结快照 `docs/archive/handover-*.md`。

## 项目一句话与定位

Python 3.13 的本地多 Agent 编程助手：LangGraph StateGraph（Planner → Executor → Verifier）
+ 6 个自建 MCP Server（stdio 子进程，25 个工具）+ FileManagementToolkit（7 个文件工具），
双入口（CLI `main.py` / Web UI `app/web/server.py`）。

**定位 = 本机单用户**（2026-09-30 定）：**不做公网部署、不做多用户账号** —— 它会执行 shell / 读写主机文件，
公网开放等于给陌生人一个远程代码执行入口；**部署与 CI/CD 留给第二个项目**。对外展示 = **GitHub 主仓库 + Gitee 镜像**。
⚠️ **改造前那批旧分数（baseline 0.983 / optimized 1.0 / 0.967）一律不可比**：旧题集已删、口径已换；
当前成绩只看**阶段 6 重建后的两轮**（见「评估」）。

## 必须遵守的约定

| # | 约定 |
|---|---|
| 1 | 包管理用 **uv**：`uv sync` / `uv add` / `uv run python`，**绝不用 pip** |
| 2 | **MCP server 的日志必须走 stderr**（stdout 是 JSON-RPC 通道，任何 print 都会污染协议） |
| 3 | 配置集中在 `app/code_agent/config.py`（`.env` + 默认值）；新增配置项要同步 `.env.example` |
| 4 | 行尾交给 `.gitattributes`（`* text=auto`）：**索引里已经是 LF**，不要手动转换 |
| 5 | 用户可见字符串用**中文** |
| 6 | 改动**前后都要留证据**（测试输出 / 结果文件 / 复现命令） |
| 7 | **不许为了让测试通过而降低断言强度** |
| 8 | 文档与代码不符时，**以代码为准**，并在回复里明确指出 |
| 9 | 文档里的每个**行号 / 文件名 / 数字都必须现场核对**，禁止凭印象写 |
| 10 | 新增依赖必须 `uv add`，并把"装什么、为什么装"写进对应文档 |
| 11 | 改前端后必须 `npm run build` 并提交 `dist/`（含被删除的旧 hash 文件） |
| 12 | **命名是"有意两套"的，别去统一**：助手**自称** `novi`（`PROMPT_CONTEXT["name"]`）、**界面产品名** `Code Agent-novi`；而 `README.md` / 本文件 / `docs/` 里的**项目名保持 `Code Agent`**（2026-09-21 定：只改界面） |
| 13 | **仓库内文档不写本机绝对路径**；要提仓库外的东西 → 写「仓库外的项目档案目录（开发者本地维护）」+ 指向仓库内冻结快照。检查命令见下方代码块（`docs/evidence/*.json` 原始证据除外） |

```bash
# 约定 13 的机械检查（**必须为空输出**）：只查『个人标识』，不查通用路径。
# 允许的通用写法：C:\ / D:\code\… / E:\… 这类示例；
# 刻意保留：docs/archive/ 与 docs/evidence/（原始记录，改了就不是证据了）。
git grep -nE "agents[t]art|lepr[i]te" -- AGENTS.md README.md docs scripts tests app ':(exclude)docs/archive/*' ':(exclude)docs/evidence/*'
```

## 常用命令

| 用途 | 命令 |
|---|---|
| 装依赖 | `uv sync` |
| 跑 Agent（REPL） | `uv run python main.py` |
| 指定会话 / 新会话 | `uv run python main.py --thread-id x` / `--new-session` |
| 指定权限模式 | `uv run python main.py --permission readonly`（readonly / confirm（默认）/ open） |
| 起 Web UI | `uv run uvicorn app.web.server:app --port 8000` |
| 一键起 Web UI（**前台**跑；`-Dev` 另开窗口跑热更新） | `.\scripts\run\start-app.ps1`（或双击 `scripts\run\start-app.cmd`；换端口 `-Port 8001`） |
| 单元 + 工具级测试 | `uv run python -m pytest tests/ -v`（**665 个**） |
| **真集成测试**（要真 MySQL / WSL / Redis / SearXNG；**只能在 Windows 本机跑**，CI 没有 WSL；默认不跑） | `uv run python -m pytest -m integration -v`（5 条） |
| **装 / 查 RAG 的本地模型**（不在仓库里，各 ≈87MB） | `uv run python scripts/fetch_models.py`（`--dry-run` 只看状态；`--source modelscope` 换通道） |
| **跑评估前先预检**（容器 / WSL / `.env` key / 端口 / 知识库，**不修任何东西**） | `uv run python evals/preflight.py --run-id v4-single` |
| **复用同名 run-id 前必跑**（清库里的 eval 线程；默认只报告，`--yes` 才删） | `uv run python evals/reset_eval_threads.py --yes` |
| 看评估题集（**不跑、不烧 token**） | `uv run python evals/run_e2e.py --list` |
| 跑评估（**逐题跑**，必须换 run-id / 前缀） | `uv run python evals/run_e2e.py --task E001 --mode single --run-id v4-single-E001` |
| 逐题跑完合并成一轮（缺题会拒绝写出） | `uv run python evals/merge_runs.py --prefix v4-single --mode single --archive` |
| 出评估报告（数字全部现算） | `uv run python evals/report.py --single docs/evidence/v3-single.json --multi docs/evidence/v3-multi.json --rag docs/evidence/rag_ablation_20260924_053228.json --out docs/evidence/评估报告.md`（⚠️ 三个参数都是**单值**、**不吃通配符**） |
| 报告生成器自测（**用假数据**） | `uv run python evals/report.py --selftest` |
| MCP server 探针（排查"工具调不通"） | `uv run python scripts/probe_mcp_server.py rag query_rag --args '{"query":"MCP"}'` |
| RAG 基准 / **消融对照**（**不用 LLM**） | `uv run python evals/rag_bench.py` / `uv run python evals/rag_ablation.py --reps 10 --archive` |
| 重建前端 | `cd app/web/frontend && npm run build` |

## 已知坑

> 写法：**症状 → 原因 → 怎么办**。每条都要能直接执行，不写"当时我们…"。

### 架构事实（别按老印象理解）

- **跨轮记忆已接线**：`AsyncSqliteSaver` + `runtime/checkpoints.db`；`AgentState.messages` 用 `add_messages` 累积；每轮由图跑完后追加一对 (任务, 回复)。
- **`thread_id` 三条路径都必须传**：CLI（`run_agent`）/ Web（`server.py`）/ 非交互入口（`run_single_task`）。漏传直接报错。
- **执行模式** `single` / `multi` / `auto`：`auto` 先由 `route_node` 判复杂度（simple 只跑 Executor，complex 走完整三阶段）；CLI `--mode` 与 Web 下拉框都能选。
- **Verifier 打回上限**：`retry_count` 只在"重跑那一次" +1 ⇒ 最多打回 `MAX_RETRY`(2) 次，Executor 共跑 3 次。
- **模型按角色配**：注册表 `config/models.json`（**默认空** ⇒ 四个角色走 `.env` 的 `MODEL_NAME`，即界面里的「使用配置默认」）；`get_llm(role)`。⚠️ **测试里必须同时 patch `ma.get_llm` 与 `ma.registry`**，否则 planner 会真调模型（实测让 pytest 从 8s 变 104s）。
- **三处配置的优先级**：`runtime/web-settings.json`（界面点的）**>** `config/models.json` **>** `.env`。⚠️ **CLI 与 evals 根本不读 `web-settings.json`** ⇒ 跑评估前必须确认 `.env` 的 `MODEL_API_KEY` 有效（界面里那把 key 帮不上忙）。
- **提示词里的模型名必须运行期取**（订正 #37）：`prompts.PROMPT_CONTEXT` **故意不含 `model_name`**，一律走 `prompt_context(role)` → 取**实际调用名**。拿静态 `PROMPT_CONTEXT` 去 format 会 `KeyError: 'model_name'` —— **这是故意的**：宁可响亮失败，别静默报错名字。
- **显示名 ≠ 调用名**：`models.json` 的键是给用户看的**显示名**，`model` 字段才是发给 API 的名字（官方改名时只动一行）。

### 环境与工具链

- **MCP 工具的接口形状**：适配层生成的是 `StructuredTool(coroutine=…)`，**没有 `func`** ⇒ 包装工具**不能用 `tool._run`**；FileManagementToolkit 的 7 个工具**只有同步 `_run`**（pydantic 模型，塞 `coroutine` 会 `ValueError: … has no field "coroutine"`，要换成同名 `StructuredTool` 代理）。所以 `wrap_tools()` **必须接返回值**。
- **运行时目录**由 `config.py` 创建（全新 clone 不会因目录缺失失败）。⚠️ `runtime/checkpoint/`（单数）已彻底移除，并留了"不存在"的断言防回归。
- **依赖服务**：`agent-mysql` / `searxng` / `redis-stack-server` 由仓库根 `docker-compose.yml` 管理（`name: code-agent-deps`）；**`my-nginx` 归 WSL 里的 `~/nginx`**（挂载源是 WSL 路径，搬到 Windows 侧 compose 会**静默挂空目录**）。一键脚本 `scripts/run/start-deps.ps1` / `stop-deps.ps1`；4 个容器都 `restart: unless-stopped`。
- ⚠️ **WSL 那份 nginx compose 不要用「单文件挂载」**：跨发行版单文件挂载会失败**且故障固化**（暂存位按源路径 hash 命名，重启 Docker 也没用）⇒ 只挂目录 + 自定义配置写 `conf/conf.d/*.conf`；已踩过就 `docker compose up -d --force-recreate`。
- ⚠️ **`powershell_tools.py` 的三个缺陷已修（订正 #39）**：旧写法 `shell=True` + 列表 ⇒ 实际走 `cmd.exe /c powershell -Command "<整条>"` ⇒ ①**多行命令在第一个换行处被截断**（还返回"成功但没输出"）②命令里的 `&` 被 cmd 当分隔符 ③固定 `encoding="gbk"` 与子进程的 UTF-8 混流 ⇒ 乱码。**修法**：`shell=False` + **逐行解码** + stderr 回显。
- 🔴 **逐行解码的判据要两条**：只写"先 UTF-8、失败退 GBK"不够 —— **GBK 字节有时恰好是合法 UTF-8**（`目录` 的 `C4 BF BC` 解成 `Ŀ¼`）⇒ 必须再加"**解出的字符落在 `U+0080–U+02FF` 就改用 GBK**"。已知假阳性（刻意接受）：真 UTF-8 拉丁文本（`café`）会被按 GBK 解成 `caf茅`。回归测试 `tests/test_powershell_exec.py`（**3 条必须真起子进程**，因为破坏发生在 cmd.exe 解析阶段）。
- **搜索不依赖浏览器**：`browser_tools.py` 只调 SearXNG JSON API（文件名是历史遗留）；Selenium / Edge / msedgedriver 都不需要。

### 上下文工程与分层记忆（阶段 4，都是实测结论）

- **工具结果外置**：≥6000 字符或 150 行 ⇒ 落盘 `runtime/tool_results/`，上下文只留预览 + 路径。⚠️ **`read_file` / `read_file_range` 在豁免名单，不许外置** —— 实测同一道"读全文并总结"：外置后模型看不见内容、改用分段读绕过，**token 从 17,361 涨到 127,071（7.3 倍）**。读 `tool_results/` 里的文件**不再外置**（同内容同 hash ⇒ 死循环）。
- **对话压实**：历史超 `COMPACT_THRESHOLD=6000` token ⇒ 最老一段压成四段式摘要；**摘要失败就原样保留**（省 token 不能把历史弄丢）。
  ⚠️ **三项都可用 `.env` 覆盖**（不设就走 `config.py` 的默认值 6000 / 30000 / 200000）：
  `CODE_AGENT_COMPACT_THRESHOLD`（压实，长会话可加大到 12000~32000）· `CODE_AGENT_NODE_TOKEN_BUDGET`
  （单次调用输入上限，**不能超过模型窗口**）· `CODE_AGENT_TASK_TOKEN_BUDGET`（单任务成本保险丝）。
  **改完必须重启进程**（`.env` 只在 import 时读一次）。
- **token 预算**：`NODE_TOKEN_BUDGET=30000` 剪枝、`TASK_TOKEN_BUDGET=200000` 硬终止。⚠️ 任务级必须在 **executor 的 ReAct 循环内部逐步判** —— 只在节点入口判，一次"读大文件 + 反复重读"能在**单个节点调用**里烧掉十几万 token。
- **只读缓存**默认**不收** `mysql_execute_query`（只读但结果会变）；任何写操作后**清空本会话缓存**；Redis 挂了静默降级。
- 🔴 **三条入口都必须接工具包装层**（权限 → 外置 → 缓存）：CLI（`run_agent`）/ evals（`run_single_task`）/ **Web（`AgentRuntime.load()`）**。漏一条，那条入口就没有外置、没有缓存、也没有权限层。
- **自动注入 / 自动沉淀走进程内** `rag/store.py`（**不走 MCP**：每次调用都要新起子进程 import chromadb + torch，毫秒级变秒级）。评估里 `auto_inject=False` + `auto_deposit=False`，**且每题开跑前复位知识库**（模型自己会调 `save_knowledge` 写进去，这条路关不掉）。⚠️ 复位**不能调 `ensure_seeded()`**（进程级 `_seeded` 标志 ⇒ 第二次直接跳过 = 永远不清）。

### RAG（含 MCP 生命周期与本地模型）

- 🔴 **`rag.py` 顶层那句 `import sentence_transformers` 不是冗余，删了会死锁**：症状是 `query_rag` / `save_knowledge` **永远不返回但副作用已发生**（文件与向量都写好了、**且不烧 token**）。根因：原生扩展在**事件循环跑起来之后**才首次加载 ⇒ 卡在 Windows DLL。回归守卫是**源码级**测试（`tests/test_mcp_tool_lifecycle.py`）。
- ⚠️ **代价说清**：这么改之后**每个 RAG 工具调用付 ≈8 秒**子进程启动（`rag` 的 initialize 8.0–10.6s，而 `code_tools` 只有 0.9s）。改进方向：把 `query_rag` 改成本地工具，或让 MCP server 常驻。
- **自动沉淀走 `store.save_document`（进程内），不走 MCP 工具**；它**不走三档权限弹框**（那是应用自己的记账，不是模型的自主动作），但**「只读」档下仍然不写**。模型**自己**调 `save_knowledge` 时照旧弹框。
- **排错工具**：`scripts/probe_mcp_server.py` —— 从 Agent 侧看，"服务端不回"与"客户端读不到"是**同一种症状**，只有手工发 JSON-RPC 才能分清。
- **检索质量（阶段 6 消融，实测）**：文件粒度 top-1 命中 **0.6 → 0.9**；真源 top-1 **0.4 → 0.6**；干扰项命中 **0.6 → 0.4**；稳态延迟 **12.8 → 86.7 ms**（全量召回对照 289.7 ms）。⚠️ **语料只有 35 条原子** ⇒ 数字是**真实下界，别外推**；`RAG_RECALL_K=10` **不动**（2026-09-23 定），对照组差异作为**局限**写进报告。
- **RAG 的定位 = 语义记忆**（记录使用中积累的经验/习惯），**不是企业知识库问答**；别为了指标好看扩语料或换模型。
- **两个本地模型不进版本控制**（各 ≈87MB）：`all-MiniLM-L6-v2`（向量）+ `ms-marco-MiniLM-L-6-v2`（精排）；默认位置 `<仓库上级目录>/embedding-model/…`（**不写死任何人的路径**）。装法 `uv run python scripts/fetch_models.py`。
- 🔴 **不许恢复"缺模型就自动下载"**：实测那条路要 **236 秒 / 下 671 MB**（`ignore_file_pattern` 没滤掉 ONNX/OpenVINO，真正需要只有 87 MB），而且外层 `redirect_stderr(None)` 把下载器的报错也堵死 ⇒ 用户只看得到 `AttributeError: 'NoneType' object has no attribute 'write'`。
- **现在的行为**：缺向量模型 ⇒ `store.RagModelMissing`，**0.06 秒**抛出（判据放在 `import sentence_transformers` **之前**），消息里给两条出路；缺精排 ⇒ **优雅降级**（纯向量召回，不抛）。Web 启动会打两行 `[RAG] …` 状态。
- **MCP 生命周期**：`MultiServerMCPClient.get_tools()` 的 docstring 明写"每次工具调用新建会话" ⇒ 没有长期连接、**没有需要关闭的 client**；`__aexit__` 是普通函数，调用即抛 `NotImplementedError`（不能当上下文管理器）。

### 评估（阶段 6 重建，口径**别改回去**）

- **规模与成绩**：30 题 / **163 条断言**（状态 **124** / 轨迹 29 / 文本 10；**LLM 评分档 0 条**）；**single 30/30、multi 30/30**，平均分都是 **1.0000**；打回 / 击穿预算 / 超时 / 未测 / 异常全 **0**。
- **成本画像**：multi/single 的 token **中位 1.58×**、时间 **2.07×**；其中 **Verifier 占总 token 25%**、Planner 2.4%。
- **四条口径（不许放松）**：① **通过 = 满分**（部分分单列 `partial` 并披露占比）；② **`skip` ≠ 0 分**（环境不可用记 `ok=None`；全 skip 的题标 `unavailable` 且**不进分母**）；③ **超时/异常也跑判定器**（产物可能已经写出来了）；④ 判定器按**真实 MCP 工具名 + 真实参数名**写（`assert_known_tools()` 对着 `permissions.ALL_TOOLS` 校验，危险命令扫描**整个 args**）。
- ⚠️ **人为闸门会制造假失败**：限额版 multi（`TASK_TOKEN_BUDGET=200k`）是 **29/30 且更贵**（1.78M token，E015 被掐断烧掉 257k 仍失败）；改成"只计量"后 **30/30 且更省**（1.64M）⇒ 生产口径只计量，归档里那份限额版就是这条教训的证据。
- **跑法**：`reset_eval_threads.py --yes` → `preflight.py --run-id …` → **逐题** `run_e2e.py` → `merge_runs.py --archive` → `report.py`。⚠️ **别用 `--all`**：结果 JSON 只在整轮结束写一次，被掐断就整轮白跑（E016 的教训）。⚠️ **每个 run-id 只能用一次**（复用会读到上一轮 checkpoint）。
- **隔离**：评估只用 `runtime/eval_knowledge/` + `runtime/chroma_db_eval/`（夹具在 `evals/fixtures/knowledge/`，35 条 = 7 文件 × 5 条），**绝不碰产品的 `data/knowledge/`**；跑评估时别用 Web/CLI 干活（共用 `runtime/workspace/`）。
- **已知局限（如实写，别外推）**：每题每轮只跑 1 次（方差量化不出来）；题集对当前模型**已饱和**（两轮都满分 ⇒ 架构差异不体现在分数上，只能看成本侧）。
- **归档**：`docs/evidence/` —— 原始结果（`v3-single.json` / `v3-multi.json` / 限额版对照 / 两份 RAG 消融）+ 报告 `评估报告.md` + 截图；**过程账在 `docs/records/`**（可编辑），不放这里。

**关键数字与核验命令**：

| 数字 | 值 | 怎么核 |
|---|---|---|
| 测试数 | **665**（+ 5 条集成测试默认不跑） | `uv run python -m pytest tests/ -q` |
| 覆盖率 | **77%**（会随环境波动：依赖容器在跑/全停时略不同） | 同上（`addopts` 自带 `--cov`，看 `TOTAL` 行） |
| 评估题数 / 断言数 | 30 题 / 163 条 | `uv run python evals/run_e2e.py --list` |
| MCP 工具数 | 25（+ 7 文件工具 = **32**） | `git grep -c "@mcp.tool" -- app/code_agent` |
| 知识库条目 | 测试语料 35；**产品库默认 0** | `Get-ChildItem evals/fixtures/knowledge -Recurse -File` |
| RAG 消融 | top-1 正解 0.40 → 0.60（对照 0.70）；干扰 0.60 → 0.40 | `uv run python evals/rag_ablation.py --reps 10` |

### 安全与权限（阶段 5，**主防线在应用层**）

- **三档权限**（只读 / **需确认（默认）** / 放开）与 **32 个工具**的档位表在 `app/code_agent/security/permissions.py`（只读 14 / 写执行 18 / 高危 6）。
- 🔴 **`tool_wrap._process` 里的顺序不许动**：**停止检查 → 权限判定 → 缓存查询**。
  ① 停止检查必须在权限之前（已经决定要停的任务不该再弹框问用户）；
  ② 权限判定必须在缓存查询**之前** —— 否则"曾经允许过"的缓存值会让**已被拒绝**的调用照样返回结果。
  测试守着 32 个工具一个不多一个不少 + 四条实现约束。
- ⚠️ **内容级黑名单修过形同虚设的漏口**：旧模式 `\brm\s+-rf\s+/\s` 要求 `/` 后**还有空白** ⇒ 最经典的 **`rm -rf /`**（`/` 在结尾）直接放行；PowerShell 侧还要求 `-Recurse` 在 `-Force` 前、不认 `rm`/`del`/`rd`/`ri` 别名。现在两侧都是"flag 后瞻匹配、顺序无关"。
- ⚠️ **别指望命令自带的开关**：GNU rm 的 `--preserve-root` **只管"参数就是 `/` 自身"**（`rm -rf /*`、`rm -rf /mnt/c/…` 都不管）；`chmod`/`chown`/`chgrp` 递归操作 `/` **默认不保护**。
- **回归测试** `tests/test_dangerous_commands.py`：**打桩 `subprocess`，断言危险命令走不到"启动子进程"那一步**。⚠️ **别再真打危险命令去"验证"**（阶段 5 真踩过：`rm -rf /` 真的进了 WSL）。
- 🔴 **WSL2 只是隔离执行环境，不是安全沙箱**：它默认挂 `/mnt/c`，而 `vm.py` 还主动用这条通道读写 Windows 文件 ⇒ 对外措辞**别说"沙箱"**。
- **可达性**：`execute_powershell_command` 是**唯一能把原始命令透传下去**的入口；VM 四个工具都 `shlex.quote` 过参数（那边属纵深防御）。
- **审计**：`runtime/permissions.log`（所有确认决定 + 放开档高危操作，一行一条 JSON）。

### 停止 / 墙钟 / 终止文案（阶段 8 · P0 + P1）

- **停止是"协作式"的**：只在三个**检查点**生效 —— ① 节点入口（planner / executor / verifier）
  ② executor 的 **ReAct 每一步**（`astream` 循环开头）③ **每次工具调用之前**（`utils/tool_wrap.py::_process`）。
  ⇒ **正在飞的那一次模型调用不会被打断**（它返回后才发现已停），但它之后的步骤一定不会再开始。
  已发生的副作用**一律保留**（不回滚：回滚自己也可能失败，还会掩盖现场）。
- 🔴 **`TaskCancelled` 必须继承 `BaseException`**（`app/code_agent/agent/cancel.py`）：工具调用发生在
  LangGraph 的 `ToolNode` 里，它 `except Exception` 会把异常**变成一条 ToolMessage**交给模型 ⇒
  "停止"会退化成"模型看到一条奇怪的错误、再决定下一步"（多烧一次调用，还可能换个办法接着干）。
  ⚠️ 代价：**所有可能穿过它的入口都要显式接住**（`multi_agent` 各节点 + `run_multi_agent` 兜底 + `web/server.py::_run_chat`）。
  ⚠️ **不许**把 `_run_chat` 的 `except TaskCancelled` 写成 `except BaseException`（会吞掉 WS 断开时的 `asyncio.CancelledError`）。
  回归测试：`tests/test_cancel.py::test_real_tool_node_lets_the_stop_signal_through`（**用真 `ToolNode`**）。
- **停止优先于"允许/拒绝"**：Web 收到 `stop` 时会把待确认的弹框**按拒绝收掉**（否则要等满确认超时才有下一个检查点），
  而 `permissions.enforce` 拿到答案后**先判停止、再判允许/拒绝** ⇒ 不会留一条误导性的 `denied_by_user` 审计。
- **墙钟** `CODE_AGENT_TASK_WALL_CLOCK`（**默认 900 秒 = 15 分钟**，0/负数 = 不限制）：
  到点走**同一条**停止路径，只有原因码不同（`wall_clock`）。⚠️ **人工确认（弹框）期间不计时**
  （`cancel.pause_clock()` 包住 `enforce` 的等待段；恒等口径 = 净任务时长）。
- ⚠️ **`CancelToken` 一次任务一个**（CLI 每轮 / Web 每条消息新建）：复用会让"单任务墙钟"变成"进程开了多久"，
  于是进程满 15 分钟后**每个**新任务都被立刻掐掉。
- **评估（evals）不绑停止开关** ⇒ evals 既不会被墙钟掐断、也不发状态条事件（`emit` 的设计：没绑就跳过）。
- **终止之后**：不调 Verifier（`after_executor` 判 `cancelled` / `budget_exceeded`）、不打回重跑、
  **不沉淀经验**（半途而废的"经验"是噪音）、文案固定中文（`multi_agent._cancel_message`）。
- **任务状态条**：`status` 事件（`elapsedSec` / `pausedSec` / `steps` / `tokens` / `usageLimit` / `wallClockSec`），
  前端 `store.status` + 本地 1 秒补时（**弹框期间冻结**，与后端口径一致）。
- **前端「停止」按钮**（`store.stopTask()` → WS `{"type":"stop"}` → `stopping` 回执 → `result.cancelled`）：
  结果卡片显示 `⏹ 已停止 · 你点了「停止」`，**不是**"验收未通过"。
- 🔴 **权限弹框是 `position: fixed; inset: 0` 的全屏遮罩** ⇒ 它**盖住输入区的停止按钮**（实测 `elementFromPoint` 命中 `div.overlay`）。
  所以弹框里**必须**有「⏹ 停止任务」（已加，`PermissionDialog.vue`）：点了 `stopTask()` + 清本地弹框，**不发** `permission_response`（那个 Future 由后端结算）。
  ⚠️ **停止 ≠ 拒绝**：拒绝只否掉这一次调用，任务会换个办法接着干；停止是整轮停下。
- ⚠️ **`step_count` 每轮必须复位**（`run_multi_agent` 的输入里带 `"step_count": 0`）：它是个只增不减的通道，
  不复位的话同一会话里第 N 张卡片会把前 N-1 轮的步数一起算进去（实测 35 → 40 → 51）—— 而卡片上写的就是"步数"。
  **一轮之内的打回重跑仍然累加**（那是它本来的意思）；停止文案与卡片必须报**同口径**的步数。
- ⚠️ **`retry_count` 在终止路径上也要计**（阶段 8 · P1 修的 F3）：`is_retry` 必须在**所有终止分支之前**算，
  否则「被打回后发起的、却被预算/停止掐断」的那一轮不计数 ⇒ 卡片写「打回 0 次」而实际被打回过。
- 🔴 **预算终止 ≠ 验收未通过**：① 出边判 `budget_exceeded` ⇒ **不调 Verifier**；
  ② `run_multi_agent` 里**不许**把预算终止套上「任务执行完成，但验收未通过」那层包装
  （重跑轮撞预算时 `verdict` 还留着上一轮的 FAIL，一包装就是第 2 题现场那句看不懂的混合文案）；
  ③ 文案统一走 `_budget_message` / `_cancel_message`，两者共用 `_SIDE_EFFECTS_KEPT` 与 `_TERMINATION_TAIL`。
- 🔴 **终止说明里不许写「见上方工具调用轨迹」**（账本 R6）：历史回放**只有最终回复、没有轨迹**
  （`web/server.py::get_session_messages` 只回 role+content），那句话会把人引到不存在的地方。
  守卫在 `tests/test_termination.py`（**AST 级**：只查字符串字面量，注释里提它是允许的）。

### 什么时候该先问一句（阶段 8 · P1，候选池 §十五A）

- 两个 Executor 提示词（`SYSTEM_PROMPT_TEMPLATE` / `EXECUTOR_PLAN_PROMPT`）都拼了同一个常量
  `prompts.CLARIFY_PRINCIPLES`：判据是「**问一下的成本 vs 猜错重做的成本**」，
  猜错代价大（删除 / 覆盖 / 上传到别人机器 / 不可逆）⇒ 先问；有明确默认值的 ⇒ 别问、直接做并写明假设；
  目标不明确 / 自相矛盾 / 明显做不到 ⇒ 先问；同类失败连续 2 次 ⇒ 停下汇报（试过什么 / 卡在哪 / 给 2~3 个选项）。
- **提问必须带默认建议**（不是空问），且防刷：一轮最多问一次、整个任务最多 2 次，超过就按最保守假设继续做。
- 🔴 **往这段文本里加字不许出现 `{` `}`**：模板会被 `PromptTemplate.format()` 处理，
  举 `${…}` 这类例子会直接炸（举例请用「」或中括号）。有测试守着（`tests/test_prompts.py`）。

### Web 端（会话管理与模型设置）

- **会话列表是两张表拼的**：`checkpoints`（saver 管，给时间与条数）+ **侧车库 `runtime/sessions.db`**（标题 / 置顶 / 软删除）。⚠️ **别把侧车并进 `checkpoints.db`**（两边抢同一把 sqlite 锁、排查时分不清谁写的）。
- **删除永远先软删除**：`deleted_at` 一置 → 回收站（可「恢复」）；只有回收站里再点「彻底删除」才真删，且顺序不能反（`writes → checkpoints`）。
- 🔴 **"会话正被使用"不能用 `_session_locks` 判断**：它按设计**不回收**（连接断了空 Lock 还留着）⇒ 会把早已断开的会话当成活的、**永远删不掉**。要用 **`_active_threads` 计数**（WS 建立 / `new_session` / `load_session` / 消息带 `threadId` / 断开 五处维护）；活会话一律 `409`。
- **系统线程**（前缀 `eval-` / `probe-` / `smoke` / `nowrap-`）默认不在列表显示，底部一行小字可展开 + 「清空系统线程」；计数为 0 时**一个字都不显示**。
- 🔴 **`llm.py` 不许出现模块级 `llm = get_llm()`**（已改成 PEP 562 惰性 `__getattr__`）：那行会在 **import 期**建 LLM ⇒ 没配 key 时 import 直接抛（**服务起不来、pytest 收集也炸**）。有源码级守卫 `tests/test_web_no_model.py`。
- ⚠️ **"还没有可用模型"不是错误状态**：`rebuild_agents()` / `apply_settings()` 捕获缺 key 的 `ValueError`（只警告 + agent 置空）；启动顺序**先 `apply_settings()` 再 `runtime.load()`**；前端据 `modelReady` 显示引导。**别在 WS 层拦 `chat`**（实测会让按协议等消息的测试卡死）。
- **用户加的第一个自定义模型 = 四个角色的默认模型**（否则未指定的角色回落 `.env` 的 `MODEL_NAME`，而面向用户的 `.env` 往往是空的）。`/api/settings` 的"系统默认模型"界面已不露出，CLI/evals 也完全不读。
- ✅ **界面第二轮反馈 6 条已全部有结论**（测试四个角色 ✅ / 侧栏文案 ✅ / 会话行按钮维持 hover / 小字已精简 / 防缓存不做 / 两个测试会话不处理）—— 论证在 `docs/records/2026-10-02_WEB端走查与修复账.md` §七。

### 工具参数校验与跨平台路径（两次实锤事故）

- 🔴 **`make_dir_in_vm` 收到 Windows 路径会造出畸形目录**：`shlex.quote` 把整串包成**一个**参数，而 **Linux 里反斜杠不是分隔符** ⇒ 整串被当成文件名；Windows 又不允许文件名含 `:` ⇒ WSL/驱动层写**私用区替身**（`:`→**U+F03A**、`\`→**U+F05C**）。⚠️ 那个目录是**空的** ⇒ **git 完全看不见它**（`git status` 一直干净），排查花了三轮。
- **防线**：`vm.py::ensure_wsl_path()` —— 四个 VM 工具的 WSL 侧路径参数（`make_dir_in_vm.dir_path` / `list_files_in_vm.dir_path` / `write_file_to_vm.file_path` / `upload_directory_to_vm.vm_dest_dir`）收到 Windows 路径或含 `\` ⇒ 抛 `VmPathError`，消息里带两条出路（改用 `execute_powershell_command`，或写成 `/mnt/<盘符>/…`）。⚠️ **刻意不自动转换**（静默转换会掩盖"用错工具"，让它一直错下去）。
- **工具描述要写"什么时候不要用它"**：事故直接原因是模型按工具名（`make_dir`）匹配、忽略了 `in_vm`；四个工具的 description 现在都点名"只接受 WSL 内路径"。回归测试 `tests/test_vm_path_guard.py`（打桩 subprocess，断言**走不到执行那一步**；去掉校验 7 条红）。
- 🔴 **路径转换不许先无条件 `os.path.abspath()`**：Linux 上 `os.path.abspath("E:\…\a\b")` 会把它当**相对路径**、拼上当前目录 ⇒ 盘符正则永远匹配不上（CI 就是这么红的）。**修法**：先按**语法**判盘符（`^[A-Za-z]:[\\/]`）再决定要不要 abspath ⇒ 跨平台一致，Windows 行为不变。
- **CI 三条教训**：① **本机绿 ≠ CI 绿**（路径语法 / 行尾 / 权限 / 大小写要专门过一遍）；② **断言别写脆**（别断言"默认目录 == 仓库上级目录" —— 用户按 `.env.example` 设了环境变量就不成立）；③ **GitHub 的 job 日志接口即使仓库公开也要求 admin 权限** ⇒ 读不到日志时先做"最小复现"，或让人贴日志。

### CI 与徽章（GitHub Actions / Gitee，2026-10-06 实测）

- ⚠️ **徽章会落后于最新提交**：它显示的是"**最近一次*完成*的运行**"。实例：run #11 的 job 从未启动、
  被 GitHub 取消（列表里是红叉），而徽章仍写着 `passing`。
  ⇒ **判断"我这次提交绿没绿"，要看 run 列表里对应 commit 的那一条，别信徽章。**
- ⚠️ **"红"不一定是测试失败**：那次 job 的 `conclusion=cancelled`、`steps: []`、`runner_id: 0`
  ⇒ **它从未获取到 runner**（队列里排 15 分钟后被取消）。**重推一次即通过**（实测 48 秒）。
  排查口诀：**先看 `steps` 是否为空** —— 空 = 没跑过，别去翻测试日志。
- **怎么查状态**（匿名可读）：`api.github.com/repos/<owner>/<repo>/actions/runs?per_page=1`
  （`status` / `conclusion` / `run_number`）；**job 详情**加 `/jobs`；
  ⚠️ **日志接口要 admin 权限**，匿名问必然 403；匿名 API 另有 **60 次/小时**限流，被限流时改用徽章或稍后再试。

### 前端产物与行尾

- **`app/web/frontend/dist/` 入库**（clone 下来不装 Node 也能开界面）⇒ 改前端必须 `npm run build` 并提交产物（含**被删除**的旧 hash 文件）。⚠️ 根 `.gitignore` 曾有裸 `dist/` 会吞掉新构建（已改 `/dist/`）。
- ⚠️ **`dist/index.html` 被 git 判为二进制（`-text`）**，不做行尾规范化；Vite 模板若 CRLF ⇒ 本地产物 CRLF、Linux CI 产物 LF ⇒ 任何两端比对**必红**（排查过三轮）。
- `.gitattributes` 已显式钉 LF：`app/web/frontend/index.html`、`public/**`、`dist/index.html`。

### 仓库整理

- `runtime/` 与 `.temp/` 都是 gitignore 的运行时目录 ⇒ **做全仓扫描必须排除**（否则扫到生成物）。
- `docs/` 四块：`architecture.md`（设计取舍）、`archive/`（**冻结快照**）、`evidence/`（评估原始结果，**只追加、不改写**）、
  `records/`（**过程账，可编辑**：走查/修复/缺陷账，命名 `<日期>_<主题>.md`）。
- ⚠️ **git 历史 2026-09-27 用 `filter-repo` 重写两次**（公开前脱敏），剔除 4 条路径（归档前后两批旧简历、`docs/handover.md`、`docs/resume-star.md`）⇒ **旧 hash 全部失效**，映射表与重写前整份备份在**本机备份（未入库）**。
- 🔴 **脱敏核对不能只对路径，要对"值"**：第一次按路径核对，漏了**改名之前**的那批旧简历（`docs/interview/`），地基提交的树里还留着 8 个文件；第二次改成"把手机号/邮箱原文取出，`git grep -F` 跨**全部提交**核对"才查出来。

## 代码地图

```text
main.py                      CLI 入口（argparse）
app/code_agent/              Agent 主体
├── agent/                   状态图(multi_agent) · 停止与墙钟(cancel) · 上下文工程(context) · 分层记忆(memory)
│                            · 节点级事件(events) · REPL 与非交互入口(code_agent) · 提示词(prompts)
├── model/llm.py             模型注册表：按角色取 / 降级链 / 热切换（build_llm / set_llm）
├── mcp_servers/             自建 MCP server：powershell(2) / browser(1) / mysql(10) / vm(4) / code_tools(4)
├── rag/                     rag.py（MCP 薄壳，4 工具）· store.py（分块 / 召回 / 精排，全懒加载）
│                            · chunking.py（纯函数，不 import torch）
├── security/permissions.py  三档权限档位表（32 工具）+ 判定 + 人工确认闸门 + 审计
├── tools/file_tools.py      FileManagementToolkit(root_dir=WORKSPACE_DIR) → 7 个文件工具
└── utils/                   mcp 工厂(load_mcp_tools) · tool_cache(Redis)
                             · tool_wrap(停止检查→权限→外置→缓存：工具调用的唯一收口)
config/models.json           模型注册表（默认空 = 走 .env；**进版本控制**，用户自定义模型在 web-settings.json）
app/web/                     server.py（FastAPI：WS /ws/chat + REST + 托管 dist + /api/sessions）
                             · sessions.py（会话侧车库 runtime/sessions.db）· frontend/（Vue3 源码 + 入库的 dist）
evals/                       评估体系：tasks(30 题) · verifiers(判定器工厂) · runner · run_e2e
                             · preflight · report · rag_bench · rag_ablation · merge_runs
                             · reset_eval_threads · env(语料隔离) + fixtures/knowledge/(35 条夹具)
scripts/                     probe_mcp_server.py · fetch_models.py · mysql-init/ · run/（启停脚本）
tests/                       665 条测试（含 5 条默认不跑的集成测试 tests/test_integration_mcp.py）
docs/                        architecture.md（ADR）· archive/（冻结快照）
                             · evidence/（评估原始结果 + 报告 + 截图）· records/（过程账，可编辑）
```

**模块级细节与设计取舍看 [`docs/architecture.md`](docs/architecture.md)** —— 6 条 ADR：RAG 全链路 /
评测体系 / 上下文工程 / Agent 编排 / 权限与安全 / 会话存储分家（每条含**代价与否决理由**）。

## 阶段收尾清单

> 为什么要有这一节：活文档描述"现在是什么样"，每改一次代码就可能失效一处。
> **过期比缺失更危险** —— 缺失可以读代码补，过期会让下一个会话照着错的做。

| # | 收尾动作 |
|---|---|
| 1 | 刷新「**接活先看**」：当前阶段 / 最近提交 / CI 状态 / 待办 |
| 2 | 核对本阶段**影响到**的段落：环境与工具链 / 常用命令 / 代码地图 / 关键数字 |
| 3 | 改掉 `README.md` 里受影响的数字与章节（对外状态 ≤10 行） |
| 4 | 在**仓库外**的《讨论结论汇总》追加订正记录（编号续上）+ 验证记录（正反两向证据） |
| 5 | **机械自查**：`git grep` 本阶段的关键名词（旧工具名 / 容器名 / 服务名 / 端口 / 测试数 / 覆盖率 / 旧路径），命中的**活文档**必须改对；只追加类文件不算 |
| 6 | `uv run python -m pytest tests/ -q` + `uv run ruff check .` + `uv run ruff format --check .` 全过 |
| 7 | `git add` → `commit`（信息里带验收数字）→ **两个远端都要推**：`git push origin master` + `git push github master` |
| 8 | 如需给历史留档：另存一份带日期的冻结快照进 `docs/archive/`（头部 3 行声明"冻结 + 当前规范看 `AGENTS.md`"） |
| 9 | **本文件超过 50 KB 必须压缩**（助手侧对指令文件有 **65 KB 预算**，超了会**从尾部静默截断** ⇒ 后面的内容等于不存在） |
| 10 | **状态只允许出现在两处**：`README.md`（对外）+ 本文件「接活先看」（对内）；**第三处出现即视为 bug**（历史只允许进 `docs/archive/`，且必须带"冻结"声明） |
| 11 | **密钥与个人信息检查**（公开前 / 定期）：① 扫入库内容 —— `git grep -nE "sk-[A-Za-z0-9_-]{16,}|ghp_|github_pat_|AKIA|hf_|glpat-" -- .`（**必须为空**）；② 确认仍被忽略 —— `git check-ignore -v .env runtime/web-settings.json .temp`；③ **历史抽样** —— `git grep -I -lE "<前缀>" $(git rev-list --all)`；⚠️ 命中若落在**考古线**（`raw-origin`）⇒ 只需确认那个仓**私有**且 **refs 从未推给主仓**（见「考古与备份」） |

## 历史与档案

- [`docs/archive/`](docs/archive/) = **冻结快照区，不再更新**。现有：
  `agent-history-raw-2026-10-03.md`（本文件的历史版本，**未清洗**）·
  `agent-history-2026-10-03.md`（同上的去冗余版）·
  `handover-raw-2026-10-03.md` / `handover-2026-10-03.md`（旧的"交接文档"原始版与清理版）。
  **想看"当时怎么做的"翻这里；当前规范一律以本文件为准**。
- [`docs/evidence/`](docs/evidence/) = 评估**原始结果**（`v3-*.json` / RAG 消融）+ 生成物（`评估报告.md`）+ 截图，
  **只追加、不改写**（里面的本机路径属原始证据，刻意保留）；规则见 [`docs/evidence/README.md`](docs/evidence/README.md)。
- [`docs/records/`](docs/records/) = **过程账（可编辑）**：缺陷账 / 走查与修复账，每条都带"现象 → 根因 → 修法 → 证据"；
  已进本文件「已知坑」的事项在这里**只留索引与当时的证据**，不重复细节。
- 项目过程档案（阶段方案 / 讨论结论汇总 / 交接文档的**活版本**）在**仓库外的项目档案目录（开发者本地维护）**
  —— 仓库内不留本机路径，也不重复它的内容。
- 阶段 5 / 阶段 6 的完整执行清单、红绿证据与 D1–D5 缺陷账：
  `docs/records/2026-09-25_评估口径与缺陷账.md` + 仓库外《讨论结论汇总》的订正记录（编号 #1–#70）。
