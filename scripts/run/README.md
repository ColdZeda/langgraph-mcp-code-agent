# scripts/run —— 运行与依赖的起停脚本

这四个都是"把东西跑起来"的脚本，**但它们不是同一个开关**：

| 脚本 | 管什么 | 怎么用 |
|---|---|---|
| `start-app.cmd` | **双击入口**（Windows 默认不能双击 `.ps1`），只负责转调下面那个 | 直接双击 |
| `start-app.ps1` | **启动 Web 界面**：查 uv → 查容器 → 查 dist → 查端口 → **前台**跑 `uvicorn:8000` | `.\scripts\run\start-app.ps1`（`-Port 8001` 换端口；`-Dev` 另开窗口跑前端热更新） |
| `start-deps.ps1` | **启动 4 个依赖容器**：MySQL / SearXNG / Redis（仓库根 compose）+ nginx（WSL 里的 `~/nginx`） | `.\scripts\run\start-deps.ps1` |
| `stop-deps.ps1` | **停**那 4 个容器（`docker compose stop`，容器保留 → 下次起得快） | `.\scripts\run\stop-deps.ps1` |

## 三条要记住的

1. **关 Web ≠ 关依赖**：在 Web 窗口按 Ctrl+C 只停掉应用本身，MySQL / Redis / 搜索 / nginx **还在跑**。
   它们其实不必停（4 个容器都是 `restart: unless-stopped`，开着 Docker Desktop 就会自动拉起）。
2. **`.ps1` 必须保持 UTF-8 with BOM**：Windows PowerShell 5.1 读**没有 BOM** 的 UTF-8 会按 ANSI 解，
   中文全是乱码（这是阶段 4 的订正 #10）。改完请用带 BOM 的方式保存。
3. **这些脚本在 `scripts/run/` 下，找仓库根要往上两层**（`..\..`）。
   只往上找一层会解析成 `scripts\` → `uv run` 找不到 `pyproject.toml`、
   `docker compose` 找不到仓库根的 `docker-compose.yml`。

## 另外两个留在 `scripts/` 的（刻意不挪）

| 文件 | 为什么留在原地 |
|---|---|
| `scripts/probe_mcp_server.py` | 排查"某个 MCP server 到底回没回"的探针（手工发 JSON-RPC），不是启动脚本 |
| `scripts/mysql-init/*.sql` | 被仓库根 `docker-compose.yml` 当**挂载目录**用（首次初始化建只读账号 `agent_readonly`）—— 挪走会**静默失效**，且已初始化的命名卷不会重跑 |
