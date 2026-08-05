<#
.SYNOPSIS
  Start local API and enabled workers
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

# Some Windows hosts expose both Path and PATH in the process environment.
# Start-Process rejects that duplicate when constructing a child environment.
$processEnvironment = [Environment]::GetEnvironmentVariables("Process")
$processPathKeys = @(
    $processEnvironment.Keys |
        Where-Object { [string]$_ -ieq "path" }
)
if ($processPathKeys.Count -gt 1) {
    $canonicalPath = $env:Path
    [Environment]::SetEnvironmentVariable("PATH", $null, "Process")
    [Environment]::SetEnvironmentVariable("Path", $canonicalPath, "Process")
}

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
            & "$env:SystemRoot\System32\taskkill.exe" /PID $procId /T /F | Out-Null
            if ($LASTEXITCODE -ne 0 -and -not $p.HasExited) {
                $p.Kill()
            }
            $p.WaitForExit(5000)
            Write-OK "$name stopped"
        }
    } catch { }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

# ── Pre-flight ────────────────────────────────────────────
Write-Step "1/7 Pre-flight checks"

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
$artifactEnabledText = if ($env:KNOWLEDGE_ARTIFACT_RUNTIME_ENABLED) {
    $env:KNOWLEDGE_ARTIFACT_RUNTIME_ENABLED
} else {
    $envHash['KNOWLEDGE_ARTIFACT_RUNTIME_ENABLED']
}
$knowledgeArtifactEnabled = $artifactEnabledText -match '(?i)^(true|1|yes|on)$'
Write-OK "KNOWLEDGE_ARTIFACT_RUNTIME_ENABLED = $knowledgeArtifactEnabled"
$classificationEnabledText = if ($env:CLASSIFICATION_RUNTIME_ENABLED) {
    $env:CLASSIFICATION_RUNTIME_ENABLED
} else {
    $envHash['CLASSIFICATION_RUNTIME_ENABLED']
}
$classificationEnabled = $classificationEnabledText -match '(?i)^(true|1|yes|on)$'
Write-OK "CLASSIFICATION_RUNTIME_ENABLED = $classificationEnabled"


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
    Stop-ProcessByPidFile (Join-Path $pidDir "importer.pid") "Import Worker"
    Stop-ProcessByPidFile (Join-Path $pidDir "embedder.pid") "Embedder Worker"
    Stop-ProcessByPidFile (Join-Path $pidDir "graph_extractor.pid") "Graph Extractor"
    Stop-ProcessByPidFile (Join-Path $pidDir "knowledge_artifacts.pid") "Knowledge Artifact Worker"
    Stop-ProcessByPidFile (Join-Path $pidDir "classifications.pid") "Classification Worker"
    Stop-ProcessByPidFile (Join-Path $pidDir "cleanup.pid") "Cleanup Worker"
}

# ── Create log dir ────────────────────────────────────────
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# ── Start API ─────────────────────────────────────────────
Write-Step "2/7 Starting API (python -m app.main)"

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
Write-Step "3/7 Starting Import Worker (python -m app.workers.importer --watch)"

$importPidFile = Join-Path $pidDir "importer.pid"
if (Test-ProcessAlive $importPidFile) {
    Write-Warn "Import Worker already running (PID $(Get-Content $importPidFile)), skipping"
} else {
    $proc = Start-Process -FilePath $venvPython `
        -ArgumentList "-m", "app.workers.importer", "--watch" `
        -WorkingDirectory $projectDir `
        -PassThru -NoNewWindow `
        -RedirectStandardOutput (Join-Path $logDir "importer_stdout.log") `
        -RedirectStandardError (Join-Path $logDir "importer_stderr.log")
    $proc.Id | Out-File -FilePath $importPidFile -Encoding utf8 -NoNewline
    Write-OK "Import Worker started (PID $($proc.Id))"
}

Write-Step "4/7 Starting Embedder Worker (python -m app.workers.embedder --watch)"

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
Write-Step "5/7 Starting Cleanup Worker (python -m app.workers.cleanup --watch)"

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
Write-Step "6/7 Starting Graph Extraction Worker"

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

# Start Knowledge Artifact Worker
Write-Step "7/7 Starting Knowledge Artifact Worker"

$artifactPidFile = Join-Path $pidDir "knowledge_artifacts.pid"
if (-not $knowledgeArtifactEnabled) {
    Write-OK "Knowledge Artifact Worker disabled; not started"
} elseif (Test-ProcessAlive $artifactPidFile) {
    Write-Warn "Knowledge Artifact Worker already running (PID $(Get-Content $artifactPidFile)), skipping"
} else {
    $proc = Start-Process -FilePath $venvPython `
        -ArgumentList "-m", "app.workers.knowledge_artifacts", "--watch" `
        -WorkingDirectory $projectDir `
        -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logDir "knowledge_artifacts_stdout.log") `
        -RedirectStandardError (Join-Path $logDir "knowledge_artifacts_stderr.log")
    $proc.Id | Out-File -FilePath $artifactPidFile -Encoding utf8 -NoNewline
    Write-OK "Knowledge Artifact Worker started (PID $($proc.Id))"
}

# ── Summary ───────────────────────────────────────────────
Write-Host ""
# Start Classification Worker
Write-Step "Starting Classification Worker"

$classificationPidFile = Join-Path $pidDir "classifications.pid"
if (-not $classificationEnabled) {
    Write-OK "Classification Worker disabled; not started"
} elseif (Test-ProcessAlive $classificationPidFile) {
    Write-Warn "Classification Worker already running (PID $(Get-Content $classificationPidFile)), skipping"
} else {
    $proc = Start-Process -FilePath $venvPython `
        -ArgumentList "-m", "app.workers.classifications", "--watch" `
        -WorkingDirectory $projectDir `
        -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logDir "classifications_stdout.log") `
        -RedirectStandardError (Join-Path $logDir "classifications_stderr.log")
    $proc.Id | Out-File -FilePath $classificationPidFile -Encoding utf8 -NoNewline
    Write-OK "Classification Worker started (PID $($proc.Id))"
}

Write-Host "=== Startup complete ===" -ForegroundColor Green
Write-Host "  API:       http://127.0.0.1:${apiPort}/console/"
Write-Host "  Health:    http://127.0.0.1:${apiPort}/health"
Write-Host "  Logs:      $logDir"
Write-Host "  Stop:      scripts\stop_local.ps1"
Write-Host "  Status:    scripts\status_local.ps1"
