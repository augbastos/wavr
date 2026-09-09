# Wavr - open on demand (Phase 0, item 5 of the deployment plan).
# Starts the server on loopback and opens the dashboard once it answers.
# Closing this window (or Ctrl+C) kills the process, which releases the VRAM.

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot   # scripts\ -> repository root
$url  = 'http://127.0.0.1:8000'

# Already running? Just open the dashboard and leave.
try {
    Invoke-WebRequest "$url/api/system" -UseBasicParsing -TimeoutSec 1 | Out-Null
    Start-Process $url
    exit 0
} catch {}

# Open the browser in parallel, as soon as the server answers (up to ~15s).
Start-Job -ArgumentList $url {
    param($url)
    for ($i = 0; $i -lt 30; $i++) {
        try {
            Invoke-WebRequest "$url/api/system" -UseBasicParsing -TimeoutSec 1 | Out-Null
            Start-Process $url
            break
        } catch { Start-Sleep -Milliseconds 500 }
    }
} | Out-Null

# Run from the repository root: load_dotenv finds .\.env, wavr.db lands at the
# root, and GET / resolves frontend\index.html. Loopback-only bind (app guard).
Set-Location $repo
& "$repo\.venv\Scripts\python.exe" -m uvicorn wavr.app:app --host 127.0.0.1 --port 8000
