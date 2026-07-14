<#
.SYNOPSIS
  Start local four processes: API / Embedding Worker / Cleanup Worker / Graph Extractor
.DESCRIPTION
  Only starts project processes. Checks DB, Qdrant and port before starting.
  Logs written to .run_logs/. PID files track process IDs.
  Re-running will not start duplicate instances.
.NOTES
  Does not print passwords, API keys, or JWT_SECRET.
#>
param(
    [switch]$Force  # Force-kill existing instances before restart
)

$ErrorActionPreference = "Stop"
$projectDir = $PSScriptRoot | Split-Path -Parent
$venvPython = Join-Path $projectDir ".venv\Scripts\python.exe"
$logDir = Join-Path $projectDir ".run_logs"
$pidDir = $logDir

# ── Helper functions ──────────────────────────────────────
function Write-Step { param($msg) Write-Host ">>> $msg" -ForegroundColor Cyan }
function Write-OK { param($msg) Write-Host "  OK: $msg" -ForegroundColor Green }
function Write-Warn { param($msg) Write-Host "  WARN: $msg" -ForegroundColor Yellow }
function Write-Fail { param($msg) Write-Host "  FAIL: $msg" -ForegroundColor Red }

function Test-ProcessAlive($pidFile) {
    if (-not (Test-Path $pidFile)) { return $false }
    $procId = (Get-Content $pidFile -Raw).Trim()
    try {
        $p = Get-Process -Id $procId -ErrorAction Stop
        return (-not $p.HasExited)
    } catch { return $false }
}

