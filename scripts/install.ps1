<#
.SYNOPSIS
    Wavr — one-shot Windows bootstrap. Installs (or upgrades) a standalone Wavr into
    %LOCALAPPDATA%\Wavr and starts it, without touching any dev `.venv` you may already
    have in a repo clone.

.DESCRIPTION
    Meant to be run either as `irm <url>/install.ps1 | iex` (fetched straight off the
    net, nothing local yet) or as a local file from inside a Wavr clone
    (`powershell -ExecutionPolicy Bypass -File scripts\install.ps1`). Both paths end up
    in the same place: a working venv at %LOCALAPPDATA%\Wavr\venv, an editable install
    of the `wavr` backend package, and the backend running at http://127.0.0.1:<port>.

    WHERE THE SOURCE COMES FROM (two modes, auto-detected):
      * Run from inside a Wavr checkout (this file's own folder, or the current
        directory, has sibling `backend\pyproject.toml` + `frontend\index.html`) ->
        that checkout is installed in place, editable. Nothing is downloaded.
      * Run standalone (piped, or from an unrelated folder) -> the `main` branch tip
        of $GitUrl is downloaded as a GitHub archive .zip (NOT `git clone`) into
        %LOCALAPPDATA%\Wavr\src and installed editable from there. Archive, not clone,
        on purpose: this repo's `.git` history is ~1.6GB (a past PII scrub), while the
        working tree is ~20MB — a branch-tip zip is the right tool, and it means this
        script never requires git to be installed at all.

    WHY EDITABLE, AND WHY THE ARCHIVE MUST KEEP `frontend\` NEXT TO `backend\`:
    `wavr.app` resolves its dashboard HTML via `Path(__file__).resolve().parents[2] /
    "frontend" / "index.html"` (backend\wavr\app.py) — i.e. it expects `frontend\` as a
    SIBLING of `backend\`, two levels above the installed `wavr` package. A normal
    (non-editable) `pip install` copies just the `wavr` package into site-packages,
    stranding it far from any `frontend\` folder — the dashboard 404s. Verified on this
    machine: a non-editable install's `wavr.app.__file__` lands under
    `...\Lib\site-packages\wavr\app.py`, whose parents[2] is nowhere near a checkout.
    An EDITABLE install keeps `__file__` pointing at the real checkout, where
    `frontend\` is right there — so this script (a) always keeps the full repo layout
    (never just the `backend\` subdirectory) and (b) always installs with `pip install
    --editable`, matching this repo's own CI (`pip install -e "backend[dev]"`,
    .github\workflows\tests.yml).

    THIS SCRIPT WRITES NO .env. Configuration (LAN mode, instance name, sensitivity,
    ...) is a job for the in-app settings UI (backend\wavr\settings_store.py, new on
    this branch) once you open the dashboard — never this installer.

    CAVEAT, VERIFIED ON THIS MACHINE: `wavr.config` calls python-dotenv's
    `load_dotenv()` with no path, which searches from the CALLING MODULE'S OWN FILE
    location, not from this script's data dir or working directory. So if you run
    this script against a local checkout that itself has a `.env` (a dev's own repo
    clone), that `.env` -- WAVR_MULTIDEVICE, GEMINI_API_KEY, whatever is in it -- is
    loaded into THIS install's process too, even though its data lives in a
    completely separate %LOCALAPPDATA%\Wavr. Concretely: a checkout with
    WAVR_MULTIDEVICE=1 and no [tls] extra installed makes the backend crash on start
    with "Local TLS needs the 'cryptography' package" -- fix with `-Extras tls`, or
    run this against a plain download instead of that checkout. A freshly-downloaded
    standalone source tree has no .env (it's git-ignored, never shipped) and is
    unaffected.

    Idempotent: re-running it upgrades the existing venv/source in place (pip
    --upgrade; the standalone source folder is simply re-fetched) rather than creating
    a second copy, and if Wavr is already answering on the target port it is left
    alone (no duplicate process, no port fight).

    Needs no admin. If Python 3.11+ isn't already on this machine, it tells you exactly
    what to install and where to get it, then stops — it never silently downloads and
    runs a Python installer on your behalf.

.PARAMETER InstallDir
    Where Wavr's own venv + data (wavr.db, house.json, ...) live. Default
    %LOCALAPPDATA%\Wavr. This directory is never removed by -Uninstall.

.PARAMETER GitUrl
    Canonical repo, used only to derive the branch-archive download URL for the
    standalone (no-local-checkout) path. Must be a github.com https URL today.

.PARAMETER Branch
    Branch to fetch when running standalone. Default 'main', which is this
    repository's default branch and the only branch the CI workflows trigger on.
    Renamed from 'master'; GitHub keeps redirecting the old name, but nothing here
    should depend on that redirect.

.PARAMETER InstallBaseUrl
    Where this script is meant to be hosted for the one-line installer
    (`irm $InstallBaseUrl/install.ps1 | iex`) — parameterisable, defaults to
    https://wavr.dev, the domain the install website (site/public/install.html,
    assets/wavr.js's BOOTSTRAP_DOMAIN) already names for this exact purpose. NOT LIVE
    YET as of this writing — that page says so itself ("Not live yet"). Purely
    cosmetic here: it only shows up in this script's own "how to re-run me" banner.
    Until it's live, use the raw GitHub URL instead:
        irm https://raw.githubusercontent.com/augbastos/wavr/main/scripts/install.ps1 | iex

.PARAMETER PythonPath
    Explicit path to a python.exe (>=3.11) to use, skipping auto-detection.

.PARAMETER Extras
    Comma-separated pyproject extras to install alongside the base package, e.g.
    "camera,mqtt" (see backend\pyproject.toml [project.optional-dependencies]).
    Default: base install only (network/BLE presence + fusion + dashboard; no
    torch/cv2). Heavy extras can take a while and need real disk space.

.PARAMETER Port
    Backend port. Default 8000, or $env:WAVR_PORT.

.PARAMETER NoBrowser
    Start the backend but do not open a browser tab.

.PARAMETER Uninstall
    Remove the venv, the standalone source download (if any) and the "Wavr Autostart"
    scheduled task (if one was registered via scripts\install-autostart.ps1) — but
    NEVER wavr.db or house.json. Their path is printed so you can remove them yourself
    if that is really what you want.

.PARAMETER DryRun
    Print every step this script would take (Python found, source resolution, venv,
    pip command, start command, URL) without creating, downloading, installing or
    starting anything.

.EXAMPLE
    irm https://raw.githubusercontent.com/augbastos/wavr/main/scripts/install.ps1 | iex

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -DryRun

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Wavr'),
    [string]$GitUrl = 'https://github.com/augbastos/wavr.git',
    [string]$Branch = 'main',
    [string]$InstallBaseUrl = 'https://wavr.dev',
    [string]$PythonPath = '',
    [string]$Extras = '',
    [int]$Port = $(if ($env:WAVR_PORT) { [int]$env:WAVR_PORT } else { 8000 }),
    [switch]$NoBrowser,
    [switch]$Uninstall,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Write-Info([string]$m) { Write-Host $m -ForegroundColor Cyan }
function Write-Ok([string]$m) { Write-Host $m -ForegroundColor Green }
function Write-Note([string]$m) { Write-Host $m -ForegroundColor Yellow }
function Write-Err2([string]$m) { Write-Host $m -ForegroundColor Red }
function Write-Would([string]$m) { Write-Host "[DryRun] would $m" -ForegroundColor DarkGray }

# ---- Windows / Python discovery ----------------------------------------------------

function Show-WavrEnvironment {
    $winVer = [System.Environment]::OSVersion.VersionString
    $arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture
    Write-Info "Windows: $winVer (arch: $arch)"
}

function Find-WavrPython {
    # Returns a python.exe path for the newest available interpreter >=3.11, or $null.
    # Prefers the `py` launcher's own registry (handles both its old "-3.11-64" tag
    # format and the newer "-V:3.11" tag format), falls back to `python`/`python3` on
    # PATH. Never guesses at a version it hasn't actually queried.
    param([string]$Explicit)

    if ($Explicit) {
        if (-not (Test-Path $Explicit)) { throw "PythonPath not found: $Explicit" }
        return (Resolve-Path $Explicit).Path
    }

    $candidates = New-Object System.Collections.Generic.List[object]

    if (Get-Command py -ErrorAction SilentlyContinue) {
        try {
            & py -0p 2>$null | ForEach-Object {
                # `py -0p` prints "<tag><spaces>[*]<spaces><path>", and the path
                # may contain spaces. Splitting on whitespace and taking the last
                # token therefore returns only the fragment after the interpreter's
                # LAST space -- on a machine whose Python lives under, say,
                # "...\Open Design\...", that yields a relative path with no drive
                # letter. The venv step then asks PowerShell to run a command named
                # after the second half of a directory, and the user sees an error
                # that never mentions Python.
                #
                # The path is everything after the tag, to end of line.
                if ($_ -match '^\s*-(?:V:)?(\d+)\.(\d+)\S*\s+\*?\s*(?<path>\S.*?)\s*$') {
                    $maj = [int]$Matches[1]; $min = [int]$Matches[2]
                    $path = $Matches['path']
                    # A candidate that is not a file on disk is a parse failure, not
                    # an interpreter. Dropping it here keeps a bad guess from
                    # becoming an unreadable error several steps later.
                    if ((($maj -eq 3) -and ($min -ge 11)) -or ($maj -gt 3)) {
                        if (Test-Path -LiteralPath $path -PathType Leaf) {
                            $candidates.Add([pscustomobject]@{ Path = $path; Ver = [version]"$maj.$min" })
                        }
                    }
                }
            }
        } catch {}
    }

    foreach ($name in 'python', 'python3') {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        try {
            $verStr = & $cmd.Source -c "import sys;print('{}.{}'.format(*sys.version_info[:2]))" 2>$null
            if ($verStr -match '^(\d+)\.(\d+)$') {
                $maj = [int]$Matches[1]; $min = [int]$Matches[2]
                if ((($maj -eq 3) -and ($min -ge 11)) -or ($maj -gt 3)) {
                    $candidates.Add([pscustomobject]@{ Path = $cmd.Source; Ver = [version]"$maj.$min" })
                }
            }
        } catch {}
    }

    if ($candidates.Count -eq 0) { return $null }
    return ($candidates | Sort-Object Ver -Descending | Select-Object -First 1).Path
}

function Show-PythonMissingHelp {
    Write-Err2 "No Python 3.11+ found (checked the 'py' launcher and 'python'/'python3' on PATH)."
    Write-Host ""
    Write-Host "Install Python 3.11 or newer yourself, then re-run this script:" -ForegroundColor Yellow
    Write-Host "  * Download:  https://www.python.org/downloads/windows/  (check ""Add python.exe to PATH"")"
    Write-Host "  * Or, if you use winget:  winget install --id Python.Python.3.12 -e"
    Write-Host ""
    Write-Host "This script never downloads or runs a Python installer for you." -ForegroundColor Yellow
}

# ---- Source resolution (local checkout vs standalone archive download) -------------

function Resolve-WavrRepoRoot {
    # A "checkout" = a folder with backend\pyproject.toml AND frontend\index.html as
    # direct children (see the .DESCRIPTION note on why both must be present).
    # $PSScriptRoot is EMPTY under `irm ... | iex` (verified: Invoke-Expression has no
    # script file, so there is no script root) -- that's fine, it just means this
    # candidate is skipped and we fall through to the current directory check, which is
    # how a user who `cd`'d into a checkout before pasting the one-liner gets picked up.
    $candidates = @()
    if ($PSScriptRoot) { $candidates += (Split-Path -Parent $PSScriptRoot) }
    $candidates += (Get-Location).Path

    foreach ($c in $candidates) {
        $pj = Join-Path $c 'backend\pyproject.toml'
        $fe = Join-Path $c 'frontend\index.html'
        if ((Test-Path $pj) -and (Test-Path $fe)) { return $c }
    }
    return $null
}

function Get-WavrSource {
    param([string]$InstallDir, [string]$GitUrl, [string]$Branch, [switch]$DryRun)

    $repo = Resolve-WavrRepoRoot
    if ($repo) {
        Write-Info "Found a Wavr checkout at $repo -- installing from it (no download)."
        return $repo
    }

    if ($GitUrl -notmatch '^https://github\.com/([^/]+)/([^/.]+?)(\.git)?/?$') {
        throw "GitUrl must be a github.com https URL for the standalone (no local checkout) path; got: $GitUrl"
    }
    $owner = $Matches[1]; $repoName = $Matches[2]
    $archiveUrl = "https://github.com/$owner/$repoName/archive/refs/heads/$Branch.zip"
    $srcDir = Join-Path $InstallDir 'src\wavr'

    if ($DryRun) {
        Write-Would "download $archiveUrl and extract it to $srcDir (replacing any previous copy)"
        return $srcDir
    }

    Write-Info "No local checkout found -- downloading Wavr ($Branch) from $archiveUrl ..."
    $zipPath = Join-Path $env:TEMP "wavr-src-$([guid]::NewGuid().ToString('N')).zip"
    $extractRoot = Join-Path $env:TEMP "wavr-src-$([guid]::NewGuid().ToString('N'))"
    try {
        Invoke-WebRequest -Uri $archiveUrl -OutFile $zipPath -UseBasicParsing
        Expand-Archive -Path $zipPath -DestinationPath $extractRoot -Force
        $inner = Get-ChildItem -Path $extractRoot -Directory | Select-Object -First 1
        if (-not $inner) { throw "Downloaded archive had no top-level folder (unexpected GitHub zip layout)." }
        $srcParent = Split-Path -Parent $srcDir
        New-Item -ItemType Directory -Force -Path $srcParent | Out-Null
        if (Test-Path $srcDir) { Remove-Item -Recurse -Force $srcDir }
        Move-Item -Path $inner.FullName -Destination $srcDir
    } finally {
        Remove-Item -Force $zipPath -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force $extractRoot -ErrorAction SilentlyContinue
    }
    return $srcDir
}

# ---- Health check --------------------------------------------------------------------

function Test-WavrHealthy([string]$Url) {
    try {
        $r = Invoke-WebRequest -Uri "$Url/healthz" -UseBasicParsing -TimeoutSec 2
        return $r.StatusCode -eq 200
    } catch { return $false }
}

# ---- Uninstall -------------------------------------------------------------------

function Invoke-WavrUninstall {
    param([string]$InstallDir, [switch]$DryRun)

    $VenvDir = Join-Path $InstallDir 'venv'
    $SrcDir = Join-Path $InstallDir 'src'
    $DbPath = Join-Path $InstallDir 'wavr.db'
    $HouseMap = Join-Path $InstallDir 'house.json'
    $TaskName = 'Wavr Autostart'

    # Venv + source removal first: this is the part of "uninstall" that must always
    # happen. Task-Scheduler access turns out to be flaky in at least one real
    # environment this was tested in (Get-ScheduledTask threw a terminating "Access is
    # denied" that -ErrorAction SilentlyContinue did NOT suppress -- ScheduledTasks is a
    # CDXML/WMI-backed module, and those raise hard exceptions for real access failures
    # rather than writing to the non-terminating error stream). Wrapping it in its own
    # try/catch means a flaky/locked-down Task Scheduler can never abort the rest of the
    # uninstall or, worse, skip the "your data is untouched" notice below.
    # Stop the backend THIS installer started, and only that one. Windows will not
    # delete a file a running process holds open, so removing the venv underneath a
    # live Core fails on a locked .pyd with a message that never mentions Wavr --
    # which is the ordinary path, because installing ends by starting it.
    #
    # The pid comes from the file the install step wrote. It is re-checked against
    # this installation's own venv before anything is stopped: a pid file can
    # outlive its process and the operating system reuses pids, so a stale number
    # must never be enough to kill something. If it does not match, it is left
    # alone -- the existing rule stands, and nothing is stopped that was not
    # started here.
    $PidFile = Join-Path $InstallDir 'wavr.pid'
    if (Test-Path $PidFile) {
        try {
            $wavrPid = [int](Get-Content -LiteralPath $PidFile -Raw).Trim()
            $proc = Get-Process -Id $wavrPid -ErrorAction SilentlyContinue
            $ours = $proc -and $proc.Path -and
                    $proc.Path.StartsWith($VenvDir, [StringComparison]::OrdinalIgnoreCase)
            if ($ours) {
                if ($DryRun) { Write-Would "stop the Wavr backend this installer started (pid $wavrPid)" }
                else {
                    Stop-Process -Id $wavrPid -Force -ErrorAction SilentlyContinue
                    $proc.WaitForExit(10000) | Out-Null
                    Write-Ok "Stopped the Wavr backend this installer started (pid $wavrPid)."
                }
            } elseif ($proc) {
                Write-Note "pid $wavrPid is not this installation's backend -- left alone."
            }
        } catch {}
        if (-not $DryRun) { Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue }
    }

    foreach ($dir in @($VenvDir, $SrcDir)) {
        if (Test-Path $dir) {
            if ($DryRun) { Write-Would "remove $dir" }
            else { Remove-Item -Recurse -Force $dir; Write-Ok "Removed $dir" }
        }
    }

    # Same task name as scripts\install-autostart.ps1 / uninstall-autostart.ps1, so
    # this cleans up whichever of the two registered it. Never touches a process it
    # did not itself stop (known trap: don't taskkill things you didn't start) -- this
    # only ever stops/unregisters the TASK, never a manually-launched backend.
    try {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($task) {
            if ($DryRun) { Write-Would "stop and unregister the scheduled task '$TaskName'" }
            else {
                Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
                Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
                Write-Ok "Removed scheduled task '$TaskName'."
            }
        } else {
            Write-Info "Scheduled task '$TaskName' was not installed -- nothing to remove there."
        }
    } catch {
        Write-Note "Could not query/remove the '$TaskName' scheduled task ($($_.Exception.Message))."
        Write-Note "Remove it yourself if present: Task Scheduler -> '$TaskName', or:  Unregister-ScheduledTask -TaskName `"$TaskName`" -Confirm:`$false"
    }

    # NEVER delete data. Refuse and say exactly where it is -- the operator decides.
    if (Test-Path $DbPath) {
        Write-Note "Your database is untouched: $DbPath"
        Write-Note "This script never deletes it. To remove it yourself:  Remove-Item -Force `"$DbPath`""
    }
    if (Test-Path $HouseMap) {
        Write-Note "Your floor plan is untouched: $HouseMap"
    }
    Write-Ok "Uninstall complete."
}

