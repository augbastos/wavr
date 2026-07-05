<#
.SYNOPSIS
    Headless Wavr runner invoked by the "Wavr Autostart" scheduled task (F4).

.DESCRIPTION
    NOT a user-facing launcher. This is the program the Task Scheduler action actually
    runs at logon: it starts `python -m wavr.serve` in its DEFAULT loopback mode (plain
    HTTP on 127.0.0.1), from the repo root, with all output appended to a size-capped log.

    Privacy invariants (unchanged by this script):
      * WAVR_MULTIDEVICE is NEVER set here, so serve.py:27 keeps the 127.0.0.1 plain-HTTP
        branch (ADR-0002 loopback-only / ADR-0007 no-silent-LAN). NOTE: if the operator
        already set WAVR_MULTIDEVICE=1 in .env or their user env, `python -m wavr.serve`
        still honours it; install-autostart.ps1 / -Status surface that so it stays visible.
      * Cameras always boot OFF (ADR-0002): this runner only starts the process; it never
        toggles or auto-resumes a camera source.
      * Zero cloud egress: the only network call here is the local loopback health probe.

    Credential safety: OpenCV's FFmpeg backend can print RTSP connection errors to native
    stderr WITHOUT redacting credentials embedded in the URL, and every stream is appended
    to the log. OPENCV_FFMPEG_LOGLEVEL is forced to quiet (-8, OpenCV >= 4.6) before launch
    so those raw URLs never land in logs\wavr-autostart.log.

    Log rotation runs ONCE, at launch: if the log is already >5MB it is rolled to a single
    wavr-autostart.log.1 backup (overwriting any prior .1) and a fresh file started. A
    long-lived instance can therefore exceed 5MB until the next logon/restart -- accepted.

.PARAMETER Port
    Port for the backend, passed through as WAVR_PORT so the health probe and the backend
    stay in sync. Default: $env:WAVR_PORT else 8000.
#>
[CmdletBinding()]
param(
    [int]$Port = $(if ($env:WAVR_PORT) { [int]$env:WAVR_PORT } else { 8000 })
)

$ErrorActionPreference = 'Stop'

# scripts\ -> repo root (same idiom as wavr.ps1:6), so load_dotenv() finds .\.env,
# wavr.db resolves at the root, and GET / serves frontend\index.html.
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# Resolve Python EXACTLY like wavr-desktop.ps1:36-40 -> console python.exe (NOT pythonw)
# so stdout/stderr can be redirected to the log.
$Python = $env:WAVR_PYTHON
if (-not $Python) {
    $venv = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    $Python = if (Test-Path $venv) { $venv } else { 'python' }
}

# Keep the health probe and the backend on the same port.
$env:WAVR_PORT = "$Port"

# Ensure the log dir exists before anything writes to it.
$LogDir = Join-Path $RepoRoot 'logs'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$Log = Join-Path $LogDir 'wavr-autostart.log'
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

# Restart-storm guard (wavr.ps1:10-14 pattern): if an instance already answers (e.g. the
# user ran wavr.ps1), do NOT fight for the port or trigger the task's failure-restart loop.
try {
    Invoke-WebRequest "http://127.0.0.1:$Port/api/system" -UseBasicParsing -TimeoutSec 1 | Out-Null
    Add-Content -Path $Log -Value "$stamp  Wavr already running on 127.0.0.1:$Port -- autostart runner exiting 0 (no port fight)."
    exit 0
} catch {}

# Log rotation BEFORE launch: single 5MB-capped backup (a hard cap, not unbounded growth).
if ((Test-Path $Log) -and ((Get-Item $Log).Length -gt 5MB)) {
    Move-Item -Path $Log -Destination "$Log.1" -Force
}

# Credential-safety insurance: quiet OpenCV/FFmpeg so RTSP errors WITH embedded creds on
# native stderr never reach the log (OpenCV >= 4.6).
$env:OPENCV_FFMPEG_LOGLEVEL = '-8'

Add-Content -Path $Log -Value "$stamp  Starting Wavr (loopback-only) on 127.0.0.1:$Port via `"$Python`" -m wavr.serve"

# In-process launch: the task starts THIS powershell host -WindowStyle Hidden, so the
# python child shares that hidden console (no flashing window) while all streams append
# to the log. Propagate the child's exit code so the task's restart-on-failure can act.
& $Python -m wavr.serve *>> $Log
exit $LASTEXITCODE
