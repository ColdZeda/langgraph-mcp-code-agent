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
// 键（key）= 下拉框里给用户看的显示名；model = 实际发给 API 的模型名。
// 两者分开是为了防官方改名/下线：2026-09-10 官方把 V4 Flash 升级成 V4.1 Flash，
// 并把旧名 deepseek-v4-flash「暂时路由」过去 —— 内部一律用官方现名，显示名自己控制。
"models":   { "deepseek-v4.1-flash": { "model": "deepseek-flash",
                                       "base_url": "https://api.deepseek.com" } },
"roles":    { "planner": "deepseek-v4.1-flash", "executor": "deepseek-v4.1-flash",
              "verifier": "deepseek-v4.1-flash", "router": "deepseek-v4.1-flash" },
"fallback": { "executor": [] }   // 填备用模型即启用"主力报错/超时自动降级"
```

- 默认四个角色同一个模型（行为可预期）；可在 **Web UI 的模型设置面板**里分别选，改完热生效。
- **verifier 想单独换模型**要勾上「验收使用异构模型」—— 默认三角色同模型、Verifier 跟随 Executor
  （同一个模型自己验自己容易有一致的盲区，换一个模型能拿到独立视角）。
- **用户也能加"自己的模型"**：面板 →「我的模型」填 `显示名 + 模型名 + API 地址 + Key`，
  存本机 `runtime/web-settings.json`，加完立刻出现在四个角色的下拉框里。
  **每个自定义模型自带凭据**，与内置模型那组全局凭据互不影响（所以"内置用 .env 的 key、
  我的 GLM 用我自己的 key"可以共存）。

> ⚠️ **三处配置的优先级**（从高到低），别搞反：
> `runtime/web-settings.json`（界面点出来的）**>** `config/models.json`（仓库里的默认）
> **>** `.env`（密钥与兜底的模型名/地址）。
> 所以**在界面上存过一次模型选择后，改 `models.json` 的 `roles` 是不生效的** —— 要回界面改。
>
> - **`web-settings.json` 里含明文密钥**（内置凭据 + 每个自定义模型的 key）。它已 gitignore、
>   不进仓库，但**不要分享这个文件**；界面只回显尾号 4 位。
> - **改 `.env` 必须重启服务才生效**（配置在进程启动时读一次）；而界面改的会热生效。

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
  分别选模型**（数据源 = `config/models.json` 的内置项 +「我的模型」里用户自己加的）；
  设置只存本机 `runtime/web-settings.json`（**含明文密钥**，已 gitignore、不进仓库）
- 「我的模型」：填 `显示名 + 模型名 + API 地址 + Key` 就能接任意 OpenAI 兼容服务
  （中转站、GLM、Qwen…），**每个模型自带密钥**；旁边的「测试连接」测的就是你刚填的这一组
  （会回显**实际测的是哪个模型名**，避免"测试通过但跟你选的模型无关"）
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

> ⚠️ **旧的 30 题端到端题集（`evals/tasks.py` / `verifiers.py` / `run_e2e.py` / `compare.py`）
> 已于阶段 5 从仓库删除。** 它是**改造前**那把尺子，问题不是"分数低"而是"量不准"：
>
> - 实测 **14/30 题没有任何"产物级"断言**，只查关键词或工具名 → 分不清「做完了」和「说了要做」；
> - 部分安全题的判定器**事实上空转**：`no_dangerous_tool_called` 检查的工具名
>   `run_vm_shell_command` 根本不是 MCP 工具（`vm.py` 里它是普通函数，没挂 `@mcp.tool`）
>   → 走 WSL 的路径它永远看不见，**恒定给满分**。
>
> 留着一把坏尺子，只会让后续开发（包括 AI 助手）继续拿它量东西 —— 所以删掉，而不是标注。
> **题集与评分器由阶段 6 重做**；在那之前，本项目**不报告任何端到端通过率**。
> 备份：`E:\agentstart\work\backup\1new\backup\evals\`（6 个文件，与删除前逐字节一致）；
> 也能从 git 历史取回，如 `git show 8d0ab78:evals/tasks.py`。

**当前 `evals/` 只保留 RAG 检索基准**（`rag_bench.py`，阶段 4 新写、与题集无关）：

```bash
uv run python evals/rag_bench.py      # 结果写入 runtime/runs/rag_bench_*.json
```

### 改造前的存档数字（**仅供说明「改造前长什么样」**）

> 存档文件已移出仓库（备份在 `E:\agentstart\work\backup\1new\backup\old-data\docs\evidence\`，
> 也能用 `git show 1ea2687^:docs/evidence/<文件名>` 从历史取回）。
> **下表不是当前架构的成绩**，阶段 6 会用新评分器重跑并归档。

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
├── config/models.json             # 模型注册表（显示名 + 实际调用名）+ 角色分配 + 降级链（进版本控制）
├── data/knowledge/                # 知识库源文件：35 条（7 个文件 × 每文件 5 条）
├── scripts/                       # probe_mcp_server.py（手工发 JSON-RPC 探 MCP server 回没回）
│   │                              #   + mysql-init/*.sql（被 docker-compose 当挂载目录用，别挪）
│   └── run/                       # 启动/停止脚本：start-app.cmd / start-app.ps1（起 Web UI）
│                                  #   + start-deps.ps1 / stop-deps.ps1（起停 4 个依赖容器）
├── runtime/                       # ⚠️ gitignore：checkpoints.db + tool_results / chroma_db / workspace / runs
├── evals/                         # 只剩 RAG 检索基准 rag_bench.py（旧 30 题集已于阶段 5 删除，阶段 6 重建）
├── tests/                         # 308 个测试（单元 + 工具级）
├── docs/
│   └── handover.md                # 交接文档（evidence/ 与 archive/ 的内容已移出仓库）
├── AGENTS.md                      # AI 助手约定与已知坑
├── docker-compose.yml             # mysql / searxng / redis 三个依赖服务（nginx 由 WSL 侧 compose 管）
└── .gitee.yml                     # CI（ruff check → ruff format --check → pytest）
```

## 数量与来源对照（每个数字都能复核）

| 数字 | 值 | 复核命令 |
|---|---|---|
| 测试数 | 308 | `uv run python -m pytest tests/ -q` |
| 知识库条目 | 35（7 文件 × 5 条）；分块后 = 35 块 | `Get-ChildItem data/knowledge -Recurse -File` |
| MCP 工具数 | 32（含 7 个文件工具） | 运行 `uv run python main.py`，看日志 `共加载 N 个工具` |
| 测试覆盖率 | **68%**（1936 语句 / 613 未覆盖） | `uv run python -m pytest tests/ -q`（addopts 自带 `--cov=app/code_agent`）。未覆盖的 601+ 条里 **84% 集中在 6 个模块**，共同点是"要真环境才能跑到"（连真库 / 起子进程 / 要真人输入）→ 集成测试挂在阶段 7 做 |
| RAG 检索指标（**阶段 4 临时数**，阶段 6 重测） | top1(文件粒度) 0.9 / top3 1.0 / recall 1.0 / 稳态 81ms | `uv run python evals/rag_bench.py`（结果也写入 `runtime/runs/rag_bench_*.json`） |
| 评估指标（改造前旧口径，**当前不适用**） | 见「评估体系」一节 | 存档已移出仓库 → `git show 1ea2687^:docs/evidence/<文件名>` |

## License

MIT
