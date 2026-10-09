# AI Agent Test — LangGraph + MCP Code Agent

> **给谁看**：**AI 编码助手**（Claude / Cursor / Copilot / 其他）。
> 人类读者看 [`README.md`](README.md)；**设计取舍与代价**看 [`docs/architecture.md`](docs/architecture.md)；
> **历史过程（冻结）**看 [`docs/archive/`](docs/archive/)。
> 本文件是**操作手册**：约定 / 命令 / 已知坑 / 当前状态。**只写"现在该怎么做"，不写叙事。**

## 接活先看

**当前状态（2026-10-07）**：

| 项 | 值 |
|---|---|
| 阶段 | **阶段 8（探索测试驱动的稳健性修复）✅ 已收口（2026-10-08）** —— 阶段 0–7 已完成收口；**P0 已落地并做完真机验收**（停止 + 墙钟 + 停止按钮 + 任务状态条；真机撞出的 F1/F2 也已修）· **P1 已落地**（终止文案统一 + §十五A 三条提问原则 + 顺手修的 F3）· **P1.5 已落地**（入口「模板没替换」检测，含 30 道评估题的护栏）· **P2 已落地**（三项阈值按模型窗口自动算 + 任务预算放宽到 50 万~200 万；比例 2026-10-07 定为 **压实 25% / 单次输入 50% / 任务 max(50 万, 4×窗口)**）—— **阶段 8 助手侧已收口**；另有**一轮用户实测驱动的修复**（2026-10-07 深夜：跨轮串味 / 黑名单误伤 / 注入来源 / 路径报错 / 断线留档 + 执行中拦住切会话 + 状态条停止按钮 + 知识库面板），见修复账 §十一 |
| 最近提交 | `git log --oneline -1`（别在文档里写死 hash —— 提交一次就过期）|
| 远端 | `origin` = Gitee（镜像）· `github` = GitHub（主仓）；**tag `v1.0.0` 两边都有** |
| CI | **passing**（`.github/workflows/ci.yml`）；跑 `ruff check` → `ruff format --check` → `pytest tests/ -v`，**不需要 `.env`** |
| 测试 | **723** 条通过（另有 5 条真集成测试**默认不跑**）；覆盖率 **78%~79%**（同一提交在不同环境下实测区间） |
| 评估 | **single 30/30 · multi 30/30**（平均分 1.0000，163 条断言）→ 报告 `docs/evidence/评估报告.md`；⚠️ **该成绩取自旧阈值口径**（压实 6000 / 单次输入 30000 / 任务 200000，且当时**还没有**按窗口推导）—— 与将来的新口径**不可直接对比** |
| 仓库可见性 | **public**（2026-10-03 定：作为对招聘方展示的入口） |

**状态与未决事项**（✅ = 已完成，不占待办；每条标归属）：

1. ✅ **阶段 8 助手侧已收口（2026-10-07）** —— P0（停止 / 墙钟 / 状态条 / 真机验收 + F1、F2）·
   P1（终止文案统一 + §十五A 提问原则 + F3）· P1.5（入口「模板没替换」检测）· P2（阈值按窗口推导）
   + 收尾补记（比例 15%/35% → **25%/50%**、`node` 下限 30000→4000、本机 `.env` 三行注释掉）。
   账本：`docs/records/2026-10-07_探索测试第1-2轮与问题账.md`；
   修复账：`docs/records/2026-10-07_阶段8修复账.md`（§一~§十，含**两处刻意偏离候选池**的留痕与用户第 3 轮实测）；
   计划与结果快照：`docs/archive/阶段8-计划与结果-2026-10-07.md`；
   设计取舍：`docs/architecture.md` **ADR-3 补充**（窗口 → 三项阈值）。
