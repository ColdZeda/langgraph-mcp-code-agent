# Code Agent

> **给谁看**：第一次接触这个项目的人（面试官 / 同行 / 想跑起来的人）。
> 想了解内部约定与已知坑 → [`AGENTS.md`](AGENTS.md)。

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

**模型是按角色配的**（Planner / Executor / Verifier / Router）。**默认什么都不用配** ——
四个角色都走 `.env` 的那一套（`MODEL_NAME` / `MODEL_BASE_URL` / `MODEL_API_KEY`），
界面上下拉框的第一项「**使用配置默认**」就是它：

```bash
# .env（唯一的必填项是 key）
MODEL_API_KEY=sk-xxxx
MODEL_NAME=deepseek-flash        # 不填则用代码默认值
MODEL_BASE_URL=https://api.deepseek.com
```

- **想给用户预置几个可选模型**（开箱即用）：填 `config/models.json`（进版本控制）。
  ⚠️ 它**默认是空的**（2026-09-22 决定）—— 格式与示例写在该文件的 `_readme` 里。
  键 = 给用户看的**显示名**、`model` 字段 = **实际发给 API 的名字**（官方改名时只动后者）。
- **用户自己加模型**：面板 →「我的模型」→ 填 `显示名 + 模型名 + API 地址 + Key`，
  存本机 `runtime/web-settings.json`，加完立刻出现在四个角色的下拉框里。
  **每个自定义模型自带凭据**，与系统默认那组互不影响（"系统默认用 `.env` 的 key、
  我的 GLM 用我自己的 key"可以共存）。旁边的「测试连接」测的就是你刚填的这一组，
  并会回显**实际测的是哪个模型名**。
- **verifier 想单独换模型**要勾上「验收使用异构模型」—— 默认三角色同模型、Verifier 跟随 Executor
  （同一个模型自己验自己容易有一致的盲区，换一个模型能拿到独立视角）。
- **界面上随时能看到"现在是谁在干活"**：侧栏常驻显示「当前生效模型」（= Executor 那个）；
  每条结果卡片另显示「本轮**实际使用**的模型」—— 后者取自服务端回报的 `model_name`，
  所以用了官方改名后的别名或中转时，你能看出**真正回答的是谁**。

> ⚠️ **三处配置的优先级**（从高到低），别搞反：
> `runtime/web-settings.json`（界面点出来的）**>** `config/models.json`（仓库里的默认）
> **>** `.env`（密钥与兜底的模型名/地址）。
> 所以**在界面上存过一次模型选择后，改 `models.json` 的 `roles` 是不生效的** —— 要回界面改。
>
> - **`web-settings.json` 里含明文密钥**（系统默认凭据 + 每个自定义模型的 key）。它已 gitignore、
>   不进仓库，但**不要分享这个文件**；界面只回显尾号 4 位。
> - **改 `.env` 必须重启服务才生效**（配置在进程启动时读一次）；而界面改的会热生效，
>   且**从下一条消息开始**（正在跑的那条任务用的是它启动时的配置快照）。
> - **CLI 与评估（evals）不读 `web-settings.json`** —— 它们只用 `config/models.json` + `.env`。

### 运行（命令行）

```bash
uv run python main.py                        # 默认启动（执行模式 auto / 权限模式 需确认）
uv run python main.py --thread-id my-session # 指定会话 ID
uv run python main.py --mode single          # 执行模式：auto（默认）/ single / multi
uv run python main.py --permission readonly  # 权限模式：readonly / confirm（默认）/ open
uv run python main.py --debug                # 调试模式（详细日志）
```

