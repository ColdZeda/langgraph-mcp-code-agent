# scripts/stop-deps.ps1 —— 停止全部依赖服务（保留容器，下次 up 更快）
$root = Split-Path -Parent $PSScriptRoot

Write-Host "[1/2] 停止 nginx ..." -ForegroundColor Cyan
wsl -d Ubuntu -- bash -lc "cd ~/nginx && docker compose stop"

Write-Host "[2/2] 停止 mysql / searxng / redis ..." -ForegroundColor Cyan
Push-Location $root
docker compose stop
Pop-Location

Write-Host "`n完成。当前容器：" -ForegroundColor Green
docker ps -a --format "table {{.Names}}`t{{.Status}}"