2. ✅ **用户第 3 轮实测驱动的修复（2026-10-08 凌晨完成，见修复账 §十一）** ——
   后端：**跨轮状态复位**（`PER_TURN_RESET` + 机械守卫，修掉"新任务拿到上一轮计划"）· 黑名单误伤 `Format-*` 收窄 ·
   注入经验标明来源（模型不再报"疑似提示注入"）· 路径类报错补根目录 · **断线也留档**（关页面不再整轮消失）。
   前端：**执行中拦住切会话/新会话 + 明确提示**（用户定的口径：不许切，但要说清为什么）· 状态条加「⏹ 停止任务」·
   新增**知识库面板**（查看 + 单条删除，删文件同时删向量）。
   ⚠️ **撤回三条**（不是缺陷）：B2 串味（同会话问两件事是正常用法，缺陷只有 B1）· B4 路由误判（simple 判得对，
   问题是代价失控，归 C/D/E）· B6 弹框数量（`always_allow` 是工具级，用户点的是拒绝，再问一次是对的）。
3. ✅ **探索测试：第 4 轮（2026-10-08）11 题全部通过**，未发现必须修复的缺陷 ⇒ 阶段 8 收尾依据；结果归档在 `docs/evidence/探索测试第4轮-2026-10-08.md`（另附机器可读的 JSON）。
   ⏸ **第 9 题（口令跨轮记忆）用户主动跳过**；本轮记录的三件小事（模型名表述 / 步数偏多 / 弹框次数）**都判定为不修**，其中「步数偏多」归 C/D/E 等数据。
4. **P3（暂缓，还不是很有必要）**：候选池 §十三③ 的「缓存命中显示」· 轨迹持久化+回放 · R4 减少弹框 —— 想做时从候选池取。
5. **C（结构化提问出口）/ D / E（循环与无进展检测）**：按原计划**等真实数据**（跑题时的现场记录）。
   ⚠️ 与 P3 是**两回事**：P3 是"想做但暂缓的功能"，C/D/E 是"要等数据才能定的方案"。
6. 本机 `.env` 的三项阈值已**注释掉** ⇒ 走窗口推导（128k 兜底 → `32000 / 64000 / 512000`）；
   模型窗口可在 Web 面板「我的模型」里填（**填了更准**：兜底 128k 只是保守假设）。
7. **用户侧**：本机 `.temp/` 有临时产物（不入库）；12 题继续测（助手**不接手**）。
8. ✅ **2026-10-08 已定的小事**（都做完，不再挂着）：执行中点「＋新会话」不再清空界面（`ChatView` 的
   wrapper 去掉、直接暴露 store 的函数）· README 补「文档地图」+ 数字对齐（723 条 / 覆盖率 78%~79% / 语句 2557）+
   写明「**多标签页可各跑一个任务**」这条边界（不堵后端，按用户定的口径）·
   《讨论结论汇总》**降级为历史档案**：新结论只写仓库内，**不再逐条追加订正记录**。
9. 📦 **仓库外文档清理（等本项目修完再做，用户定的）**：阶段 0–7 计划书归档 · `handover_交接文档.md` 归档 ·
   阶段 5 两份材料归档 · `支干得出结论/` 两份删除 · 《讨论结论汇总》正式标注冻结。
   ⚠️ **仓库内不动**：`docs/records/` 那两份老账不挪、`docs/archive/` 那两份 `-raw` 不删（用户明确要求保持不变）。
