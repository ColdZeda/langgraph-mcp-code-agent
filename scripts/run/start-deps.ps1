# scripts/run/start-deps.ps1 —— 一键拉起全部依赖服务（4 个容器）
#
# 两边分开管的原因见 docker-compose.yml 顶部注释：
#   mysql / searxng / redis 由仓库根的 compose 管（Windows 侧）
#   nginx 由 WSL 里的 ~/nginx/docker-compose.yaml 管（挂载源是 WSL 路径）
$ErrorActionPreference = "Stop"
# ⚠️ 本脚本在 scripts/run/ 下 → 仓库根要**往上两层**（少一层会变成 scripts\ ，
#    于是 `docker compose up` 找不到仓库根的 docker-compose.yml）。
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

Write-Host "[1/2] 启动 mysql / searxng / redis ..." -ForegroundColor Cyan
Push-Location $root
docker compose up -d
Pop-Location

Write-Host "[2/2] 启动 nginx（走 WSL）..." -ForegroundColor Cyan
# ⚠️ ~/nginx/ 只存在于 WSL，仓库里没有副本（干净 clone 下来必然没有）→
#    先探一次，缺了就打印明确提示并跳过，而不是甩一段难懂的 WSL 报错。
#    细节见 AGENTS.md 的「环境与工具链」——那里还记着「WSL compose 别用单文件挂载」的坑。
#    注意：探测失败时 wsl 会把错误信息写进 **stdout**（不是 stderr），所以只认「整串等于 yes」，
#    其余一律当"没有"处理（Out-String + Trim 兜住换行与多余空白）。
$nginxExists = (
    wsl -d Ubuntu -- bash -lc 'test -d ~/nginx && echo yes || echo no' 2>$null | Out-String
).Trim()
if ($nginxExists -eq "yes") {
    wsl -d Ubuntu -- bash -lc "cd ~/nginx && docker compose up -d"
} else {
    Write-Host "  [跳过] WSL 里没有 ~/nginx 目录。" -ForegroundColor Yellow
    Write-Host "         它只存在于 WSL，仓库里没有副本；需要 nginx 时请先在 WSL 里准备好 compose + conf。" -ForegroundColor Yellow
}

Write-Host "`n完成。当前容器：" -ForegroundColor Green
docker ps --format "table {{.Names}}`t{{.Status}}`t{{.Ports}}"