# ---- Main ----------------------------------------------------------------------------

try {
    if ($Uninstall) {
        Invoke-WavrUninstall -InstallDir $InstallDir -DryRun:$DryRun
        exit 0
    }

    if ($DryRun) { Write-Note "DRY RUN -- nothing will be downloaded, installed or started." }

    Show-WavrEnvironment

    $PythonExe = Find-WavrPython -Explicit $PythonPath
    if (-not $PythonExe) {
        Show-PythonMissingHelp
        exit 1
    }
    Write-Info "Using Python: $PythonExe"

    $SourceDir = Get-WavrSource -InstallDir $InstallDir -GitUrl $GitUrl -Branch $Branch -DryRun:$DryRun
    $BackendDir = Join-Path $SourceDir 'backend'

    if (-not $DryRun) { New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null }

    $VenvDir = Join-Path $InstallDir 'venv'
    $VenvPython = Join-Path $VenvDir 'Scripts\python.exe'

    if (Test-Path $VenvPython) {
        Write-Info "Reusing existing venv at $VenvDir"
    } elseif ($DryRun) {
        Write-Would "create a venv at $VenvDir using $PythonExe"
    } else {
        Write-Info "Creating venv at $VenvDir ..."
        & $PythonExe -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) { throw "venv creation failed (exit $LASTEXITCODE). Is the 'venv' module available for this Python?" }
    }

    $pipTarget = if ($Extras) { "$BackendDir[$Extras]" } else { $BackendDir }
    if ($DryRun) {
        Write-Would "run: `"$VenvPython`" -m pip install --upgrade --editable `"$pipTarget`""
    } else {
        Write-Info "Installing Wavr (editable) from $BackendDir ..."
        & $VenvPython -m pip install --upgrade pip --quiet
        & $VenvPython -m pip install --upgrade --editable $pipTarget
        if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)." }
    }

    # Data dir = InstallDir itself (never the source checkout, so re-fetching the
    # source on the next upgrade can never touch it). WAVR_DB set explicitly per the
    # brief; -WorkingDirectory below also points here so the default cwd-relative
    # house.json (config.py's WAVR_HOUSE_MAP default) lands next to it, not in $PWD.
    $DbPath = Join-Path $InstallDir 'wavr.db'
    $Url = "http://127.0.0.1:$Port"

    if (Test-WavrHealthy $Url) {
        Write-Ok "Wavr is already running at $Url -- not starting a second copy."
    } elseif ($DryRun) {
        Write-Would "start: `"$VenvPython`" -m wavr.serve (cwd=$InstallDir, WAVR_DB=$DbPath, WAVR_PORT=$Port)"
    } else {
        Write-Info "Starting Wavr backend on $Url ..."
        $env:WAVR_DB = $DbPath
        $env:WAVR_PORT = "$Port"
        $proc = Start-Process -FilePath $VenvPython -ArgumentList '-m', 'wavr.serve' `
            -WorkingDirectory $InstallDir -WindowStyle Minimized -PassThru

        $deadline = (Get-Date).AddSeconds(30)
        while (-not (Test-WavrHealthy $Url)) {
            if ($proc.HasExited) { throw "Backend exited early (code $($proc.ExitCode)). Check $InstallDir for clues." }
            if ((Get-Date) -gt $deadline) { throw "Backend did not become healthy at $Url within 30s." }
            Start-Sleep -Milliseconds 400
        }
        # Record the pid we started, so -Uninstall can stop THIS backend without
        # ever reaching for a process it did not start. Without it, uninstalling
        # a running install fails on a locked .pyd with a message that never
        # mentions Wavr.
        try {
            Set-Content -LiteralPath (Join-Path $InstallDir 'wavr.pid') `
                        -Value $proc.Id -Encoding ascii
        } catch {}
        Write-Ok "Wavr is up (pid $($proc.Id))."
    }

    if (-not $NoBrowser -and -not $DryRun) { Start-Process $Url }

    $rawUrl = "https://raw.githubusercontent.com/augbastos/wavr/$Branch/scripts/install.ps1"

    Write-Host ""
    Write-Ok "Wavr install dir : $InstallDir"
    Write-Ok "Dashboard        : $Url"
    Write-Host "Re-run this same one-liner any time to upgrade in place:"
    Write-Host "  irm $InstallBaseUrl/install.ps1 | iex          # once that domain is real"
    Write-Host "  irm $rawUrl | iex   # works today"
    Write-Host "Uninstall (keeps your data):"
    Write-Host "  If you saved this file:  powershell -File scripts\install.ps1 -Uninstall"
    Write-Host "  If you only ever piped it (verified this actually passes -Uninstall through):"
    Write-Host "    iex ""& { `$(irm $rawUrl) } -Uninstall"""
} catch {
    Write-Err2 "Wavr install failed: $($_.Exception.Message)"
    exit 1
}