10. 不在本项目内：JD 分析 → 知识补课 → 面试追问演练；**项目 #2**（部署 / CI-CD 那条线）。

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
| 单元 + 工具级测试 | `uv run python -m pytest tests/ -v`（**723 个**） |
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
- **对话压实**：历史超阈值 ⇒ 最老一段压成四段式摘要；**摘要失败就原样保留**（省 token 不能把历史弄丢）。
  ⚠️ **阶段 8 · P2 起三项阈值是运行期按「当前模型窗口」算的**（`config.token_budgets()`）：
  压实 **25%** · 单次调用输入 **50%** · 任务累计 **max(50 万, 4×窗口)**（各带上下限封顶）。
  128k 窗口 → `32000 / 64000 / 512000`；1M 窗口 → `64000 / 200000 / 2000000`。
  🔴 **顺序约束**：压实必须**明显小于**单次输入 —— 前者是「用摘要换掉老历史」（信息还在），
  后者超限是「**直接砍掉**最老的消息」（信息没了）。25%:50% = 1:2 是刻意的。
  ⚠️ **上限是理智闸门**：用 >500k 窗口的模型时要**连上限一起抬**（否则 1M 窗口也退回 64k / 200k）。
  ⚠️ **`node` 下限不许设高**：曾设 30000，会把 32k 窗口顶到窗口的 94%（方向反了）⇒ 现为 4000，
  并有「`node` 不得超过窗口一半」的回归断言（`tests/test_token_budgets.py`）。
  **解析顺序（高→低）**：`.env` 显式设的三个老名字 > 该角色模型的 `context_window`
  （`config/models.json`，或 Web 面板「我的模型」里填）> `CODE_AGENT_CONTEXT_WINDOW`（全局覆盖）> 128000。
  ⚠️ 换模型**不必重启**（额度每次任务重新解析）；但**改了 `.env` 必须重启**（那是 import 期读的）。
  ⚠️ 阈值是**估算值** ⇒ 一律留足余量；`NODE` 不要设得接近窗口。
- **token 预算**：额度见上一条（按窗口算，env 可覆盖）。⚠️ 任务级必须在 **executor 的 ReAct 循环内部逐步判** —— 只在节点入口判，一次"读大文件 + 反复重读"能在**单个节点调用**里烧掉十几万 token。
- **token 计量口径 = 计费口径**：`token_usage` 是**每次 LLM 调用累加**（同一段历史会被反复计费）⇒ 数字大是正常的；它**不是**上下文长度，两个数字别混着讲。
- **`token_detail` 是「计费原料」通道**（输入 / 输出 / 缓存命中 / 调用次数 / 未计量次数 / 首末时间 / `by_model` 分桶）：
  归一化在 `agent/usage.py`（纯函数 `normalize_usage` + `merge_token_detail`），三家字段名都认
  （langchain 给的是 `input_token_details.cache_read`；兜底认 `prompt_cache_hit_tokens` / `cache_read_input_tokens`）。
  ⚠️ **本项目只计量、不算钱**：没有价格表（各家单价不同、会调价、还有峰谷价与缓存价）⇒ 成本的用途是**相对比较**，不是财务对账。
  ⚠️ **缺失 usage 不许静默按 0**：`unmetered` 会一路带到状态条与结果卡（「N 次调用未计量」）——
  否则"provider 不返回 usage"会让**预算闸门静默失效**。实测（2026-10-09 真实 DeepSeek 响应）：DeepSeek 同时返回
  `prompt_cache_hit_tokens` 与 `prompt_tokens_details.cached_tokens`，并单列 `completion_tokens_details.reasoning_tokens`
  （推理 token 按**输出**计价）。⚠️ **新增该通道必须同时进 `PER_TURN_RESET`**（见下方「跨轮状态必须复位」）。
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
| 测试数 | **723**（+ 5 条集成测试默认不跑） | `uv run python -m pytest tests/ -q` |
| 覆盖率 | **78%~79%**（会随环境波动：依赖容器在跑/全停时略不同 —— 同一提交实测到过这两个值） | 同上（`addopts` 自带 `--cov`，看 `TOTAL` 行） |
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

### 跨轮状态必须复位（阶段 8 · 实测，**最容易复发的一类**）

- 🔴 **症状**：新任务**带着上一轮的"计划/轨迹/判定"跑**。现场：用户只发「你好」，Executor 拿到的是上一轮的
  上传计划 ⇒ 13 次工具调用 / 6 分钟 / 整段英文；回复里还混进上一轮计划的章节。
- **根因**：LangGraph 的语义是"**没传进 `ainvoke` 的通道保留上一轮 checkpoint 的值**"。`plan` 从不在复位字典里；
  `auto` 判 `simple` 时会**跳过 Planner**（于是也不会产生新计划）⇒ 旧计划被当成本轮计划。
  同一根因还造成：`verdict` 残留 ⇒ 新一轮第一次执行被 `_is_retry_round` 当成"重跑轮"；`executor_trace_list`
  残留 ⇒ 轨迹跨轮累加（实测 28 条 = 两轮混在一起）。**这跟 P0 修过的 F2（`step_count`）是同一个坑。**
