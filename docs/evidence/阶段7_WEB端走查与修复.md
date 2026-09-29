# 阶段 7 · Web 端走查与修复（T7.5）

> **给谁看**：接手这个项目的人 / 下一个会话的 AI。
> **本文件的定位**：阶段 7 的 Web 端走查账本 —— 走了哪些路、发现什么、怎么修的、证据在哪。
> **走查日期**：2026-09-27（会话管理部分 2026-09-29 补完）。
> **走查方式**：本机真实跑起来的服务（`http://127.0.0.1:8000`）+ **无头 Chrome 经 CDP 驱动真实点击/输入**，
> 截图存 `docs/evidence/阶段7_web走查/`。

⚠️ **为什么不是 browser-skill**：那套要装 `bsk` CLI，本机没有（插件直接报错）。
退路是 `chrome --headless=new --remote-debugging-port=9333` + CDP：
脚本在 `.temp/cdp-shot.mjs`（`.temp/` 已 gitignore），只操作本机 dev server、**不碰任何登录态**。
复现命令见本文末尾。

---

## 一、修复清单（每条都带"现象 → 根因 → 修法 → 证据"）

### 1. 答案不渲染 Markdown：`##` / `**` / 表格全是源码

- **现象**：问"深圳今天天气"，回复里的 `## 🏙 深圳今日天气`、`**实况**`、`| 时间 | 天气 |` 表格
  **原样显示成纯文本**，读起来很费劲（截图 `01-历史回放-Markdown修复前.png`）。
- **根因**：不是"渲染坏了"，是**从来没渲染过** —— 两处渲染点都是纯文本 + `white-space: pre-wrap`：
  ① `ResultCard.vue` 的 `.answer-text`（本轮结果）；
  ② `ChatView.vue` 的 `.assistant-bubble`（**历史会话回放**走的就是这一支）。
  前端依赖里也没有任何 markdown 库。
- **修法**：新增 `MarkdownText.vue` —— `marked`（gfm + `breaks: true`）渲染 → **`DOMPurify.sanitize()`** → `v-html`；
  两处渲染点都改用它。用户气泡 / 运行中 / 错误三类仍走纯文本（那三类不该当 Markdown 解析）。
- **证据**：`01`（修前）vs `02`（修后）同一会话截图；**消毒实测**（真实浏览器里跑 6 个用例）：

  | 输入 | 结果 |
  |---|---|
  | `<script>window.__pwned=1</script>hello` | → `hello`（整段剥掉） |
  | `<img src=x onerror="…">` | → `<img src="x">`（属性剥掉） |
  | `<iframe src=…>` / `<svg onload=…>` | → 空 |
  | `[点我](javascript:…)` | → 保持纯文本，不成链接 |
  | `window.__pwned` | **始终 `undefined`** |

  ⚠️ 消毒不是可选项：`finalResponse` 是**模型输出 + 夹带工具回显**的不可信内容，直接 `v-html` = 自造 XSS。

### 2. 任务跑完后残留一条"协作中…"气泡

- **现象**：任务结束后列表里永远挂着 `Planner → Executor → Verifier 协作中…`（截图 `03` 的修前版本）。
- **根因**：`store.js` 收到 `result` 时只是把占位消息的 `phase` 改成 `done`，**没删掉它**；
  而 `ChatView` 的最后一条分支要求 `m.text` ⇒ 它就以"一句普通回复"的身份留在列表里。
- **修法**：收到 `result` 时**移除**占位气泡（`filter(m => m.phase !== 'running')`）；
  收到 `error` 时**复用**那一条改成错误气泡（原来是"改 phase + 再 push 一条" ⇒ 两条错误气泡叠着）。
- **证据**：`03`/`04` 里结果卡上方已无残留。

### 3. 结果卡的"未验收"标签说错话

- **现象**：标签永远是 `— 本轮未验收（简单任务直通）`。
- **根因**：`ResultCard.vue` 写的是 `mode === 'single' ? 'single' : '简单任务直通'`
  ⇒ **任何非 single 的情况都被说成"简单任务直通"**：multi 模式下 Verifier 没产出可解析裁定
  （上游抽风返回整句错误文本，实测发生过）也会被显示成"设计如此"，把"少验收了一次"粉饰掉。
- **修法**：按真实原因分三档 —— `single 模式，无验收环节` / `auto 判为简单任务，直通 Executor` /
  `${mode} 模式未产出裁定`。
- **证据**：截图 `03`/`04` 底部显示 `— 本轮未验收（auto 判为简单任务，直通 Executor）`。

### 4. 侧栏历史会话只有 8 位随机 ID，时间是一长串

