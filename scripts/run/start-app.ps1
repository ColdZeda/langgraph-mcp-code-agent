<#
.SYNOPSIS
    一键启动 Code Agent-novi 的 Web 界面（前端 + 后端，其实只有一个进程）。

.DESCRIPTION
    为什么一条命令就够：前端 Vue 的构建产物 `app/web/frontend/dist/` **已入库**，
    FastAPI 启动时用 `app.mount("/", StaticFiles(...))` 把它挂到根路径直接发出去，
    所以**运行期没有 Node 进程**（`npm run dev` 只在改前端时才需要，见 -Dev 开关）。

    本脚本做四件事，然后**在当前窗口前台**跑后端（Ctrl+C 即停，不后台、不静默）：
      1. 检查 uv；
      2. 检查 4 个依赖容器（MySQL / Redis / SearXNG / nginx）——**只提示，不自动拉**；
      3. 检查前端产物是否存在；
      4. **实测端口能否绑定**：不行就自动退避到下一个候选端口（见 -NoFallback）。

.PARAMETER Port
    期望端口，默认 8000。⚠️ 它只是**首选**：若被占用/被系统保留，脚本会自动换端口并告诉你。

.PARAMETER NoFallback
    关掉自动换端口：首选端口不可用就直接失败退出（exit 1）。

.PARAMETER Dev
    额外**另开一个窗口**跑 `npm run dev`（Vite 热更新，5173）。
    改前端代码时用它；改完记得 `npm run build` 并把 dist/ 一起提交。

.PARAMETER SkipDeps
    跳过依赖容器检查（比如你确定不用 MySQL / 搜索时）。

.EXAMPLE
    .\scripts\run\start-app.ps1
    .\scripts\run\start-app.ps1 -Port 8001
    .\scripts\run\start-app.ps1 -Dev
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [switch]$Dev,
    [switch]$SkipDeps,
    [switch]$NoFallback
)

# ⚠️ 刻意不用 $ErrorActionPreference='Stop'：uvicorn 的日志走 **stderr**，
#    而在 PowerShell 里对原生命令开 Stop 容易把正常的日志当异常中断脚本。
# ⚠️ 本脚本在 scripts/run/ 下 → 仓库根要**往上两层**（少一层会变成 scripts\ ，
#    于是 `uv run` 找不到 pyproject.toml）。
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $Root

$script:StepNo = 0
function Write-Step($Text) {
    $script:StepNo++
    Write-Host ("[{0}/4] {1}" -f $script:StepNo, $Text) -ForegroundColor Cyan
}
function Write-Ok($Text) { Write-Host "      $Text" -ForegroundColor Green }
function Write-Tip($Text) { Write-Host "      $Text" -ForegroundColor Yellow }

Write-Host ""
Write-Host "===================================================" -ForegroundColor DarkGray
Write-Host "  Code Agent-novi  ·  Web 界面启动" -ForegroundColor White
Write-Host "  项目根目录: $Root" -ForegroundColor DarkGray
Write-Host "===================================================" -ForegroundColor DarkGray

# ── 1) uv ────────────────────────────────────────────────
Write-Step "检查 uv（本项目的包管理器，约定上不用 pip）"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "      ✗ 找不到 uv。安装： https://docs.astral.sh/uv/" -ForegroundColor Red
    Write-Host "        （或 winget install astral-sh.uv，或 uv sync 一下）" -ForegroundColor Red
    exit 1
}
Write-Ok "uv 就绪"