- ✅ **怎么办**：所有"每轮"通道集中在 `multi_agent.PER_TURN_RESET`，`ainvoke` 输入用
  `{**per_turn_reset(), "user_input": …, "knowledge": …}`。**新增任何每轮通道，必须同时进这张表** ——
  `tests/test_per_turn_state.py::test_every_state_channel_is_classified` 会拿 `AgentState` 的**全部通道**
  做覆盖校验，漏一个当场变红（跨轮累积只允许 `messages`）。

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
- **前端「停止」有两个入口**（都是 `store.stopTask()` → WS `{"type":"stop"}` → `stopping` 回执 → `result.cancelled`）：
  ① 输入区右下角 `⏹ 停止`；② **任务状态条右侧 `⏹ 停止任务`**（2026-10-07 实测新增：用户视线在状态条那一行，
  原来只有输入区那个，他会"看不到停止按钮"）。结果卡片显示 `⏹ 已停止 · 你点了「停止」`，**不是**"验收未通过"。
  ⚠️ 叫"停止"不叫"暂停"：协作式停止**不可续跑**，叫暂停会让人以为能恢复。
- 🔴 **执行中不许切会话**（用户 2026-10-07 定的口径）：`loadSession` / `newSession` 都有 `sending` 守卫，
  点了会显示一行黄字提示（`setNotice`，5 秒自动消失），**不是静默忽略**（原来的静默 `return` 让用户以为"点了没反应"）；
  点"当前会话"本身不提示。**权限弹框期间**则保持"整屏遮罩挡着、点不到"（用户明确要求保持现状）。
- **知识库面板**（2026-10-07 新增）：侧栏「🧠 知识库」→ `GET /api/knowledge` 列表 + `DELETE /api/knowledge/{name}`
  单条删除（**只收单层 `.txt`/`.md`**，路径穿越一律拒）。⚠️ 删除**同时删向量**（`store.delete_document_file`），
  否则检索会命中"幽灵条目"。背景：自动沉淀会自己往 `data/knowledge/` 写，而界面此前完全看不到。
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

### 「模板没替换」的检测（阶段 8 · P1.5，候选池 §十五B）

- 判据在 `utils/placeholder_guard.py`，**两道口子共用**：① 任务入口（`run_multi_agent`：命中 ⇒ 只回问、**不进图**，0 次模型/工具调用）② 工具层（`tool_wrap._process`：**路径类参数**命中 ⇒ 抛 `PlaceholderArgError`，普通 `Exception` ⇒ ToolNode 变成 ToolMessage，模型自己改）。
- 🔴 **判据是「形状 + 占位词」双条件，不是见到尖括号就拦** —— 实测两道评估题会被朴素实现误伤：
  `E016` 的 `{"status": "ok"}`（JSON 例子，**单花括号一律不管**）、`E029` 的 `TOTAL=<数字>`（`<数字>` 是文件内容的描述）。
  只认：内层是全大写标识符（`YOUR_API_KEY`）或含中文占位词（用户名/密码/密钥/路径/目录/地址…），外加 `你的XXX` 一种（XXX 里要含占位词）。`<h1>` / `${name}` / `{{ name }}` / `<T>` / `你的代码` 都不算。
- ⚠️ **逃生口**：任务里出现 `按字面` / `原样处理` / `不要替换` / `字面处理` / `别替换` ⇒ 不拦。
- ⚠️ **正文参数不查**（`text` / `content` / `command`）：写一个含 `<h1>` 的文件是正当需求。
- 🔴 **护栏**：`tests/test_placeholder_guard.py::test_every_eval_task_prompt_passes_the_guard`
  把**全部 30 道评估题**过一遍 —— 以后谁加了带真占位符的题会当场变红。

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

### 启动脚本（Windows · 两次真机事故换来的）

