# scripts/start-deps.ps1 —— 一键拉起全部依赖服务（4 个容器）
#
# 两边分开管的原因见 docker-compose.yml 顶部注释：
#   mysql / searxng / redis 由仓库根的 compose 管（Windows 侧）
#   nginx 由 WSL 里的 ~/nginx/docker-compose.yaml 管（挂载源是 WSL 路径）
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

Write-Host "[1/2] 启动 mysql / searxng / redis ..." -ForegroundColor Cyan
Push-Location $root
docker compose up -d
Pop-Location

Write-Host "[2/2] 启动 nginx（走 WSL）..." -ForegroundColor Cyan
wsl -d Ubuntu -- bash -lc "cd ~/nginx && docker compose up -d"

Write-Host "`n完成。当前容器：" -ForegroundColor Green
docker ps --format "table {{.Names}}`t{{.Status}}`t{{.Ports}}"
