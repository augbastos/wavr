# Wavr — abrir sob demanda (Fase 0 item 5 do plano de deploy).
# Sobe o servidor em loopback, abre o dashboard no browser quando ele responder.
# Fechar esta janela (ou Ctrl+C) mata o processo -> VRAM 100% de volta pros jogos.

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot   # scripts\ -> raiz do repo
$url  = 'http://127.0.0.1:8000'

# Ja tem um Wavr rodando? So abre o dashboard e sai.
try {
    Invoke-WebRequest "$url/api/system" -UseBasicParsing -TimeoutSec 1 | Out-Null
    Start-Process $url
    exit 0
} catch {}

# Abre o browser em paralelo assim que o servidor responder (ate ~15s).
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

# Rodar da raiz do repo: load_dotenv acha .\.env, wavr.db fica na raiz,
# e o GET / resolve frontend\index.html. Bind loopback-only (guard do app).
Set-Location $repo
& "$repo\.venv\Scripts\python.exe" -m uvicorn wavr.app:app --host 127.0.0.1 --port 8000