- 🔴 **`scripts/**/*.ps1` 必须带 UTF-8 BOM**：`start-app.cmd` 调的是 **Windows PowerShell 5.1**，
  它读**无 BOM** 的 `.ps1` 会按 ANSI(GBK) 解 ⇒ 中文乱码，而且**中文字符串末尾的引号会被吞掉**
  ⇒ 语法直接坏（实测解析报 `Unexpected token`）；而 `pwsh` 7 下一切正常 ⇒ **只有双击启动的人会撞上**。
  守卫：`tests/test_launcher_scripts.py::test_every_powershell_script_keeps_the_utf8_bom`。
  ⚠️ 用编辑器改这些文件后**要确认 BOM 还在**（改一次就可能被抹掉）。
- 🔴 **`start-app.ps1` 结尾必须 `exit $LASTEXITCODE`**：`& uv run uvicorn …` 失败**不会**自动成为
  `powershell.exe` 的退出码 ⇒ `.cmd` 里的 `if errorlevel 1 pause` 不触发 ⇒ **窗口闪退、用户看不到任何错误**
  （现场：8000 绑定失败，双击后窗口一闪而过）。守卫：`test_start_app_propagates_exit_code`。
- ⚠️ **端口可用性必须用「真 bind」判断**（`TcpListener`），**不能**只查 `Get-NetTCPConnection -State Listen` ——
  后者只看得见"有没有人在监听"，**查不出"端口被系统保留"**（Windows 上 Hyper-V/WSL 的 `excludedportrange`
  会随机划走 7927–8126 这类区间；那台机器上 8000/8001 就被划走了）。判据看错误码：
  **`10048` = 有进程占用**（去 `netstat -ano` 找 PID）；**`10013` = 被系统保留**
  （换端口，或管理员 `net stop winnat` → `netsh int ipv4 add excludedportrange protocol=tcp startport=<端口> numberofports=1 store=persistent` → `net start winnat`）。
  **别去杀进程**。查保留区间：`netsh interface ipv4 show excludedportrange protocol=tcp`。
- **端口候选顺序**（每个都实测，被占/被保留就跳到下一个）：`-Port`（默认 8000）→ 8010 → 8020 → 8300 →
  8310 → 9000 → 9010 → 9200；`-NoFallback` 可关掉自动换端口（失败即退出，退出码 1）。

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
│                            · **token 计量明细(usage)** · 节点级事件(events) · REPL 与非交互入口(code_agent) · 提示词(prompts)
├── model/llm.py             模型注册表：按角色取 / 降级链 / 热切换（build_llm / set_llm）
├── mcp_servers/             自建 MCP server：powershell(2) / browser(1) / mysql(10) / vm(4) / code_tools(4)
├── rag/                     rag.py（MCP 薄壳，4 工具）· store.py（分块 / 召回 / 精排，全懒加载）
│                            · chunking.py（纯函数，不 import torch）
├── security/permissions.py  三档权限档位表（32 工具）+ 判定 + 人工确认闸门 + 审计
├── tools/file_tools.py      FileManagementToolkit(root_dir=WORKSPACE_DIR) → 7 个文件工具
└── utils/                   mcp 工厂(load_mcp_tools) · tool_cache(Redis) · 占位符守卫(placeholder_guard)
                             · tool_wrap(停止检查→占位符→权限→外置→缓存：工具调用的唯一收口)
config/models.json           模型注册表（默认空 = 走 .env；**进版本控制**，用户自定义模型在 web-settings.json）
app/web/                     server.py（FastAPI：WS /ws/chat + REST + 托管 dist + /api/sessions）
                             · sessions.py（会话侧车库 runtime/sessions.db）· frontend/（Vue3 源码 + 入库的 dist）
evals/                       评估体系：tasks(30 题) · verifiers(判定器工厂) · runner · run_e2e
                             · preflight · report · rag_bench · rag_ablation · merge_runs
                             · reset_eval_threads · env(语料隔离) + fixtures/knowledge/(35 条夹具)