- **现象**：`9a11a0a1` + `2026/9/27 19:17:46`，一屏十几条时没法扫。
- **根因**：`thread_id` 是 `str(uuid.uuid4())[:8]`（**没有任何语义**，`eval-*` 那种可读名字是评估脚本自己传的
  run-id）；时间走 `toLocaleString()` 全量输出。会话从来没有"标题"这个概念。
- **修法**：
  - **标题 = 新会话的第一条用户消息**（首行、压平空白、截 24 字），写在侧车库；
  - 侧栏显示标题（没标题回落短 ID）+ **相对时间**（`今天 19:17` / `昨天 21:04` / `09-23 13:19`）+ 步数；
  - hover 才出 📌/✏️/🗑，标题 hover 露出真实 `thread_id`（排查用）。
- **证据**：`03`（一条会话，标题 `1+1 等于几？`）、`04`（两条）。

### 5. 侧栏混着 13 条"不是用户对话"的会话

- **现象**：列表里混着 `eval-v3-single-E007-single`（阶段 6 评估留下的 4 条）、
  `probe-*`（探接口留下的 6 条）、`smoke6-1` / `nowrap-probe` / `probe-memory-restart` 等。
- **根因**：`/api/sessions` 把 `checkpoints` 库里所有 thread 一股脑列出来。
- **修法**：**系统线程**（前缀 `eval-` / `probe-` / `smoke` / `nowrap-`，写在
  `app/web/sessions.py` 的 `SYSTEM_THREAD_PREFIXES`）**默认不显示**，底部一行小字
  `显示系统线程 (N)` 可展开；展开后另给一个 `清空系统线程`（**只删这些前缀，用户会话一条不动**）。
  ⚠️ 两行小字**在计数为 0 时一个字都不显示** —— 全新用户看到的侧栏不会多出任何陌生控件。
- **证据**：`05`（8 条用户会话 + `显示系统线程 (13)`）。

### 6. 会话**根本没有删除功能**

- **现象**：只能"列出来"和"点开看"，没有删除/重命名入口。
- **根因**：`server.py` 的 8 个路由里，会话相关只有两个 **GET**。
- **修法**（用户 2026-09-27 定的语义）：**删除永远是先软删除** → 进回收站 → 回收站里才能
  「彻底删除」（不可逆）；回收站每条都有「恢复」。另加**重命名**与**置顶**（📌 切换、置顶排最前、
  那条带「置顶」标记）。
- **证据**：`06`（回收站展开：`23 乘以 17 等于多少？` 虚线框 + 「置顶」标记 + 恢复/彻底删除按钮）。

### 7. 产品知识库的内容会被 `git add -A` 一起提交

- **现象**：`data/knowledge/*.txt` **没有任何 ignore 规则**（只有 `.gitkeep` 被跟踪）。
- **根因**：历史遗留 —— 当年没有人往里写东西，所以没人注意到。
- **修法**：`.gitignore` 加 `data/knowledge/*` + `!data/knowledge/.gitkeep`（保留占位）。
  用户自己沉淀的两篇（天气查询那两条）**放回产品库**：那正是"自学习闭环"的产物，不是垃圾。

### 8. CI 在全新容器里**必红**（收集阶段就要 key）

- **现象**：`.gitee.yml` 一跑就红。实测（没有 `.env`）：
  `pytest tests/ --collect-only -q` → **`300 collected, 15 errors`**，退出码 2，
  错误统一是 `ValueError: 模型 API key 未配置，请在 .env 中设置 MODEL_API_KEY`。
- **根因**：`app/code_agent/model/llm.py` **末尾**那句模块级 `llm = get_llm()`
  （阶段 3 留下的"向后兼容别名"）在**被 import 的那一刻**就建 LLM 对象。
  CI 容器是干净的、没有 `.env`（gitignored）⇒ 任何 import 这个模块的测试文件都炸。
  ⚠️ 那行其实**全仓没人用**（只有 `invoke_with_fallback` 里一个同名局部变量）。
- **修法（用户决定：不要大面积改代码）**：`tests/conftest.py` 里
  `os.environ.setdefault("MODEL_API_KEY", "test-dummy-key")` —— **1 行**，
  与它已有的三行同类开关同一位置同一理由（必须在 `app.code_agent.config` 被 import 之前设好）；
  用 `setdefault` 是为了**不覆盖**开发者自己 `.env` 里的真 key。
- **证据（"你的真 key 从来没被用过"）**：把 `.env` 整个挪走、什么环境变量都不设 ⇒ **550 passed**。
  若真有测试拿 key 去调 API，这把假 key 会吃 401、必然失败。

### 9. `test_evals_env` 的断言比它要守的不变量更严（假阳性）

