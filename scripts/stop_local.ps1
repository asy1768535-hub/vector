<#
.SYNOPSIS
  Stop project processes started by start_local.ps1
.DESCRIPTION
  Only stops processes recorded in PID files. Does not stop unrelated services.
  Order: Knowledge Artifacts -> Graph Extractor -> Cleanup -> Importer -> Embedder -> API
#>
$ErrorActionPreference = "Continue"
$projectDir = $PSScriptRoot | Split-Path -Parent
$pidDir = Join-Path $projectDir ".run_logs"
$venvPython = Join-Path $projectDir ".venv\Scripts\python.exe"

function Test-ProjectProcess($procId, $module) {
    try {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $procId" -ErrorAction Stop
        $command = [string]$process.CommandLine
        return ($command -like "*$venvPython*" -and $command -match "(?i)(^|\s)-m\s+$([regex]::Escape($module))(\s|$)")
    } catch { return $false }
}

function Stop-ByPidFile($pidFile, $label, $module) {
    if (-not (Test-Path $pidFile)) {
        Write-Host "  $label : no PID file (not running)" -ForegroundColor DarkGray
        return
    }
    $procId = (Get-Content $pidFile -Raw).Trim()
    if (-not (Test-ProjectProcess $procId $module)) {
        Write-Host "  $label : stale PID file ignored (PID $procId identity mismatch)" -ForegroundColor Yellow
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
        return
    }
    try {
        $proc = Get-Process -Id $procId -ErrorAction Stop
        if (-not $proc.HasExited) {
            Write-Host "  Stopping $label (PID $procId)..." -ForegroundColor Yellow
            & "$env:SystemRoot\System32\taskkill.exe" /PID $procId /T /F | Out-Null
            if ($LASTEXITCODE -ne 0 -and -not $proc.HasExited) {
                $proc.Kill()
            }
            $proc.WaitForExit(5000)
            Write-Host "  $label : stopped" -ForegroundColor Green
        } else {
            Write-Host "  $label : already exited" -ForegroundColor DarkGray
        }
    } catch {
        Write-Host "  $label : process not found" -ForegroundColor DarkGray
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

Write-Host "=== Stop local project processes ===" -ForegroundColor Cyan

# Stop workers first, then API
Stop-ByPidFile (Join-Path $pidDir "classifications.pid") "Classification Worker" "app.workers.classifications"
Stop-ByPidFile (Join-Path $pidDir "knowledge_artifacts.pid") "Knowledge Artifact Worker" "app.workers.knowledge_artifacts"
Stop-ByPidFile (Join-Path $pidDir "graph_extractor.pid") "Graph Extractor" "app.workers.graph_extractor"
Stop-ByPidFile (Join-Path $pidDir "cleanup.pid") "Cleanup Worker" "app.workers.cleanup"
Stop-ByPidFile (Join-Path $pidDir "importer.pid") "Import Worker" "app.workers.importer"
Stop-ByPidFile (Join-Path $pidDir "embedder.pid") "Embedder Worker" "app.workers.embedder"
Stop-ByPidFile (Join-Path $pidDir "api.pid") "API" "app.main"

Write-Host "Done." -ForegroundColor Green