scripts/                     probe_mcp_server.py · fetch_models.py · mysql-init/ · run/（启停脚本）
tests/                       723 条测试（含 5 条默认不跑的集成测试 tests/test_integration_mcp.py）
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
| 4 | ⚠️ **2026-10-08 起改为**：新结论**只写仓库内**（`docs/records/` + 本文件），**不再**往仓库外《讨论结论汇总》追加订正记录（它已降级为历史档案，编号停在 #80） |
| 5 | **机械自查**：`git grep` 本阶段的关键名词（旧工具名 / 容器名 / 服务名 / 端口 / 测试数 / 覆盖率 / 旧路径），命中的**活文档**必须改对；只追加类文件不算 |
| 6 | `uv run python -m pytest tests/ -q` + `uv run ruff check .` + `uv run ruff format --check .` 全过 |
| 7 | `git add` → `commit`（信息里带验收数字）→ **两个远端都要推**：`git push origin master` + `git push github master` |
| 8 | 如需给历史留档：另存一份带日期的冻结快照进 `docs/archive/`（头部 3 行声明"冻结 + 当前规范看 `AGENTS.md`"） |
| 9 | **本文件超过 65 KB 必须压缩**（助手侧对指令文件有 **65 KB 预算**，超了会**从尾部静默截断** ⇒ 后面的内容等于不存在；2026-10-07 订正：原先写的 50 KB 是保守值，实际上限就是 65 KB） |
| 10 | **状态只允许出现在两处**：`README.md`（对外）+ 本文件「接活先看」（对内）；**第三处出现即视为 bug**（历史只允许进 `docs/archive/`，且必须带"冻结"声明） |
| 11 | **密钥与个人信息检查**（公开前 / 定期）：① 扫入库内容 —— `git grep -nE "sk-[A-Za-z0-9_-]{16,}|ghp_|github_pat_|AKIA|hf_|glpat-" -- .`（**必须为空**）；② 确认仍被忽略 —— `git check-ignore -v .env runtime/web-settings.json .temp`；③ **历史抽样** —— `git grep -I -lE "<前缀>" $(git rev-list --all)`；⚠️ 命中若落在**考古线**（`raw-origin`）⇒ 只需确认那个仓**私有**且 **refs 从未推给主仓**（见「考古与备份」） |

## 历史与档案

- [`docs/archive/`](docs/archive/) = **冻结快照区，不再更新**。现有：
  `agent-history-raw-2026-10-03.md`（本文件的历史版本，**未清洗**）·
  `agent-history-2026-10-03.md`（同上的去冗余版）·
  `handover-raw-2026-10-03.md` / `handover-2026-10-03.md`（旧的"交接文档"原始版与清理版）·
  `阶段8-计划与结果-2026-10-07.md`（2026-10-07：阶段 8 的**计划书中性化简写** + 实际结果对照 + 口径声明）。
  **想看"当时怎么做的"翻这里；当前规范一律以本文件为准**。
- [`docs/evidence/`](docs/evidence/) = 评估**原始结果**（`v3-*.json` / RAG 消融）+ 生成物（`评估报告.md`）+ 截图，
  **只追加、不改写**（里面的本机路径属原始证据，刻意保留）；规则见 [`docs/evidence/README.md`](docs/evidence/README.md)。
- [`docs/records/`](docs/records/) = **过程账（可编辑）**：缺陷账 / 走查与修复账，每条都带"现象 → 根因 → 修法 → 证据"；
  已进本文件「已知坑」的事项在这里**只留索引与当时的证据**，不重复细节。
- 项目过程档案（阶段方案 / 讨论结论汇总 / 交接文档的**活版本**）在**仓库外的项目档案目录（开发者本地维护）**
  —— 仓库内不留本机路径，也不重复它的内容。
- 阶段 5 / 阶段 6 的完整执行清单、红绿证据与 D1–D5 缺陷账：
  `docs/records/2026-09-25_评估口径与缺陷账.md` + 仓库外《讨论结论汇总》的订正记录（编号 #1–**#80**：阶段 5/6 到 #70，#71–#72 属阶段 7，**阶段 8 是 #73–#80**）。
