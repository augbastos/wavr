# Wavr — desktop launcher: bring the loopback/LAN central up if it isn't already,
# then open the dashboard in the default browser. Reads ./.env, so whatever mode is
# configured (loopback HTTP or multidevice HTTPS) is what serves. Safe to double-click
# repeatedly — if Wavr is already up it just opens the dashboard.
$ErrorActionPreference = 'SilentlyContinue'
$repo = 'C:\IA\wavr'
$py   = Join-Path $repo '.venv\Scripts\python.exe'

function Test-Up { (Test-NetConnection -ComputerName 127.0.0.1 -Port 8000 -InformationLevel Quiet) }

# Scheme follows the configured mode: multidevice -> https, else http.
$multi = $false
$envFile = Join-Path $repo '.env'
if (Test-Path $envFile) {
    if (Select-String -Path $envFile -Pattern '^\s*WAVR_MULTIDEVICE\s*=\s*(1|true|yes)' -Quiet) { $multi = $true }
}
$scheme = if ($multi) { 'https' } else { 'http' }

if (-not (Test-Up)) {
    Start-Process -FilePath $py -ArgumentList '-m', 'wavr.serve' -WorkingDirectory $repo -WindowStyle Minimized
    $deadline = (Get-Date).AddSeconds(25)
    while (-not (Test-Up) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
}

Start-Process "$scheme`://127.0.0.1:8000"
