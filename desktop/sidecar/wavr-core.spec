# PyInstaller spec for the Wavr Core sidecar.
#
# ADR-0007 shipped the desktop shell with "spawn-not-bundle": it launches
# `python -m wavr.serve` and expects a Python on the machine. That is fine for a
# developer and wrong for everyone else — "install Python 3.11+, tick Add to
# PATH" is exactly the developer knowledge the install experience is supposed to
# remove. This is the follow-up that ADR named and never built.
#
# ONEFILE, and the reason is Tauri rather than preference. `bundle.externalBin`
# takes a single executable per target triple; a onedir tree cannot be declared
# that way. Onefile costs a ~1-2s self-extraction on each launch, which the
# shell's existing "Starting Wavr…" splash already covers — it polls /healthz
# for up to 20s before giving up. The alternative (ship a directory as a Tauri
# resource and spawn into it) saves that second at the cost of a bespoke layout
# on every platform, which is not worth it for a startup this rare.
#
# Build:
#   pyinstaller desktop/sidecar/wavr-core.spec --distpath desktop/sidecar/dist
# then rename to the target triple Tauri expects, e.g.
#   wavr-core-x86_64-pc-windows-msvc.exe   -> desktop/src-tauri/binaries/
#
# The frontend is bundled as data and found at runtime by `app._find_frontend`
# via `sys._MEIPASS`. Without that the binary would start, answer /healthz, and
# serve no dashboard — the same failure the Docker image had.

from pathlib import Path

# `SPECPATH` is injected by PyInstaller; fall back for a direct read.
_here = Path(globals().get("SPECPATH", ".")).resolve()
REPO = _here.parents[1]        # desktop/sidecar -> repo root

block_cipher = None

a = Analysis(
    [str(REPO / "backend" / "wavr" / "serve.py")],
    pathex=[str(REPO / "backend")],
    binaries=[],
    datas=[
        # The dashboard. Not optional: without it the Core serves an API and
        # 404s on `GET /`.
        (str(REPO / "frontend"), "frontend"),
    ],
    hiddenimports=[
        # uvicorn and its standard extras resolve these by NAME at runtime, so
        # static analysis never sees them and the frozen binary dies on start
        # with a bare ModuleNotFoundError.
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        # Same pattern: wavr picks its sources at runtime from config.
        "wavr.app",
        "wavr.sources.simulated",
        "wavr.sources.network",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The heavy sensing extras stay OUT. They are optional by design (the base
    # install must not require them) and bundling torch would take this from
    # tens of megabytes to well over a gigabyte. A user who wants camera
    # inference installs the extra into a real Python; the sidecar covers the
    # default experience, which is network + Bluetooth presence.
    excludes=[
        "torch", "torchvision", "cv2", "ultralytics", "matplotlib",
        "PIL", "scipy", "pandas", "tkinter", "test", "unittest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="wavr-core",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX-packed binaries trip antivirus heuristics
    runtime_tmpdir=None,
    console=True,       # the shell captures stdout for its own log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
