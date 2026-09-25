<#
.SYNOPSIS
  One local validation entry point for Wavr development. It runs the checks
  that already exist -- it adds none of its own -- and says PASS, FAIL or SKIP
  (with the reason) for each.

.DESCRIPTION
  Tiers:
    -Fast     (default) generated files current, publication gate, the Python
              suite without the browser tests, native build + conformance.
    -Full     + the whole Python suite, the native end-to-end tests against a
              real Core (Node protocol, command contract), the Android app
              (Gradle) and the desktop client (cargo) when their toolchains exist.
    -Release  + the guarantee mutation check and a native packaging dry run.

  Two traps are closed here once, for every caller:
    * PYTHONPATH points at THIS checkout's backend/: an interpreter with Wavr
      installed editable from another checkout otherwise tests that one.
    * pytest gets a private --basetemp: a shared pytest-of-<user> directory that
      another process holds makes every test error before it runs.

  Nothing here pushes, publishes or changes anything outside the build dirs.

.EXAMPLE
  ./scripts/validate-dev.ps1 -Python C:\path\to\venv\Scripts\python.exe
  ./scripts/validate-dev.ps1 -Full -Json $env:TEMP\wavr-validate.json
#>
[CmdletBinding()]
param(
    [switch]$Fast,
    [switch]$Full,
    [switch]$Release,
    [string]$Python = "python",
    [string]$BuildDir = "",
    [string]$Json = ""
)
$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Tier = if ($Release) { 3 } elseif ($Full) { 2 } else { 1 }
if (-not $BuildDir) { $BuildDir = Join-Path $Repo "build/native" }
$env:PYTHONPATH = Join-Path $Repo "backend"
$env:PYTHON_DOTENV_DISABLED = "1"
$BaseTemp = Join-Path ([System.IO.Path]::GetTempPath()) "wavr-validate-$PID"
$env:PYTEST_ADDOPTS = "-p no:cacheprovider --basetemp=`"$BaseTemp`""
$Results = [System.Collections.Generic.List[object]]::new()

function Step([string]$Name, [int]$MinTier, [scriptblock]$Body, [string]$SkipIf = "") {
    if ($Tier -lt $MinTier) { return }
    if ($SkipIf) {
        $Results.Add([pscustomobject]@{ step = $Name; result = "SKIP"; detail = $SkipIf })
        Write-Host ("SKIP  {0}  ({1})" -f $Name, $SkipIf)
        return
    }
    Write-Host ("...   {0}" -f $Name)
    $t = [System.Diagnostics.Stopwatch]::StartNew()
    $out = & $Body 2>&1 | Out-String
    $ok = $LASTEXITCODE -eq 0
    $tail = ($out.Trim() -split "`n" | Select-Object -Last 3) -join " | "
    $Results.Add([pscustomobject]@{ step = $Name; result = $(if ($ok) { "PASS" } else { "FAIL" });
                                    detail = $tail; seconds = [math]::Round($t.Elapsed.TotalSeconds) })
    Write-Host ("{0}  {1}  ({2}s)" -f $(if ($ok) { "PASS" } else { "FAIL" }), $Name,
                [math]::Round($t.Elapsed.TotalSeconds))
    if (-not $ok) { Write-Host $out }
}

function Have([string]$Cmd) { [bool](Get-Command $Cmd -ErrorAction SilentlyContinue) }