- **现象**：`test_product_knowledge_dir_has_no_fixture_files` 断言"产品知识库目录必须为空"，
  于是**产品正常沉淀一条经验就红**（实测撞到：`data/knowledge/天气查询用wttr.in接口而非搜索.txt`）。
- **根因**：它真正要守的是**订正 #36**："测试语料不许出现在产品库里"，不是"目录必须为空"。
- **修法**：判据改成两条，任一条命中即失败 —— ① **同名**；② **同内容**（sha256 撞上夹具任意一篇）。
  **比"空目录"更强**（连"改名搬运"都管）。
- **证据（红绿都跑过）**：同名拷入 → `1 failed`；改名拷入（内容相同）→ `1 failed`；恢复原状 → `14 passed`。

---

## 二、**没修**的（登记，不装作不存在）

### A. 没有 key 时，服务和 CLI 起不来（import 期副作用）

`llm.py:341-342` 那行仍在 ⇒ `server.py:45` 顶部 import 它时会抛
⇒ **新用户不填 key 直接起服务会崩在启动阶段**。

**为什么这次不动它**（用户 2026-09-27 决定）：README 的「快速开始」本来就写着
"复制 `.env.example` 为 `.env`，**至少填入 API Key**"（第 63 行）并明说"唯一的必填项是 key"
（第 77 行）⇒ 照文档走的人不会撞上。

**代价（如实记）**：**"在面板里填 key"这条路径，在没 key 的机器上到不了**（面板还没打开就崩了）。
**以后想修**：把那行改成惰性（模块级 `__getattr__`，用到 `llm` 名字时才建）或直接删
（全仓无人引用）；顺带还能修掉"热切换模型后别名指向旧快照"的隐患。
⚠️ 修完之后还要看 `runtime.load() → rebuild_agents() → get_llm()` 这条启动链，它同样要求 key。

### B. 权限闸门在**无头**环境下的表现（是设计行为，不是 bug）

走查时让 agent 算 `23 × 17`，它想调 PowerShell 验算 → 权限档是「需确认」+ 无头浏览器里**没有人能点允许**
→ 按 B2"无人应答 → 自动拒绝"被拒 → agent **自己换路**（用除法反验算）并在回答里说明了这件事。
⇒ 这正好是 `error_recovery` 的真实演示，**不是缺陷**；有头环境里用户会看到弹框。

---

## 三、走查带来的量化变化

| | 之前 | 现在 |
|---|---|---|
| 测试条数 | 550 | **584**（+34：`test_web_sessions.py` 25 + `test_web_sessions_api.py` 9） |
| 覆盖率 | 73%（依赖容器在跑）/ 74%（全停） | **73~75%**（还会随"本机产品库里有没有内容"等波动 ⇒ 别把它当固定值引用） |
| 前端依赖 | `vue` | **+ `marked` + `dompurify`**（渲染 + 消毒，见 `.gitignore` 同级的 `package.json`） |
| 前端产物 | `index-NTtdz0Jz.js` / `index-Q3MYYX3T.css` | 见 `dist/index.html`（改前端必须 `npm run build` 并提交新 hash + 删旧 hash） |

---

## 四、怎么复现这套走查（下次照这个来）

```powershell
# ① 起服务（前台或后台都行）
uv run uvicorn app.web.server:app --host 127.0.0.1 --port 8000

# ② 无头 Chrome 截图 / 点击 / 发消息（脚本在 .temp/，已 gitignore）
node .temp\cdp-shot.mjs .temp\shot.png - 1440 1000                     # 只截图
node .temp\cdp-shot.mjs .temp\s1.png - 1440 1000 --say "1+1 等于几？" --await 200
node .temp\cdp-shot.mjs .temp\s2.png - 1000 700  --click ".link-row"   # 展开回收站
#   --click 可以给多次（同一个页面里按顺序点）—— 行内二次确认必须这么做
#   ⚠️ 每次运行都是新页面：Vue 里的"待确认"状态会重置，跨运行点两次是没用的

# ③ 纯截图（不驱动点击）
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --headless=new --disable-gpu `
  --virtual-time-budget=4000 --window-size=1440,1000 --screenshot=.temp\shot.png http://127.0.0.1:8000
```

---

## 五、恢复入口

1. 本文（走查与修复的唯一账本）；
2. 仓库 `AGENTS.md` 的「已知坑 · 会话管理」与「仓库整理」；
3. 会话管理的实现与语义：`app/web/sessions.py` 顶部注释 + `tests/test_web_sessions*.py`；
4. 阶段 7 的任务表与决策：仓库外 `program-fix第八版\阶段7_收尾包装.md` 顶部「执行中状态」；
5. 原始数字与红绿证据：`docs/evidence/阶段6_修复与口径记录.md`（阶段 6）、本文（阶段 7）。
