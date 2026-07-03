<#
.SYNOPSIS
    Wavr Desktop (interim launcher) — start the loopback central and open the dashboard.

.DESCRIPTION
    The "click-to-open" experience without waiting for the Tauri build (ADR-0007): starts
    `python -m wavr.serve` in its DEFAULT loopback mode (HTTP on 127.0.0.1:<port>, no LAN
    exposure), waits until the API answers, then opens the dashboard in your browser.

    Nothing here changes the backend or its privacy model — it is loopback-only, exactly
    like running uvicorn by hand. LAN access stays a separate, explicit opt-in
    (set WAVR_MULTIDEVICE=1 and use `python -m wavr.serve` directly).

.PARAMETER Port
    Port for the backend (default 8000, or $env:WAVR_PORT).

.PARAMETER NoBrowser
    Start the backend but do not open a browser.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\wavr-desktop.ps1
#>
[CmdletBinding()]
param(
    [int]$Port = $(if ($env:WAVR_PORT) { [int]$env:WAVR_PORT } else { 8000 }),
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'

# Repo root = this script's parent directory's parent (scripts/ -> repo root).
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Url = "http://127.0.0.1:$Port"

# Resolve the Python interpreter: WAVR_PYTHON, else the repo venv, else PATH.
$Python = $env:WAVR_PYTHON
if (-not $Python) {
    $venv = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    $Python = if (Test-Path $venv) { $venv } else { 'python' }
}

function Test-Backend {
    try {
        $r = Invoke-WebRequest -Uri "$Url/api/state" -UseBasicParsing -TimeoutSec 2
        return $r.StatusCode -eq 200
    } catch { return $false }
}

if (Test-Backend) {
    Write-Host "Wavr already running at $Url" -ForegroundColor Green
} else {
    Write-Host "Starting Wavr backend ($Python -m wavr.serve) on $Url ..." -ForegroundColor Cyan
    # Loopback mode: DO NOT set WAVR_MULTIDEVICE here. Run from the repo root so the
    # backend's load_dotenv() finds ./.env and wavr.db resolves as usual.
    $env:WAVR_PORT = "$Port"
    $proc = Start-Process -FilePath $Python -ArgumentList '-m', 'wavr.serve' `
        -WorkingDirectory $RepoRoot -WindowStyle Minimized -PassThru

    $deadline = (Get-Date).AddSeconds(30)
    while (-not (Test-Backend)) {
        if ($proc.HasExited) {
            throw "Backend exited early (code $($proc.ExitCode)). Check the venv and deps: pip install -e backend"
        }
        if ((Get-Date) -gt $deadline) {
            throw "Backend did not become healthy at $Url within 30s."
        }
        Start-Sleep -Milliseconds 400
    }
    Write-Host "Wavr is up (pid $($proc.Id))." -ForegroundColor Green
}

if (-not $NoBrowser) {
    Start-Process $Url
}

Write-Host "Dashboard: $Url" -ForegroundColor Green
Write-Host "The backend keeps running in a minimized window. Close that window to stop sensing." -ForegroundColor DarkGray
