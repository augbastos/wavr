<#
.SYNOPSIS
    Register (or query) the per-user "Wavr Autostart" logon task (F4). Idempotent.

.DESCRIPTION
    Idempotently registers a Windows Task Scheduler task that starts the Wavr backend
    headless at logon, in its DEFAULT loopback mode, from the repo root, via
    scripts\wavr-headless.ps1. Current-user only, RunLevel Limited (NO admin), restart-on-
    failure (3x, 1 min apart), single instance, no visible window.

    This changes NOTHING about the backend or its privacy model -- the task runs
    `python -m wavr.serve` exactly like running it by hand: loopback-only, cameras boot OFF
    (ADR-0002), LAN stays a separate explicit opt-in (ADR-0007). The runner never sets
    WAVR_MULTIDEVICE.

    ADR-0007 (no silent LAN): `python -m wavr.serve` still honours an existing
    WAVR_MULTIDEVICE=1 from .env or the user env, so an operator who once opted in would
    get an UNATTENDED LAN+TLS listener at every logon. This script therefore checks the
    effective network mode -- at install and under -Status -- and prints 'loopback-only'
    vs a LAN-mode warning so the choice stays visible.

.PARAMETER Port
    Port passed to the runner (and its health probe). Default: $env:WAVR_PORT else 8000.

.PARAMETER Status
    Print whether the task is installed, its last/next run info, and the effective network
    mode, then exit.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Status
#>
[CmdletBinding()]
param(
    [int]$Port = $(if ($env:WAVR_PORT) { [int]$env:WAVR_PORT } else { 8000 }),
    [switch]$Status
)

$ErrorActionPreference = 'Stop'

$TaskName = 'Wavr Autostart'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Runner   = Join-Path $RepoRoot 'scripts\wavr-headless.ps1'

# Surface the EFFECTIVE network mode (ADR-0007 no-silent-LAN). The runner never sets
# WAVR_MULTIDEVICE, but serve.py honours it if it is already truthy in the user env or
# the repo .env (which load_dotenv reads at launch). Truthiness mirrors config.py:255.
function Get-WavrLanMode {
    $truthy = @('1', 'true', 'yes')
    if ($env:WAVR_MULTIDEVICE -and ($truthy -contains $env:WAVR_MULTIDEVICE.Trim().ToLower())) {
        return @{ Lan = $true; Reason = 'WAVR_MULTIDEVICE is set in the current user environment' }
    }
    $envFile = Join-Path $RepoRoot '.env'
    if (Test-Path $envFile) {
        $hit = Select-String -Path $envFile `
            -Pattern '^\s*WAVR_MULTIDEVICE\s*=\s*(1|true|yes)\b' -ErrorAction SilentlyContinue
        if ($hit) {
            return @{ Lan = $true; Reason = "WAVR_MULTIDEVICE is set in $envFile" }
        }
    }
    return @{ Lan = $false; Reason = '' }
}

$lan = Get-WavrLanMode

# ---- -Status branch ----------------------------------------------------------------
if ($Status) {
    if ($lan.Lan) {
        Write-Host "Network mode: LAN mode ($($lan.Reason))" -ForegroundColor Yellow
    } else {
        Write-Host "Network mode: loopback-only (WAVR_MULTIDEVICE not set)" -ForegroundColor Green
    }
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "Task '$TaskName': not installed"
        exit 0
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
    Write-Host "Task '$TaskName': installed"
    Write-Host "  State             : $($task.State)"
    Write-Host "  LastRunTime       : $($info.LastRunTime)"
    Write-Host "  LastTaskResult    : $($info.LastTaskResult)"
    Write-Host "  NextRunTime       : $($info.NextRunTime)"
    Write-Host "  NumberOfMissedRuns: $($info.NumberOfMissedRuns)"
    exit 0
}

# ---- Install branch ----------------------------------------------------------------
if ($lan.Lan) {
    Write-Host "WARNING: $($lan.Reason)." -ForegroundColor Yellow
    Write-Host "         'python -m wavr.serve' will start an UNATTENDED LAN+TLS listener at EVERY logon." -ForegroundColor Yellow
    Write-Host "         Unset WAVR_MULTIDEVICE (env + .env) first if you want loopback-only autostart." -ForegroundColor Yellow
} else {
    Write-Host "Network mode: loopback-only (WAVR_MULTIDEVICE not set)." -ForegroundColor Green
}

if (-not (Test-Path $Runner)) {
    throw "Runner not found: $Runner"
}

$psExe    = Join-Path $PSHOME 'powershell.exe'
$Argument = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Runner`" -Port $Port"

$Action    = New-ScheduledTaskAction -Execute $psExe -Argument $Argument -WorkingDirectory $RepoRoot
$Trigger   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited
$Settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -Hidden

# Idempotency: unregister-then-register so a second run re-registers cleanly (no duplicate).
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

try {
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
        -Principal $Principal -Settings $Settings `
        -Description 'Wavr loopback backend at logon (cameras stay OFF; loopback-only)' | Out-Null
} catch {
    Write-Host "Failed to register '$TaskName': $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "If this is an elevation error, re-run this script in an ELEVATED PowerShell (Run as administrator)." -ForegroundColor Yellow
    exit 1
}

Write-Host "Registered scheduled task '$TaskName' (per-user, at logon, loopback-only, port $Port)." -ForegroundColor Green
Write-Host "Check status: powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Status"
Write-Host "Remove      : powershell -ExecutionPolicy Bypass -File scripts\uninstall-autostart.ps1"