# ── 2) 依赖容器 ──────────────────────────────────────────
if ($SkipDeps) {
    Write-Step "跳过依赖容器检查（-SkipDeps）"
}
else {
    Write-Step "检查依赖容器（agent-mysql / redis-stack-server / searxng / my-nginx）"
    $needed = @('agent-mysql', 'redis-stack-server', 'searxng', 'my-nginx')
    $running = @()
    try { $running = @(docker ps --format '{{.Names}}' 2>$null) } catch { $running = @() }

    if ($running.Count -eq 0) {
        Write-Tip "Docker 没在跑（Docker Desktop 没开？）→ MySQL / Redis / 搜索会不可用"
        Write-Tip "先开 Docker Desktop；容器没起就跑： .\scripts\run\start-deps.ps1"
    }
    else {
        $missing = @($needed | Where-Object { $running -notcontains $_ })
        if ($missing.Count -gt 0) {
            Write-Tip ("这些容器没起：" + ($missing -join '、'))
            Write-Tip "拉起来： .\scripts\run\start-deps.ps1"
        }
        else {
            Write-Ok "4 个容器都在跑"
        }
    }
}

# ── 3) 前端产物 ──────────────────────────────────────────
Write-Step "检查前端构建产物 app\web\frontend\dist"
$distIndex = Join-Path $Root 'app\web\frontend\dist\index.html'
if (Test-Path $distIndex) {
    Write-Ok "dist/index.html 已就绪（clone 下来就能开界面，不需要 Node）"
}
else {
    Write-Tip "缺 dist\index.html —— 界面会打不开（接口仍可用）"
    Write-Tip "构建一次： cd app\web\frontend ; npm install ; npm run build"
}

# ── 4) 端口：**实测能否绑定**，不行自动退避 ──────────────
# ⚠️ 为什么不查 `Get-NetTCPConnection -State Listen`（旧实现的做法）：
#    那只查得到"有没有人在监听"，**查不出"端口被 Windows 保留"** ——
#    2026-10-09 实测踩过：脚本打印"端口 8000 空闲"，随后 uvicorn 绑定报
#    `[Errno 13] … [winerror 10013]`（Hyper-V/WSL 的 excludedportrange 把 7927–8126 划走了）。
#    ⇒ 唯一可靠的判据是**真 bind 一次**：成功=可用；失败看错误码——
#      10048 = 有进程占着（可查 PID）；10013 = 被系统保留（换端口或清保留，**别去杀进程**）。
function Test-PortBindable {
    param([int]$P)
    $listener = $null
    try {
        $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $P)
        $listener.Start()
        return [pscustomobject]@{ Ok = $true; Code = 0 }
    }
    catch {
        $code = 0
        if ($_.Exception -is [System.Net.Sockets.SocketException]) { $code = $_.Exception.NativeErrorCode }
        elseif ($_.Exception.InnerException -is [System.Net.Sockets.SocketException]) { $code = $_.Exception.InnerException.NativeErrorCode }
        return [pscustomobject]@{ Ok = $false; Code = $code }
    }
    finally {
        if ($listener) { try { $listener.Stop() } catch { } }
    }
}

function Format-PortReason {
    param([int]$P, [int]$Code)
    if ($Code -eq 10048) {
        $busy = Get-NetTCPConnection -LocalPort $P -State Listen -ErrorAction SilentlyContinue
        $ownerPid = ($busy | Select-Object -First 1).OwningProcess
        $procName = (Get-Process -Id $ownerPid -ErrorAction SilentlyContinue).ProcessName
        if ($ownerPid) { return "被占用（PID $ownerPid / $procName）" }
        return '被占用（10048）'
    }
    if ($Code -eq 10013) {
        return '被系统保留（Hyper-V/WSL 抢走的端口段；netsh interface ipv4 show excludedportrange protocol=tcp 可查）'
    }
    if ($Code -eq 0) { return '可用' }
    return "绑定失败（错误码 $Code）"
}

