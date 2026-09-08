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

# Refuse to build a Core that cannot be found.
#
# `hiddenimports` only tells PyInstaller to LOOK for a module; if the build
# environment does not have it, the module is simply absent from the result and
# the build still succeeds. That is how a Core shipped with no mDNS at all: the
# import is lazy, the extra was not installed, and nothing anywhere said so
# until a phone spent a minute failing to find a hub that was never
# advertising.
#
# An installer is not the place to discover a missing dependency. This is.
for _needed, _why in (
    ("zeroconf", "the phone app's 'Find your Wavr hub' browses _wavr._tcp; "
                 "without this the Core never advertises and no device can "
                 "discover it. Install it: pip install -e backend[mdns]"),
    ("ifaddr", "zeroconf enumerates interfaces through it"),
):
    try:
        __import__(_needed)
    except ImportError as _exc:      # noqa: PERF203 -- three items, once, at build time
        raise SystemExit(
            f"\nwavr-core.spec: refusing to build without {_needed!r}.\n"
            f"  Why it matters: {_why}\n"
            f"  ({_exc})\n"
        )

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
        # mDNS. Imported lazily inside a function in `wavr/mdns_peers.py`
        # (`from zeroconf import ServiceInfo  # lazy: real path only`), so
        # static analysis never sees it and it was simply absent from the
        # frozen Core -- `grep zeroconf` over the shipped binary returned zero,
        # next to 41 hits for bleak and 66 for cryptography.
        #
        # It is declared in pyproject as an optional extra, `mdns`, and for the
        # library that is the right call. For the PRODUCT it is not: the phone
        # app's first screen after install is "Find your Wavr hub", browsing
        # `_wavr._tcp` -- the same service `mdns_peers.py` advertises. With the
        # module missing, the Core never advertises, so that screen can never
        # succeed for anybody who installs Wavr rather than running it from a
        # checkout. The first user hit it, tried it with his VPN off to be
        # sure, and reported "nao achou". The Core had been logging
        # `ModuleNotFoundError: No module named 'zeroconf'` at every start,
        # inside a traceback nobody reads.
        #
        # `ifaddr` is zeroconf's own way of enumerating interfaces and is
        # imported the same indirect way.
        "zeroconf",
        "ifaddr",
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
