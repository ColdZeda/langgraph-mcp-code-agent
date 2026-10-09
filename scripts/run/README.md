# scripts/run —— 运行与依赖的起停脚本

这四个都是"把东西跑起来"的脚本，**但它们不是同一个开关**：

| 脚本 | 管什么 | 怎么用 |
|---|---|---|
| `start-app.cmd` | **双击入口**（Windows 默认不能双击 `.ps1`），只负责转调下面那个 | 直接双击 |
| `start-app.ps1` | **启动 Web 界面**：查 uv → 查容器 → 查 dist → **实测端口** → **前台**跑 `uvicorn` | `.\scripts\run\start-app.ps1`（`-Port 8300` 指定首选端口；`-NoFallback` 不许自动换；`-Dev` 另开窗口跑前端热更新） |
| `start-deps.ps1` | **启动 4 个依赖容器**：MySQL / SearXNG / Redis（仓库根 compose）+ nginx（WSL 里的 `~/nginx`） | `.\scripts\run\start-deps.ps1` |
| `stop-deps.ps1` | **停**那 4 个容器（`docker compose stop`，容器保留 → 下次起得快） | `.\scripts\run\stop-deps.ps1` |

## 端口是怎么选的（实测 + 自动退避）

`start-app.ps1` 启动前会**真的 bind 一次**（不是查"有没有人在监听"），然后把结果翻成人话：

- 顺序：`-Port`（默认 8000）→ 8010 → 8020 → 8300 → 8310 → 9000 → 9010 → 9200，**每个都实测**，不行就跳下一个；
  换端口时会打印「✗ 8000 被系统保留 … → 已自动改用 8300」。全部不行才失败退出。
- **错误码判据**：`10048` = 有进程占用（会给 PID）；`10013` = **被系统保留**（Windows 的 Hyper-V/WSL
  会划走 `7927–8126` 这类区间 —— 端口没人用也绑不上，**别去杀进程**）。
  查保留区间：`netsh interface ipv4 show excludedportrange protocol=tcp`。
- 想固定用 8000（管理员）：`net stop winnat` →
  `netsh int ipv4 add excludedportrange protocol=tcp startport=8000 numberofports=1 store=persistent` →
  `net start winnat`。
- 失败时脚本**退出码非 0** ⇒ 双击 `.cmd` 时窗口会停住显示错误（不再"闪退"）。

## 三条要记住的

1. **关 Web ≠ 关依赖**：在 Web 窗口按 Ctrl+C 只停掉应用本身，MySQL / Redis / 搜索 / nginx **还在跑**。
   它们其实不必停（4 个容器都是 `restart: unless-stopped`，开着 Docker Desktop 就会自动拉起）。
2. **`.ps1` 必须保持 UTF-8 with BOM**：`start-app.cmd` 调的是 **Windows PowerShell 5.1**，它读**没有 BOM** 的
   UTF-8 会按 ANSI(GBK) 解 —— 中文乱码之外，**中文字符串末尾的引号还会被吞掉 ⇒ 语法直接坏**
   （`pwsh` 7 下却正常，所以只有双击启动的人会撞上）。改完请用带 BOM 的方式保存；
   守卫测试：`tests/test_launcher_scripts.py`。
3. **这些脚本在 `scripts/run/` 下，找仓库根要往上两层**（`..\..`）。
   只往上找一层会解析成 `scripts\` → `uv run` 找不到 `pyproject.toml`、
   `docker compose` 找不到仓库根的 `docker-compose.yml`。

## 另外两个留在 `scripts/` 的（刻意不挪）

| 文件 | 为什么留在原地 |
|---|---|
| `scripts/probe_mcp_server.py` | 排查"某个 MCP server 到底回没回"的探针（手工发 JSON-RPC），不是启动脚本 |
| `scripts/mysql-init/*.sql` | 被仓库根 `docker-compose.yml` 当**挂载目录**用（首次初始化建只读账号 `agent_readonly`）—— 挪走会**静默失效**，且已初始化的命名卷不会重跑 |
