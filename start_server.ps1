<#
.SYNOPSIS
  Start / restart all server containers (API + Postgres + Milvus + MinIO).

.EXAMPLE
  .\start_server.ps1            # Start or restart
  .\start_server.ps1 -Clean     # Wipe volumes and start fresh
  .\start_server.ps1 -Logs      # Start then tail logs
#>
param(
    [switch]$Clean,
    [switch]$Logs
)

Set-Location $PSScriptRoot

Write-Host "========================================"  -ForegroundColor Cyan
Write-Host "  ResourceUpload Server Startup"          -ForegroundColor Cyan
Write-Host "========================================"  -ForegroundColor Cyan
Write-Host ""

if ($Clean) {
    Write-Host "[1/3] Removing old containers and volumes ..." -ForegroundColor Yellow
    & docker compose down -v 2>&1 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
} else {
    Write-Host "[1/3] Stopping old containers ..." -ForegroundColor Yellow
    & docker compose down 2>&1 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
}

Write-Host "[2/3] Building and starting containers ..." -ForegroundColor Yellow
& docker compose up -d --build 2>&1 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }

Write-Host "[3/3] Waiting for services to be ready ..." -ForegroundColor Yellow

$maxWait = 120
$elapsed = 0
$ready = $false

while ($elapsed -lt $maxWait) {
    Start-Sleep -Seconds 3
    $elapsed += 3

    try {
        $resp = Invoke-RestMethod -Uri "http://localhost:8000/health" -Method Get -TimeoutSec 5
        if ($resp.status -eq "ok") {
            $ready = $true
            break
        }
        $pg = $resp.postgres.status
        $mv = $resp.milvus.status
        $s3 = $resp.s3.status
        Write-Host "  Status: $($resp.status) | pg=$pg mv=$mv s3=$s3 | ${elapsed}s" -ForegroundColor Gray
    } catch {
        Write-Host "  Waiting for API ... ${elapsed}s" -ForegroundColor Gray
    }
}

Write-Host ""
if ($ready) {
    Write-Host "=== Server Ready ===" -ForegroundColor Green
    Write-Host "  API:     http://localhost:8000"                        -ForegroundColor White
    Write-Host "  Docs:    http://localhost:8000/docs"                   -ForegroundColor White
    Write-Host "  Health:  http://localhost:8000/health"                 -ForegroundColor White
    Write-Host "  MinIO:   http://localhost:9001 (minioadmin/minioadmin)" -ForegroundColor White
    Write-Host ""
} else {
    Write-Host "=== Timeout: services may not be fully ready ===" -ForegroundColor Red
    Write-Host "Run 'docker compose logs api' to check logs"     -ForegroundColor Yellow
    exit 1
}

if ($Logs) {
    Write-Host "Following logs (Ctrl+C to stop) ..." -ForegroundColor Cyan
    & docker compose logs -f api
}