Write-Step "检查端口 $Port（实测能否绑定；不行自动换）"
# 候选顺序：先试你指定的那个，再试一组常见安全端口（都会**实测**，被保留的自动跳过）
$candidates = @($Port) + @(8010, 8020, 8300, 8310, 9000, 9010, 9200) | Select-Object -Unique
$chosen = 0
$firstReason = ''
foreach ($cand in $candidates) {
    $probe = Test-PortBindable -P $cand
    if ($probe.Ok) {
        $chosen = $cand
        if ($cand -eq $Port) {
            Write-Ok "端口 $Port 可用（已实测绑定）"
        }
        elseif ($NoFallback) {
            # -NoFallback：首选端口不可用就直接失败（**不**打印"已自动改用"，否则自相矛盾）
            Write-Host "      ✗ 端口 $Port $firstReason（-NoFallback 已开启，不自动换端口）" -ForegroundColor Red
            exit 1
        }
        else {
            Write-Tip "✗ 端口 $Port $firstReason"
            Write-Ok "→ 已自动改用端口 $chosen（同样经过实测）"
            Write-Tip "想固定端口：关掉占用者，或用管理员把端口钉成保留 ——"
            Write-Tip "  net stop winnat ; netsh int ipv4 add excludedportrange protocol=tcp startport=$Port numberofports=1 store=persistent ; net start winnat"
        }
        break
    }
    if ($cand -eq $Port) { $firstReason = Format-PortReason -P $cand -Code $probe.Code }
}

if ($chosen -eq 0) {
    Write-Host "      ✗ 候选端口全部不可用（$($candidates -join ', ')）" -ForegroundColor Red
    Write-Host "        首选端口 $Port $firstReason" -ForegroundColor Red
    exit 1
}
$Port = $chosen

# ── 可选：前端热更新（另开窗口）──────────────────────────
if ($Dev) {
    Write-Host "[dev] 另开一个窗口跑 npm run dev（前端热更新）" -ForegroundColor Cyan
    Start-Process -FilePath 'powershell' -ArgumentList @(
        '-NoExit', '-NoProfile', '-Command',
        "Set-Location '$Root\app\web\frontend'; npm run dev"
    )
    Write-Tip "开发时请打开 http://127.0.0.1:5173 （Vite 会把 /api 与 /ws 转发到后端）"
}

# ── 前台启动后端 ─────────────────────────────────────────
# ⚠️ 这里**故意先不打 URL**（阶段 7 · 界面第二轮反馈）：
#    启动要加载 32 个工具 + 知识库，通常 10~30 秒；以前脚本一上来就把地址打出来，
#    用户点进去看到的是"网页打不开"，很容易以为项目坏了。
#    现在改成：先提示"正在启动"，把地址交给**后端自己**在启动完成时打印
#    （`lifespan` 末尾会读下面这个环境变量，输出「✅ 已就绪 —— 在浏览器打开：…」）。
Write-Host ""
Write-Host "  正在启动，请稍候……" -ForegroundColor Yellow
Write-Host "     要加载 32 个工具 + 知识库，通常 10~30 秒（首次更久）。" -ForegroundColor DarkGray
Write-Host "     等下面出现绿色方框里的「[OK] 已就绪」再打开浏览器 —— 提前点会打不开，那不是项目坏了。" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  停止:  在这个窗口按 Ctrl+C" -ForegroundColor DarkGray
Write-Host ""
$env:CODE_AGENT_WEB_URL = "http://127.0.0.1:$Port/"
# `--log-level warning`：uvicorn 自己那 4 行 INFO（Started server process / Waiting for
# application startup / Application startup complete / Uvicorn running on …）纯属噪音 ——
# 地址由后端在启动完成时用**绿色方框**打出来（见 server.py 的 `_log_ready_banner`）。
# ⚠️ 只影响 uvicorn 自己的 logger，不影响本项目 `code_agent.*` 的日志（那是我们自己的配置）。
& uv run uvicorn app.web.server:app --port $Port --log-level warning

# ⚠️ **必须把退出码传出去**（2026-10-09 修）：`& uv run uvicorn …` 失败**不会**自动成为
#    powershell.exe 的退出码 ⇒ 上层 `start-app.cmd` 的 `if errorlevel 1 pause` 不触发
#    ⇒ 双击启动时"闪退"，用户看不到任何错误（现场：端口被系统保留，绑定报 10013）。
exit $LASTEXITCODE