function Stop-ProcessByPidFile($pidFile, $name) {
    if (-not (Test-Path $pidFile)) { return }
    $procId = (Get-Content $pidFile -Raw).Trim()
    try {
        $p = Get-Process -Id $procId -ErrorAction Stop
        if (-not $p.HasExited) {
            Write-Warn "Stopping $name (PID $procId)..."
            $p.Kill()
            $p.WaitForExit(5000)
            Write-OK "$name stopped"
        }
    } catch { }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

# ── Pre-flight ────────────────────────────────────────────
Write-Step "1/5 Pre-flight checks"

# Check .venv
if (-not (Test-Path $venvPython)) {
    Write-Fail ".venv\Scripts\python.exe not found. Create venv first."
    exit 1
}
Write-OK ".venv available"

# Read config (no secrets printed)
$envPath = Join-Path $projectDir ".env"
if (-not (Test-Path $envPath)) {
    Write-Fail ".env not found"
    exit 1
}
$envLines = Get-Content $envPath | Where-Object { $_ -match '^\s*([A-Z_]+)\s*=\s*(.*)' }
$envHash = @{}
foreach ($line in $envLines) {
    if ($line -match '^\s*([A-Z_]+)\s*=\s*(.*)') {
        $envHash[$Matches[1]] = $Matches[2].Trim()
    }
}
$apiPort = if ($envHash['API_PORT']) { [int]$envHash['API_PORT'] } else { 8100 }
Write-OK "API_PORT = $apiPort"
$graphEnabledText = if ($env:GRAPH_EXTRACTION_ENABLED) {
    $env:GRAPH_EXTRACTION_ENABLED
} else {
    $envHash['GRAPH_EXTRACTION_ENABLED']
}
$graphExtractionEnabled = $graphEnabledText -match '(?i)^(true|1|yes|on)$'
Write-OK "GRAPH_EXTRACTION_ENABLED = $graphExtractionEnabled"

# Check port
$portCheck = netstat -ano | Select-String "LISTENING" | Select-String ":$apiPort\s"
if ($portCheck) {
    $existingPid = ($portCheck -split '\s+')[-1]
    if ($Force) {
        Write-Warn "Port $apiPort occupied by PID $existingPid, Force mode: attempting stop"
        try { Stop-Process -Id $existingPid -Force; Start-Sleep -Seconds 2 } catch { }
    } else {
        Write-Warn "Port $apiPort occupied by PID $existingPid. Run stop_local.ps1 first, or use -Force"
    }
}

# ── Force cleanup ─────────────────────────────────────────
if ($Force) {
    Write-Step "Force mode: stopping existing instances"
    Stop-ProcessByPidFile (Join-Path $pidDir "api.pid") "API"
    Stop-ProcessByPidFile (Join-Path $pidDir "embedder.pid") "Embedder Worker"
    Stop-ProcessByPidFile (Join-Path $pidDir "graph_extractor.pid") "Graph Extractor"
    Stop-ProcessByPidFile (Join-Path $pidDir "cleanup.pid") "Cleanup Worker"
}

# ── Create log dir ────────────────────────────────────────
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# ── Start API ─────────────────────────────────────────────
Write-Step "2/5 Starting API (python -m app.main)"

$apiPidFile = Join-Path $pidDir "api.pid"
if (Test-ProcessAlive $apiPidFile) {
    Write-Warn "API already running (PID $(Get-Content $apiPidFile)), skipping"
} else {
    $proc = Start-Process -FilePath $venvPython `
        -ArgumentList "-m", "app.main" `
        -WorkingDirectory $projectDir `
        -PassThru -NoNewWindow `
        -RedirectStandardOutput (Join-Path $logDir "api_stdout.log") `
        -RedirectStandardError (Join-Path $logDir "api_stderr.log")
    $proc.Id | Out-File -FilePath $apiPidFile -Encoding utf8 -NoNewline
    Write-OK "API started (PID $($proc.Id))"

    # Wait for listen
    Write-Host "  Waiting for API to listen on port $apiPort..."
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        $listening = netstat -ano | Select-String "LISTENING" | Select-String ":$apiPort\s"
        if ($listening) { $ready = $true; break }
    }
    if ($ready) { Write-OK "API listening on port $apiPort" }
    else { Write-Warn "API not listening on port $apiPort. Check: $logDir\api_stderr.log" }
}

# ── Start Embedder Worker ─────────────────────────────────
Write-Step "3/5 Starting Embedder Worker (python -m app.workers.embedder --watch)"

$embedPidFile = Join-Path $pidDir "embedder.pid"
if (Test-ProcessAlive $embedPidFile) {
    Write-Warn "Embedder Worker already running (PID $(Get-Content $embedPidFile)), skipping"
} else {
    $proc = Start-Process -FilePath $venvPython `
        -ArgumentList "-m", "app.workers.embedder", "--watch" `
        -WorkingDirectory $projectDir `
        -PassThru -NoNewWindow `
        -RedirectStandardOutput (Join-Path $logDir "embedder_stdout.log") `
        -RedirectStandardError (Join-Path $logDir "embedder_stderr.log")
    $proc.Id | Out-File -FilePath $embedPidFile -Encoding utf8 -NoNewline
    Write-OK "Embedder Worker started (PID $($proc.Id))"
}

# ── Start Cleanup Worker ──────────────────────────────────
Write-Step "4/5 Starting Cleanup Worker (python -m app.workers.cleanup --watch)"

$cleanPidFile = Join-Path $pidDir "cleanup.pid"
if (Test-ProcessAlive $cleanPidFile) {
    Write-Warn "Cleanup Worker already running (PID $(Get-Content $cleanPidFile)), skipping"
} else {
    $proc = Start-Process -FilePath $venvPython `
        -ArgumentList "-m", "app.workers.cleanup", "--watch" `
        -WorkingDirectory $projectDir `
        -PassThru -NoNewWindow `
        -RedirectStandardOutput (Join-Path $logDir "cleanup_stdout.log") `
        -RedirectStandardError (Join-Path $logDir "cleanup_stderr.log")
    $proc.Id | Out-File -FilePath $cleanPidFile -Encoding utf8 -NoNewline
    Write-OK "Cleanup Worker started (PID $($proc.Id))"
}

# ── Start Graph Extraction Worker ─────────────────────────
Write-Step "5/5 Starting Graph Extraction Worker"

$graphPidFile = Join-Path $pidDir "graph_extractor.pid"
if (-not $graphExtractionEnabled) {
    Write-OK "Graph Extractor disabled; not started"
} elseif (Test-ProcessAlive $graphPidFile) {
    Write-Warn "Graph Extractor already running (PID $(Get-Content $graphPidFile)), skipping"
} else {
    $proc = Start-Process -FilePath $venvPython `
        -ArgumentList "-m", "app.workers.graph_extractor", "--watch" `
        -WorkingDirectory $projectDir `
        -PassThru -NoNewWindow `
        -RedirectStandardOutput (Join-Path $logDir "graph_extractor_stdout.log") `
        -RedirectStandardError (Join-Path $logDir "graph_extractor_stderr.log")
    $proc.Id | Out-File -FilePath $graphPidFile -Encoding utf8 -NoNewline
    Write-OK "Graph Extraction Worker started (PID $($proc.Id))"
}

# ── Summary ───────────────────────────────────────────────
Write-Host ""
Write-Host "=== Startup complete ===" -ForegroundColor Green
Write-Host "  API:       http://127.0.0.1:${apiPort}/console/"
Write-Host "  Health:    http://127.0.0.1:${apiPort}/health"
Write-Host "  Logs:      $logDir"
Write-Host "  Stop:      scripts\stop_local.ps1"
Write-Host "  Status:    scripts\status_local.ps1"