Push-Location $Repo
try {
    $py = (& $Python -c "import sys; print(sys.version_info >= (3, 11))" 2>$null)
    if ($py -ne "True") { throw "no Python 3.11+ at '$Python' (pass -Python)" }

    # -- generated files and the publication gate ----------------------------------
    Step "conformance fixtures current" 1 { & $Python scripts/gen_conformance.py --check }
    Step "design tokens current" 1 { & $Python scripts/gen_design_tokens.py --check }
    Step "publication gate" 1 { & $Python scripts/publication_gate.py }

    # -- Python -------------------------------------------------------------------
    $browser = Get-ChildItem backend/tests -Filter "test_*.py" |
        Where-Object { Select-String -Path $_.FullName -Pattern "playwright" -Quiet } |
        ForEach-Object { "--ignore=$($_.FullName)" }
    if ($Tier -eq 1) {
        Step "python suite (no browser tests)" 1 { & $Python -m pytest backend/tests -q @browser }
    } else {
        Step "python suite (full)" 2 { & $Python -m pytest backend/tests -q }
    }

    # -- native -------------------------------------------------------------------
    $noCmake = if (-not (Have cmake)) { "cmake not on PATH" } else { "" }
    Step "native build" 1 -SkipIf $noCmake {
        if (-not (Test-Path (Join-Path $BuildDir "CMakeCache.txt"))) {
            cmake -S native -B $BuildDir -G Ninja -DCMAKE_BUILD_TYPE=Release
            if ($LASTEXITCODE -ne 0) { return }
        }
        cmake --build $BuildDir
    }
    Step "native conformance + C consumer" 1 -SkipIf $noCmake { ctest --test-dir $BuildDir --output-on-failure }
    $exe = @("wavr.exe", "wavr") | ForEach-Object { Join-Path $BuildDir $_ } | Where-Object { Test-Path $_ } | Select-Object -First 1
    $noExe = if (-not $exe) { "no native wavr binary in $BuildDir" } else { "" }
    Step "native e2e: Node protocol vs a real Core" 2 -SkipIf $noExe { & $Python native/tests/e2e_node.py --wavr $exe }
    Step "native e2e: command contract across the LAN" 2 -SkipIf $noExe { & $Python native/tests/e2e_commands.py --wavr $exe }

    # -- clients ------------------------------------------------------------------
    $noAndroid = if (-not (Test-Path core-launcher/local.properties) -and -not $env:ANDROID_HOME) { "no Android SDK configured" } else { "" }
    Step "Android app: assemble + unit tests" 2 -SkipIf $noAndroid {
        Push-Location core-launcher
        try { ./gradlew --offline -q assembleDebug testDebugUnitTest } finally { Pop-Location }
    }
    $lib = @("libwavr_native.dll", "libwavr_native.so") | ForEach-Object { Join-Path $BuildDir $_ } | Where-Object { Test-Path $_ } | Select-Object -First 1
    $noCargo = if (-not (Have cargo)) { "cargo not on PATH" } else { "" }
    Step "desktop client: cargo test" 2 -SkipIf $noCargo {
        $env:WAVR_NATIVE_LIB = $(if ($lib) { $lib } else { "" })
        Push-Location clients/desktop
        try { cargo test --offline -q } finally { Pop-Location }
    }

    # -- release ------------------------------------------------------------------
    Step "guarantees fail when broken (mutation check)" 3 { & $Python scripts/check_guarantees.py }
    Step "native packaging dry run" 3 -SkipIf $noExe {
        $dist = Join-Path ([System.IO.Path]::GetTempPath()) "wavr-validate-dist-$PID"
        $target = if ($IsLinux) { "x86_64-linux-gnu" } else { "x86_64-windows-gnu" }
        & $Python scripts/package_native.py --out $dist "$target=$BuildDir"
        $code = $LASTEXITCODE
        Remove-Item -Recurse -Force $dist -ErrorAction SilentlyContinue
        $global:LASTEXITCODE = $code
    }
}
finally {
    Pop-Location
    Remove-Item -Recurse -Force $BaseTemp -ErrorAction SilentlyContinue
}

$failed = @($Results | Where-Object result -eq "FAIL").Count
$verdict = if ($failed) { "FAIL" } else { "PASS" }
Write-Host ""
$Results | Format-Table step, result, detail -AutoSize | Out-String -Width 200 | Write-Host
Write-Host "validate-dev ($(@('', 'fast', 'full', 'release')[$Tier])): $verdict"
if ($Json) {
    [pscustomobject]@{ tier = @('', 'fast', 'full', 'release')[$Tier]; verdict = $verdict;
                       commit = (git -C $Repo rev-parse HEAD); steps = $Results } |
        ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 $Json
}
exit $(if ($failed) { 1 } else { 0 })
