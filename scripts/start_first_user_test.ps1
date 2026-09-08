# Launches the first-user test portal: regenerates the release manifest,
# serves the site and the installers to every device on this Wi-Fi, opens
# the browser, and prints a QR code -- all from one double-click.
#
# This wraps first_user_portal.py rather than being the logic itself. The
# reason is a PowerShell console window: it is what lets a failure stay ON
# SCREEN (the Read-Host at the bottom) instead of flashing and closing,
# which is what double-clicking a bare .py file does the moment it errors.
#
# Como usar: nao clique aqui direto -- use o atalho
# "_local/first-user-rc/Comecar o teste do Wavr.lnk", que chama este
# arquivo com os parametros certos.

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

# This file lives at <repoRoot>/scripts/start_first_user_test.ps1.
$repoRoot = Split-Path -Parent $PSScriptRoot

function Escreva([string] $texto) { Write-Host $texto }
function Titulo([string] $texto) {
    Write-Host ''
    Write-Host $texto -ForegroundColor Cyan
}

Titulo 'Wavr -- teste do primeiro usuario'

$pythonCandidates = @(
    'C:\Python314\python.exe',
    (Join-Path $repoRoot '.venv\Scripts\python.exe')
)
$python = $pythonCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $python) {
    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if ($onPath) { $python = $onPath.Source }
}

if (-not $python) {
    Escreva 'Nao encontrei nenhum Python nesta maquina (procurei em'
    Escreva 'C:\Python314, no .venv do projeto e no PATH).'
    Escreva 'Sem Python, este portal nao consegue subir.'
    Read-Host 'Aperte Enter para fechar'
    exit 1
}

$portalScript = Join-Path $repoRoot 'scripts\first_user_portal.py'
if (-not (Test-Path $portalScript)) {
    Escreva "Nao encontrei $portalScript."
    Escreva 'A instalacao do Wavr parece incompleta nesta pasta.'
    Read-Host 'Aperte Enter para fechar'
    exit 1
}

Escreva "Usando Python: $python"
Escreva ''

$exitCode = 0
try {
    & $python $portalScript
    $exitCode = $LASTEXITCODE
} catch {
    Escreva ''
    Escreva 'Alguma coisa deu errado ao iniciar o Wavr:'
    Escreva $_.Exception.Message
    $exitCode = 1
}

if ($exitCode -ne 0) {
    Titulo 'O portal do Wavr terminou com um erro (veja acima).'
    Read-Host 'Aperte Enter para fechar'
} else {
    Titulo 'Portal do Wavr encerrado.'
    Start-Sleep -Seconds 2
}