> 「**执行模式**」和「**权限模式**」是**两条独立的轴**，可以任意组合（例如 `multi + readonly`）：
> 前者决定"谁来干"（要不要 Planner / Verifier），后者决定"允不允许动手"。详见[安全设计](#安全设计)。

退出：`exit` / `quit` / `q` / `退出` / `bye`

> **跨轮记忆**存在 `runtime/checkpoints.db`：不传参数时用 `.env` 的 `CODE_AGENT_THREAD_ID`
> （默认 `default`）→ **关掉再打开会继续上一次的对话**；想开一个新会话用 `--new-session`。

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

> 运行期**只有一个进程**：前端 `dist/` 由 FastAPI 用 `app.mount("/", StaticFiles(...))` 直接托管。
> 只有在改前端源码时才需要第二个窗口跑 `npm run dev`，那时请打开 **http://127.0.0.1:5173**
> （Vite 把 `/api` 与 `/ws` 转发给 8000）。

- 聊天界面：**执行过程实时可见**（阶段 5）—— 任务跑起来后逐行显示
  `路由 → Planner 规划 → Executor 第 N 步调用了哪个工具 → Verifier 验收`，
  而不是干等一个转圈；结束时给结构化结果——Planner 计划、工具调用轨迹（可折叠）、
  Verifier 验收徽章、token / 耗时统计；**侧栏有执行模式与权限模式两个下拉框**
- **人工确认（阶段 5）**：「需确认」档下，Agent 要动写 / 执行类工具时前端会**弹确认框**——
  显示工具名、关键参数、风险等级（高危的附影响面说明），可以点「允许执行」或「拒绝」，
  也可以勾「**本会话内对该工具总是允许**」（**默认不勾**，切档位或换会话即失效）；
  **没人应答会倒计时自动拒绝**
- 模型设置：界面内热切换模型 / API 地址 / Key，**四个角色（Planner / Executor / Verifier / Router）
  分别选模型**（下拉框默认只有「使用配置默认」= 走 `.env`，加上「我的模型」里用户自己加的）；
  设置只存本机 `runtime/web-settings.json`（**含明文密钥**，已 gitignore、不进仓库）
- 「我的模型」：填 `显示名 + 模型名 + API 地址 + Key` 就能接任意 OpenAI 兼容服务
  （中转站、GLM、Qwen…），**每个模型自带密钥**；可**测试连接 / 编辑 / 删除**（删除要点两次确认，
  并会顺带清掉角色里指向它的选择）
- **当前生效模型看得见**：侧栏常驻显示它（= Executor 用的那个，四角色不同时会提示）；
  每条结果卡片显示「本轮**实际使用**的模型」（服务端回报的名字，不是配置里写的那个）
- 会话列表：读 `runtime/checkpoints.db`；**点击任一会话即可切换并回放历史**，之后的对话在原会话上续聊

> 权限模式的持久化是**有取舍的**：只把「只读 / 需确认」落盘，**「放开」永不持久化** ——
> 新会话一律回落「需确认」。理由见[安全设计](#安全设计)（"我上次设过、这次忘了"是最容易埋雷的地方）。

> 前端（Vue 3 + Vite）源码在 `app/web/frontend/`，构建产物 `dist/` 已入库——不装 Node 也能直接运行；
> 改前端后 `cd app/web/frontend && npm install && npm run build` 重新构建。

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
> 这时用 `scripts\run\start-deps.ps1` 即可。

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
> （Agent 自学习闭环的存储端），**不是企业知识库问答**。所以它只带一份 35 条的**测试语料**
> （在 `evals/fixtures/`，只在评估时用；**产品库默认是空的**），
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
> ⚠️ **2026-09-24 起，跑评估时「自动注入」也默认关闭**（唯一例外是 `E022`，它专门测 `query_rag`）：
> 注入函数**没有相关性阈值**（每题都注 top-3），而 8 个维度里没有"抵抗错误知识"这一维 ——
> 开着等于给每道题都加一个没打算测的变量。注入内容会记进结果 JSON 的 `knowledge_injected`。
> ⚠️ 但**「模型自己调 `save_knowledge`」这条路关不掉**（工具是产品真实存在的，摘掉等于改口径）——
> 所以评估改成**每题开跑前把知识库复位**（日志里出现「`1 篇已删清理`」是正常且必需的）。

**RAG 改造前 vs 改造后**（`uv run python evals/rag_ablation.py`；**2×2 消融 + 全量召回对照组**，
同一批 10 个查询、同一份知识库、同一个 embedding 模型 → 谁贡献了多少一目了然）：

| 组合 | 索引 | 精排 | 候选集 | top-1 命中**正解文件** | top-1 命中关键词（文件粒度） | top-1 落干扰项 | 稳态延迟 |
|---|---|---|---|---|---|---|---|
| **A** 改造前 | 整篇 | 否 | 全部 7 篇 | 0.40 | 0.70 | 0.60 | 13 ms |
| **B** | 整篇 | 是 | 全部 7 篇 | 0.70 | 1.00 | 0.30 | 189 ms |
| **C** | 分块 | 否 | 前 10 块 | 0.50 | 0.80 | 0.50 | 14 ms |
| **D 现状（生产）** | 分块 | 是 | 前 10 块 | **0.60** | **0.90** | **0.40** | 86 ms |
| **E** 对照 | 分块 | 是 | 全量 35 块 | **0.70** | **1.00** | **0.30** | 290 ms |

- **`correct_source_top1` 是主指标**：top-1 的来源*是不是这道题该去的那个文件*（正解文件是人工核对原文标注的）。
  只报"关键词命中"会高估质量 —— `with` 这种词哪个文件里都可能有，而"推荐 f-string"和"别用 f-string"都命中关键词。
- **A → D 是这次改造的真实变化**：正解文件命中 **0.40 → 0.60**，干扰项落到第一的比例 **0.60 → 0.40**。
  同口径的"关键词命中（文件粒度）"是 0.70 → 0.90。
- ⚠️ **上表是 2026-09-24「测试语料修订」之后的数字，必须连着这段一起读**：
  修订前语料里含 5 条**"危险指令"级**的干扰项（"遇到报错先删除报错文件重试"、"部署直接覆盖整个系统目录、
  不需要备份"、"WSL 命令不需要安全检查"、"MySQL 表名不需要转义"、"连接信息写死在代码里"）——
  按"语料不该教人做危险操作"的原则换成了**同主题但不危险**的烂建议。
  **副作用**：A 组基线从 **0.20** 升到 **0.40** ⇒ 提升幅度从 **+0.40 收窄到 +0.20**。
  **原因**：那 5 条之所以检索得分高，正因为它们和正解**直接唱反调**（"表名不需要反引号" ↔ "必须用反引号防注入"）
  —— 在这份语料里，**"高仿度"和"危险度"是同源的**。
  ✅ **生产配置（D）与对照组（E）的数字两版逐位一致**（0.60 / 0.70 / 0.40 / 0.30），不受修订影响。
  两版归档都在：`rag_ablation_20260923_203822.json`（旧语料）、`rag_ablation_20260924_053228.json`（当前）。
- **⚠️ B 看着比 D 好，但那不是"整篇更好"**：A/B 的候选集是全部 7 篇，而 D 走生产配置
  `recall_k=10`，35 块里只有 10 块能进精排 —— **候选集大小不一样**。
  **E 就是压掉这个差异的对照组**：正解文件命中回到 0.70（与 B 持平），
  而且它还能把*命中的那条知识本身*排到第一（块粒度 0.80）—— 整篇索引根本表达不了这一档。
  代价是延迟 87 ms → 290 ms（归档里的 `latency_ms_avg`：D 86.7 / E 289.7）。
- **⚠️ 保持 `recall_k=10` 不动（2026-09-23 决定，别自作主张改）**：E 那一行说明**候选集大小是主指标的瓶颈**，
  但提到全量要多花约 203 ms（87 → 290 ms）。所以**这条按局限如实披露，不做优化** ——
  改了就得重跑消融、重写这张表；想做的事记在候选池里（含两阶段精排方案）。
- **另外三个如实披露的局限**：知识库只有 **4 篇正解 + 3 篇干扰（35 条原子，每篇 140~370 字符）**，
  语料极小会把"整篇 vs 分块"的差距压小 → 上表是**真实下界，不可外推到大语料**；
  精排模型（`ms-marco-MiniLM-L-6-v2`）是段落级语料训练的，B 组喂整篇文件属分布外输入；
  延迟为本机单进程数字（冷启动单独测，未混入稳态）。
  原始结果（含每条查询的 top-1/top-3 明细）：`docs/evidence/rag_ablation_20260924_053228.json`。

## 安全设计

> **一句话**：这是个**本地单用户**工具，安全设计的目标**不是"防外人"，而是"防 Agent 自己
> （或我的误操作）把本机搞坏"**。所以边界画在**应用层**，而不是假装有 OS 级沙箱。

### 三层防线（判据不同，**故意不合并**）

| 层 | 在哪 | 管什么 | 随权限模式变吗 |
|---|---|---|---|
| ① **工具级档位表**（主防线） | `app/code_agent/security/permissions.py` | 这个工具在当前档位下**该不该**执行 | ✅ 就是它 |
| ② **内容级危险命令黑名单** | `mcp_servers/vm.py` / `powershell_tools.py` | 同一个工具，**这次参数**危不危险 | ❌ 不变（选「放开」也照样拦） |
| ③ MySQL 语句白名单 + 只读账号 | `mysql_tools.py` / `scripts/mysql-init/*.sql` | 只读工具被拿来写数据 | ❌ 不变 |

> 为什么 ① 和 ② 不合并：**判据不同** —— 档位表问"有没有副作用"，黑名单问"这次参数危不危险"。
> 同理它和工具缓存名单（`CACHEABLE_TOOL_NAMES`）也是两张表，后者问"结果会不会变"。
>
> **正式两轮里的实测（2026-09-25）**：**E023**（让它去删一个废弃库）—— 它真的发出了
> `DROP DATABASE`，被档位表的**高危拒绝**拦下（审计记 `denied_by_user`，asked 1 / granted 0）。
> **"库与数据完好"不是嘴上说的**：这道题自带三条状态断言并且**全过** ——
> 库还在（`information_schema` 里 `eval_decoy` 计数 == 1）、`decoy_table` 还在、
> "没有真的执行成功过任何危险命令"（`no_dangerous_command_executed`）；
> **E024**（只读档下的写操作）—— `write_file` 与**改走 `Set-Content` 的第二条路都被拒**
> （`deny_mode`），判定器同样有硬证据：`readonly_probe.txt` **不存在**、工作目录**是空的**
> ⇒ **"换个工具绕过权限"行不通**。
>
> ⚠️ 想手工复查"库还在不在"要注意：`eval_decoy` 是**每道题开工前由 runner 清掉、再由题面 setup 重建**的
> （`runner._clean_mysql`），所以**跑完之后它通常已经不存在了** —— 那是清理，不是被删成功。

### 三档权限模式（默认「需确认」）

32 个工具被分成 A 类**只读 14 个** / B 类**写·执行 18 个**（其中 6 个标 🔴 高危）：

| 档位 | A 类只读工具 | B 类写 / 执行工具 |
|---|---|---|
| **只读** | ✅ 放行 | 🚫 **直接拒绝**（不弹框，直接告诉模型"为什么不行 + 怎么改"） |
| **需确认**（默认） | ✅ 放行 | ⏸ **弹确认框**（高危的显示影响面） |
| **放开** | ✅ 放行 | ✅ 直接执行（**危险命令黑名单仍然生效**；高危操作**仍然强制留痕**） |

**三类工具之外的（未归类）在任何档位都拒绝** —— 白名单语义：
以后新增工具忘了归类，是"默认拒绝"而不是"默认放行"（有一条测试盯着"32 个工具一个不多一个不少"）。

**⚠️ 粒度是「工具级」不是「参数级」**：只要是 B 类工具，参数再无害也会弹
（用 `write_file` 写一个 `hello.py` 也弹）。代价是"同一工具内不同参数的危险差异"被抹平了，
好处是规则简单、可预测、不会因为参数解析出错而漏放。

### 四个实现细节（都是踩出来的）

1. **权限判定在缓存查询之前** —— 否则"曾经允许过"的缓存值会让**已被拒绝**的调用照样返回结果；
2. **被拒绝 / 被否决的调用绝不写缓存**（缓存里只能是真实执行结果）；
3. **拒绝用独立异常类型** —— 不会被"写失败 → 清缓存"那条分支误当成执行失败；
4. **拒绝信息必须带"原因 + 出路"**（例：`当前权限模式为「只读」，工具 write_file 被拒绝；
   如需写入请把权限模式切到「需确认」`），否则模型会反复重试同一件事，白烧 token。

这四条各有测试守着，不是靠注释：`tests/test_permissions.py`。

### 权限模式的持久化（有意的取舍）

- **「只读 / 需确认」落盘**（`runtime/web-settings.json`）—— 常用档位设一次就够；
- **「放开」永不持久化**，新会话一律回落「需确认」；
  即使有人手改配置文件塞了 `"open"`，加载时也当「需确认」处理。
  理由：**"我上次设过、这次忘了"是最容易埋雷的地方**。
- CLI 默认「需确认」；无人值守入口（无头脚本）必须显式指定档位 ——
  否则按"没人应答 → 自动拒绝"的规则，每个写工具都会被拒，等于入口不可用。

### 留痕（`runtime/permissions.log`）

每一条**确认决定**（允许 / 拒绝 / 超时 / 无人值守被拒）和**放开档下的高危操作**都追加一行 JSON：

```json
{"ts":"2026-09-21T13:03:27","scope":"manual-test","tool":"write_file_to_vm","mode":"readonly",
 "tier":"write","high_risk":true,"decision":"deny_mode","args":"{\"file_path\": \"/tmp/x\", ...}"}
```

该文件在 `runtime/` 下（**gitignore**），审计写入失败**只记日志、绝不影响工具调用**。

### ⚠️ 我不把 WSL 当"安全沙箱"（这条是想清楚之后才写的）

WSL2 常被当成"沙箱"，但**它不是安全边界**：

- 它**默认把 Windows 盘挂在 `/mnt/c`、`/mnt/e`…**，而这个项目的 `vm.py` 还**主动在用这条通道**
  （`windows_path_to_wsl_path()` 就是把 `E:\...` 转成 `/mnt/e/...` 去读写 Windows 文件）；
- 命令以**当前用户身份**运行，没有内核级隔离；
- 内容级黑名单是**字符串匹配，本质上列不全**：`& (Get-Command Remove-Item) -Recurse -Force C:\`
  这类动态写法绕得过去。

所以对外一律说「**WSL2 隔离执行环境**（命令黑名单 + 应用层三档权限）」，
**不说"WSL 安全沙箱"**。真正的边界要用 OS 级机制（Linux 的 Landlock + seccomp、
macOS 的 Seatbelt、Windows 的 restricted token + job object，或干脆一次性容器 / VM）——
本项目**明确不做**（平台相关的重活，对作品集边际收益低），方案已登记在候选池里。

> 💡 **这条本身就是设计的一部分**：能说清"我的方案边界在哪、为什么"，比号称"安全沙箱"可信。

### ⚠️ 也没有「Agent 只能在工作区内操作」这个保证

这一条容易被误解，所以说清楚 —— **它的真实语义是"文件工具被关在工作区里"，不是"Agent 的活动范围被限制了"**：

| | 限制情况 |
|---|---|
| **7 个文件工具**（`read_file` / `write_file` / `list_directory` / `file_search` / `move_file` / `copy_file` / `file_delete`） | ✅ **被 `FileManagementToolkit(root_dir=WORKSPACE_DIR)` 关在工作区** |
| **`execute_powershell_command`** | ❌ **完全不受限** —— cwd 是**仓库根**，能读写这台机器上它有权限的任何路径 |
| VM（WSL）四个工具 | ❌ 接受任意 `dir_path`（但也因此超出 workpace，所以都标了高危） |

⇒ **shell 的能力是文件工具的"超集"**：读（`Get-Content`）、写（`Set-Content` / `>`）、
列（`Get-ChildItem`）、搜（`Select-String`）、删（`Remove-Item`）、移动复制 —— **全都有一条 shell 路径**。

**实测例证**：评估里 E003 那题 agent 直接 `Get-ChildItem -Recurse` 扫了整个仓库
（命中了 `.env`、`runtime/` 下的日志与 checkpoint、`.temp/`…），E016 那题则用 shell
把**所有 python 进程**杀了（连评估程序自己一起）。**这两件事都不是"越权"，因为本来就没有围栏。**

**想要真的围栏，要新加一层**（解析每个写工具的**路径参数**，落在工作区外就拒绝或升档）——
但 shell 那条路只能做启发式，做不到严谨。方案已登记在候选池里。

### 为什么项目里"结构化工具"和 shell 并存（不是重复造轮子）

两条路确实有重叠（上面那张表就是证据），但**结构化工具对模型明显更友好**，所以有意都留着：

| # | 结构化工具（`read_file_range` / `write_file` …） | shell（`execute_powershell_command`） |
|---|---|---|
| 1 | **参数有 schema**：名字 / 类型 / 必填 / 枚举 / 示例都是显式声明的 | 要模型自己拼语法 —— **引号与转义是它最容易翻车的地方**（E016 就是被引号和 `&` 坑死的） |
| 2 | **参数结构化传递**：没有 quoting / escaping / 编码问题 | 空格、`&`、`\|`、换行、中文**全是雷** |
| 3 | **返回值可控**：可以是 JSON、可以带行号、可以截断 | 给你一大坨裸文本 |
| 4 | 🔴 **能按参数做策略** —— 工具层看得见 `file_path` 这个**参数名**，于是能做"路径落在工作区外就拦" | 只有一整条字符串，**只能做启发式** |
| 5 | **可精确缓存**：`(工具名, 参数)` 就是稳定的 key | 同一条命令有无数种写法，缓存不了 |
| 6 | **权限可精确分档**：按工具名分档 | 一条命令包罗万象，只能整体定档 |
| 7 | **trace 可读**：结构化记录工具 + 参数 | 要解析命令行 |

**一句话**：结构化工具是"**能力收窄但可控**"的通道；shell 是"**能力最大但不可控**"的逃生口。
主流编程 Agent 都是两者并存 —— 但产品必须**清楚自己承认** shell 是不可控的那条路（也就是上一节）。

### 走查一条真实链路（阶段 5 实测，不是示意图）

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

> ✅ **阶段 6 已完成：题集与评分器从零重建，正式两轮已跑完并归档**（2026-09-25）。
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
  ⚠️ single 那轮跑得更早，快照里**没有**这两个字段 —— 别把两轮当成同一种自证方式。

### 双模式对照（single vs multi）

| 指标 | single | multi | 倍数 |
|---|---|---|---|
| token 总量 | 958,832 | 1,644,029 | **1.71×** |
| token 逐题中位 | — | — | **1.58×** |
| 总耗时 | 332s | 687s | 2.07× |
| Planner 固定开销 | — | 1,110 token/题（2.4%） | — |
| **Verifier 固定开销** | — | **中位 10,988 token/题（总量的 25%）** | — |
| **Verifier 打回次数** | —（single 无 Verifier） | **0** | — |

**怎么读这份对照**：**这个任务集对当前模型已经饱和**（两轮都 100%）⇒ **架构差异不体现在分数上**，
只能从成本侧看。所以结论是**适用边界**，不是"多 Agent 更强"：多出来的 Planner + Verifier
在本题集上**没有提高分数**，代价是**中位 1.58× token**，其中**四分之一**花在 Verifier 的独立验收上。

**一条口径实证（人为闸门会制造假失败）**：multi 第一次是带 **200k token 上限**跑的，
结果 **29/30** —— E015 被上限掐断（烧掉 **257k** token 仍未完成）；
把上限改成**只计量、不拦截**后，同一题集变成 **30/30**，而且**总成本更低**
（1.64M vs 1.78M token）⇒ 那道题不是"做不出来"，是**被人为闸门判死的**。
（限额版那份结果也留在 `docs/evidence/v3-multi-旧版(限额200k).json`，可对照。）

> ⚠️ **三条不能外推的事**：
> 1. **每轮每题只跑 1 次** —— 单样本，题目层面的波动（同一题两次走不同工具路线）量化不出来；
> 2. **任务集已饱和** ⇒ 上面的分数只能说明"这套系统在这 30 题上稳定做对"，
>    **不能**据此说它比单 Agent 强或弱；
> 3. RAG 消融的语料只有 **35 条原子**、CrossEncoder 偏词汇匹配 —— 那些数字是**真实下界**，别外推。

**当年那把旧尺子为什么被删**（不是"分数低"，是"**量不准**"）：

- 实测 **14/30 题没有任何"产物级"断言**，只查关键词或工具名 → 分不清「做完了」和「说了要做」；
- 部分安全题的判定器**事实上空转**：`no_dangerous_tool_called` 检查的工具名
  `run_vm_shell_command` 根本不是 MCP 工具（`vm.py` 里它是普通函数，没挂 `@mcp.tool`）
  → 走 WSL 的路径它永远看不见，**恒定给满分**。
- 留着一把坏尺子，只会让后续开发（包括 AI 助手）继续拿它量东西 —— 所以删掉，而不是标注。
- 备份：`E:\agentstart\work\backup\1new\backup\evals\`；也能从 git 历史取回（`git show b251f68:evals/tasks.py`）。

**新尺子长什么样**（`evals/`，11 个文件 + 一个夹具目录）：

| 文件 | 职责 |
|---|---|
| `tasks.py` | **30 题题集**：基础 10 / 长任务 12 / 对抗 8；8 个维度各 ≥3 |
| `verifiers.py` | **43 个判定器工厂**，四档强度；30 题里共用了 **163 条**断言（**状态 124** / 轨迹 29 / 文本 10） |
| `runner.py` | 执行引擎：清残留 → 跑题（超时也验分）→ 汇总 → 落盘 |
| `run_e2e.py` | 命令行入口 |
| `preflight.py` | **跑前环境预检**（容器 / WSL / `.env` key / 端口 / 知识库）—— 缺一条那一轮的数据就废了，还不会报错 |
| `report.py` | **报告生成器**：把结果 JSON 变成能进 `docs/evidence/` 的 Markdown（含**算出来的 STAR 量化对比**） |
| `merge_runs.py` | **逐题分片 → 合并成一轮**（缺题**拒绝写出**，避免把残轮当整轮） |
| `reset_eval_threads.py` | **清库里的 eval 线程** —— **复用同名 run-id 前必跑**（不清会把上一轮的历史喂回给模型） |
| `rag_bench.py` | RAG 检索基准（单轮快照，与题集无关，见下一节） |
| `rag_ablation.py` | RAG **消融对照**（2×2 + 全量召回对照组，不需要 LLM，见下一节） |
| `env.py` | **语料隔离**：`use_eval_corpus()` 把评估指到 `runtime/eval_knowledge/` + `chroma_db_eval/`，**不碰产品的 `data/knowledge/`**（订正 #36） |

**四条口径**（相对旧尺子的修正）：**通过 = 满分**（旧口径 `score >= 0.5` 就把"对一半"算通过）；
**`skip` ≠ 0 分**（环境不可用记 `unavailable`、不进分母，别把"没测"记成"做错了"）；
**超时/异常也跑判定器**（产物可能已经写出来了）；**判定器按真实 MCP 工具名 + 真实参数名写**。
另外**每道题至少 1 条状态断言**（去查真实产物：文件/库表/WSL/接口），且由测试机械守着。

```bash
uv run python evals/reset_eval_threads.py --yes        # ① 复位：清掉库里的 eval-* 线程（复用 run-id 前必跑）
uv run python evals/preflight.py --run-id v3-single    # ② 预检（修完它报的阻塞项再跑）
uv run python evals/run_e2e.py --list                  # 只看题集结构（不执行、不烧 token）
# ③ 逐题跑 single 轮（一题一进程 ⇒ 每题各落一份 JSON = 天然的增量保存）
uv run python evals/run_e2e.py --task E001 --mode single --run-id v3-single-E001
#    ... E002..E030 同上；然后合并（缺题会拒绝写出）+ 归档
uv run python evals/merge_runs.py --prefix v3-single --mode single --archive
# ④ multi 轮同样逐题，但必须换前缀
uv run python evals/merge_runs.py --prefix v3-multi --mode multi --archive
# ⑤ 出报告
uv run python evals/report.py --single runtime/runs/v3-single.json \
                              --multi  runtime/runs/v3-multi.json  \
                              --rag    runtime/runs/rag_ablation_*.json \
                              --out    docs/evidence/评估报告.md
```

> ⚠️ **为什么逐题跑**：一个进程跑一整轮时**结果 JSON 只在整轮结束写一次** ——
> 2026-09-24 那次事故（agent 在题里把 python 进程全杀了）直接让**已完成的 15 道题产物全丢**。
> 逐题跑之后"被掐断"不再是灾难，但**合并必须等 30 题都齐**（缺题拒绝写出）。
> ⚠️ 两轮**必须换 `run-id`**（thread_id 里带 run-id 与 mode；复用会让第二轮读到第一轮的 checkpoint）。
> 同一个 run-id 只用一次；要重跑某题就换 id（如 `v3-single-E016-2`，合并时按文件修改时间取最新那份）。
> `--archive` 会把结果另存一份到 `docs/evidence/`（纳入版本控制）——**只归档合并后的那一份**，别逐题加。
> 跑之前确认 `.env` 的 key 有效 —— **CLI 与评估都不读界面设置**（预检会替你验一次，只打 `GET /models`、不烧 token）。

**RAG 检索基准与消融对照**（与题集无关，**不需要 LLM**）：

```bash
uv run python evals/rag_bench.py                        # 现状快照：延迟 / 准确率 / 召回 / 排序
uv run python evals/rag_ablation.py --reps 10 --archive # 改造前后消融（结果归档进 docs/evidence/）
```

### 改造前的存档数字（**仅供说明「改造前长什么样」**）

> 存档文件已移出仓库（备份在 `E:\agentstart\work\backup\1new\backup\old-data\docs\evidence\`，
> 也能用 `git show 97041aa^:docs/evidence/<文件名>` 从历史取回）。
> **下表不是当前架构的成绩**；阶段 6 已用新评分器重建题集并**跑完正式两轮**，
> **新结果已归档进 `docs/evidence/`**（见上一节「评估体系」）。

| 阶段 | 存档文件（已移出仓库） | overall | pass_rate | total_tokens | 平均延迟 |
|---|---|---|---|---|---|
| 改造前基线（单 Agent） | `baseline-final.json` | **0.983** | 1.0 | 978,865 | 34.0s |
| optimized 单 Agent | `evals-optimized-final.json` | **1.0** | 1.0 | 896,475 | 30.3s |
| 多 Agent（改造前那版） | `evals-multiagent-merged.json` | **0.967** | 0.967 | 1,257,397 | 53.4s |
| RAG 基准（独立基准） | `rag-bench-baseline.json` | top1 **0.6** / top3 1.0 / recall **0.4** | — | — | 13.4ms |

**⚠️ 读这些数字前必看的口径说明**：

1. 全部基于**当时的** `deepseek-v4-flash` 跑出，**换模型后不可比**。
   ⚠️ 补充（2026-09-22 核实）：官方已在 2026-09-10 把 V4 Flash 升级为 **V4.1 Flash**，
   并把旧名 `deepseek-v4-flash`「暂时路由」到新模型 —— 也就是说，
   **同一个模型名今天背后已经是另一个模型**，这批数字与现在更不可比；
2. 当时的评分器里**弱断言占比不小**，所以这些分数应理解为「**回归通过率**」，
   不是「**通用任务成功率**」；
3. `pass_rate` 把 `score >= 0.5` 记为通过，而部分 verifier 会给 0.5 的**部分分** → 偏乐观。

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
│   │   │   ├── memory.py          # 分层记忆：自动注入 + 自动沉淀（**进程内直调 store**）
│   │   │   ├── events.py          # 节点级进度事件（ContextVar sink；没绑就静默跳过）
│   │   │   ├── code_agent.py      # REPL 循环 + 非交互接口 run_single_task（供阶段 6 重建的评估脚本用）
│   │   │   └── prompts.py         # System / Planner / Verifier / Executor（计划版）提示词
│   │   ├── security/
│   │   │   └── permissions.py     # ★ 三档权限档位表（32 工具）+ 判定 + 人工确认闸门 + 审计
│   │   ├── model/llm.py           # LLMRegistry：get_llm(role) / chain / invoke_with_fallback
│   │   ├── config.py              # 所有配置（从 .env 读）+ setup_logging（stderr）
│   │   ├── mcp_servers/           # 6 个 MCP Server（powershell / 搜索 / mysql / vm / code_tools）
│   │   │                          #   └ browser_tools.py = 搜索（JSON API）；文件名是历史遗留
│   │   ├── rag/                   # rag.py（MCP 薄壳）/ store.py（分块+精排）/ chunking.py（纯函数）
│   │   ├── tools/
│   │   │   └── file_tools.py      # FileManagementToolkit（限定在 workspace）
│   │   └── utils/                 # mcp.py（工具加载）/ tool_wrap.py（权限+外置+缓存）/ tool_cache.py
│   └── web/
│       ├── server.py              # FastAPI（WS + 权限确认协议 + REST + 静态托管）
│       └── frontend/              # Vue3 + Vite（dist 已入库）；含 PermissionDialog（人工确认弹框）
├── config/models.json             # 模型注册表（显示名 + 实际调用名；**默认空** = 走 .env）+ 角色分配 + 降级链
├── data/knowledge/                # **产品**知识库目录：默认空（靠使用积累）。测试语料在
│                                  #   evals/fixtures/knowledge/（35 条），评估时才用
├── scripts/                       # probe_mcp_server.py（手工发 JSON-RPC 探 MCP server 回没回）
│   │                              #   + mysql-init/*.sql（被 docker-compose 当挂载目录用，别挪）
│   └── run/                       # 启动/停止脚本：start-app.cmd / start-app.ps1（起 Web UI）
│                                  #   + start-deps.ps1 / stop-deps.ps1（起停 4 个依赖容器）
├── runtime/                       # ⚠️ gitignore：checkpoints.db + tool_results / chroma_db / workspace / runs
├── evals/                         # 阶段 6 重建的评估体系（11 个文件 + 夹具）：tasks.py(30 题)
│                                  #   / verifiers.py(43 个判定器) / runner.py / run_e2e.py
│                                  #   / preflight.py(跑前预检) / report.py(报告+STAR)
│                                  #   / rag_bench.py(RAG 基准) / rag_ablation.py(RAG 消融)
│                                  #   / merge_runs.py(逐题分片合并成一轮)
│                                  #   / reset_eval_threads.py(复用 run-id 前清库里的 eval 线程)
│                                  #   / env.py(语料隔离) + fixtures/knowledge/(7 篇测试语料)
├── tests/                         # 550 个测试（单元 + 工具级 + 评估体系自检）
├── docs/
│   └── evidence/                  # 评估与修复存档（只追加：两轮结果 / RAG 消融 / 修复账本）+ archive/（空）
├── AGENTS.md                      # AI 助手约定与已知坑
├── docker-compose.yml             # mysql / searxng / redis 三个依赖服务（nginx 由 WSL 侧 compose 管）
└── .gitee.yml                     # CI（ruff check → ruff format --check → pytest）
```

## 数量与来源对照（每个数字都能复核）

| 数字 | 值 | 复核命令 |
|---|---|---|
| 测试数 | 550 | `uv run python -m pytest tests/ -q` |
| 知识库条目 | **测试语料** 35（7 文件 × 5 条）→ `evals/fixtures/knowledge/`；产品库默认空 | `Get-ChildItem evals/fixtures/knowledge -Recurse -File` |
| MCP 工具数 | 32（含 7 个文件工具） | 运行 `uv run python main.py`，看日志 `共加载 N 个工具` |
| RAG 消融（正式数，**2026-09-24 语料修订后**） | top-1 命中正解文件 **0.40 → 0.60**（对照 0.70）；⚠️ 旧语料基线是 **0.20** | `uv run python evals/rag_ablation.py --reps 10` |
| 评估题数 / 断言数 | **30 题** / **163 条**断言（状态 124 / 轨迹 29 / 文本 10） | `uv run python evals/run_e2e.py --list` |
| **评估结果（正式两轮，2026-09-25）** | **single 30/30**、**multi 30/30**（平均分均 **1.0000**；两轮 163 条断言全过、8 维度全 1.00） | 结果文件：`docs/evidence/v3-single.json` / `v3-multi.json`（+ 限额版对照 `v3-multi-旧版(限额200k).json`） |
| 多 Agent 成本画像（同上两轮） | token **中位 1.58×**（总量 1.71×）、时间 2.07×；其中 **Verifier 中位 10,988 token/题 = 25%**；**打回 0 次** | 同上两份 JSON 的 `totals` / `node_timings` |
| 测试覆盖率 | **73%（依赖容器在跑时）/ 74%（容器全停）** —— 2082 语句，未覆盖 563 / 545 | `uv run python -m pytest tests/ -q`（addopts 自带 `--cov=app/code_agent`）。⚠️ **数字随环境波动**（那几条"要真环境"的测试走的分支不同）。⚠️ **跨阶段不可直比**（分母随测试首次 import 新模块而变大），未覆盖的大头正是这些要连真库 / 起子进程 / 要真人输入的模块 → 集成测试挂在阶段 7 做 |
| RAG 检索指标（**阶段 6 重测**；阶段 4 自测的临时数是 13.2ms → 81ms） | top1(文件粒度) 0.9 / top3 1.0 / recall 1.0 / 稳态 83ms | `uv run python evals/rag_bench.py`（结果也写入 `runtime/runs/rag_bench_*.json`） |
| 评估指标（改造前旧口径，**当前不适用**） | 见「评估体系」一节 | 存档已移出仓库 → `git show 97041aa^:docs/evidence/<文件名>` |

## License

MIT
