<#
.SYNOPSIS
  Show local three-process status, port 8100, and /health
.DESCRIPTION
  Checks PID files for process liveness, port occupancy, and calls /health.
  Does not print passwords, API keys, or JWT_SECRET.
#>
$ErrorActionPreference = "Continue"
$projectDir = $PSScriptRoot | Split-Path -Parent
$pidDir = Join-Path $projectDir ".run_logs"

# Read API_PORT from .env
$apiPort = 8100
$envPath = Join-Path $projectDir ".env"
if (Test-Path $envPath) {
    $match = Select-String -Path $envPath -Pattern '^\s*API_PORT\s*=\s*(\d+)' | Select-Object -First 1
    if ($match -and $match.Matches.Groups[1].Value) {
        $apiPort = [int]$match.Matches.Groups[1].Value
    }
}

function Test-PidAlive($pidFile, $label) {
    if (-not (Test-Path $pidFile)) {
        Write-Host "  $label : DOWN (no PID file)" -ForegroundColor Red
        return $false
    }
    $procId = (Get-Content $pidFile -Raw).Trim()
    try {
        $p = Get-Process -Id $procId -ErrorAction Stop
        if (-not $p.HasExited) {
            Write-Host "  $label : UP (PID $procId)" -ForegroundColor Green
            return $true
        } else {
            Write-Host "  $label : DOWN (PID $procId exited)" -ForegroundColor Red
            return $false
        }
    } catch {
        Write-Host "  $label : DOWN (PID $procId not found)" -ForegroundColor Red
        return $false
    }
}

Write-Host "=== Local process status ===" -ForegroundColor Cyan
Write-Host "Time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host ""

Write-Host "--- Project processes ---" -ForegroundColor White
$apiAlive = Test-PidAlive (Join-Path $pidDir "api.pid") "API"
$embedAlive = Test-PidAlive (Join-Path $pidDir "embedder.pid") "Embedder Worker"
$cleanAlive = Test-PidAlive (Join-Path $pidDir "cleanup.pid") "Cleanup Worker"

Write-Host ""

Write-Host "--- Ports ---" -ForegroundColor White
$portCheck = netstat -ano | Select-String "LISTENING" | Select-String ":$apiPort\s"
if ($portCheck) {
    Write-Host "  $apiPort : LISTENING" -ForegroundColor Green
} else {
    Write-Host "  $apiPort : NOT LISTENING" -ForegroundColor Red
}

$staticCheck = netstat -ano | Select-String "LISTENING" | Select-String ":5599\s"
if ($staticCheck) {
    Write-Host "  5599 : LISTENING (static preview)" -ForegroundColor DarkGray
}

Write-Host ""

Write-Host "--- /health ---" -ForegroundColor White
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$apiPort/health" -TimeoutSec 5 -UseBasicParsing
    $data = $r.Content | ConvertFrom-Json
    $color = if ($data.status -eq 'ok') { 'Green' } else { 'Red' }
    Write-Host "  Status: $($data.status)" -ForegroundColor $color
    Write-Host "  DB:     $($data.db)"
    Write-Host "  Qdrant: $($data.qdrant)"
    Write-Host "  Embed:  $($data.embedding) ($($data.embedding_model) dim=$($data.embedding_dim))"
    Write-Host "  Rerank: $($data.rerank)"
    Write-Host "  OCR:    $($data.ocr)"
} catch {
    Write-Host "  /health unreachable: $_" -ForegroundColor Red
}

Write-Host ""
Write-Host "--- Quick actions ---" -ForegroundColor White
Write-Host "  Start:  .\scripts\start_local.ps1"
Write-Host "  Stop:   .\scripts\stop_local.ps1"
Write-Host "  Logs:   $pidDir"
Write-Host "  Console: http://127.0.0.1:${apiPort}/console/"
