<#
.SYNOPSIS
    Remove the per-user "Wavr Autostart" logon task (F4). Idempotent.

.DESCRIPTION
    Stops and unregisters ONLY the "Wavr Autostart" scheduled task. It NEVER taskkills
    python or touches any process it did not start (known trap #1): a backend a user
    launched manually via scripts\wavr.ps1 is deliberately left running. Stopping/
    unregistering the task is the entire surface of this script.

    Idempotent: if the task is not installed, it prints a note and exits 0.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\uninstall-autostart.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$TaskName = 'Wavr Autostart'

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "Task '$TaskName': not installed (nothing to do)."
    exit 0
}

# Stop ONLY the task-owned instance (never a user's manual wavr.ps1 backend), then remove.
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Task '$TaskName': removed."
exit 0
