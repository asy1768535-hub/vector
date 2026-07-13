<#
.SYNOPSIS
  Stop the four project processes started by start_local.ps1
.DESCRIPTION
  Only stops processes recorded in PID files. Does not stop unrelated services.
  Order: Graph Extractor -> Cleanup -> Embedder -> API
#>
$ErrorActionPreference = "Continue"
$projectDir = $PSScriptRoot | Split-Path -Parent
$pidDir = Join-Path $projectDir ".run_logs"

function Stop-ByPidFile($pidFile, $label) {
    if (-not (Test-Path $pidFile)) {
        Write-Host "  $label : no PID file (not running)" -ForegroundColor DarkGray
        return
    }
    $procId = (Get-Content $pidFile -Raw).Trim()
    try {
        $proc = Get-Process -Id $procId -ErrorAction Stop
        if (-not $proc.HasExited) {
            Write-Host "  Stopping $label (PID $procId)..." -ForegroundColor Yellow
            $proc.Kill()
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
Stop-ByPidFile (Join-Path $pidDir "graph_extractor.pid") "Graph Extractor"
Stop-ByPidFile (Join-Path $pidDir "cleanup.pid") "Cleanup Worker"
Stop-ByPidFile (Join-Path $pidDir "embedder.pid") "Embedder Worker"
Stop-ByPidFile (Join-Path $pidDir "api.pid") "API"

Write-Host "Done." -ForegroundColor Green
