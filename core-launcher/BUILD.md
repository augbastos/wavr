# Building `core-launcher`

Two APKs come out of this one module, chosen by a single Gradle property. Same
`applicationId` (`dev.wavr.core`), same code, different answer to "does this
phone run a Wavr Core, or only render one?"

| | command | size | what it is |
|---|---|---|---|
| **Kiosk** (default) | `./gradlew assembleDebug` | ~4.3 MB | Full-screen WebView pointed at a Core that runs somewhere else. What shipped before ADR-0010. |
| **Core** | `./gradlew assembleDebug -PwavrPython=true` | ~39 MB | The same app plus CPython 3.13 and the whole `backend/wavr` package, supervised by a foreground service. |

The default is the kiosk deliberately: the Core build needs a host CPython on the
build machine and is nine times the size, so it is a choice someone makes rather
than a thing that happens to them. See
[ADR-0010](../docs/adr/0010-android-core-runtime.md) for why the Core is embedded
CPython and not a Kotlin rewrite or a Termux container.

## Prerequisites

Both builds:

- **JDK 17** (`JAVA_HOME` set). Verified with Temurin 17.0.20.
- **Android SDK** with `platforms;android-35` and `build-tools;35.0.0`. Path goes
  in `local.properties` (git-ignored, machine-local):
  ```properties
  sdk.dir=C\:\\Users\\you\\AppData\\Local\\Android\\Sdk
  ```
- Gradle 8.14.3 comes from the wrapper. AGP 8.13.0 and Kotlin 2.2.0 are pinned.
- **No NDK required.** Chaquopy ships prebuilt native libraries; nothing in this
  project compiles C.

The Core build additionally needs:

- **CPython 3.13 on the build machine**, matching the APK's interpreter, for
  Chaquopy's `pip` step. Not 3.14 — Chaquopy's package repository tops out at
  `cp313` for numpy, so 3.14 would trade a working numpy for a version number.
  Auto-detected via `python3.13` / `python3` / `py -3.13`; if that fails, point at
  it:
  ```
  ./gradlew assembleDebug -PwavrPython=true \
      -PwavrBuildPython="C:/path/to/python3.13.exe"
  ```
  A `uv`-managed interpreter works:
  `uv python install 3.13`, then
  `~/AppData/Roaming/uv/python/cpython-3.13-windows-x86_64-none/python.exe`.
- **Network on the first Core build.** Chaquopy downloads wheels from PyPI and
  from `chaquo.com/pypi-13.1`, then caches them. Every version is pinned exactly
  in `app/build.gradle`, so the download is reproducible and subsequent builds are
  offline.

## What the Core build packages

- `backend/wavr/**` → the APK's Python source set, staged by `stageWavrPython`
  (a `Sync`, so a deleted file disappears from the APK too). `tests/` and
  `__pycache__` are excluded. 128 modules — the whole Core, not a subset.
- `frontend/**` → `assets/frontend`, staged by `stageWavrFrontend`, extracted at
  runtime by `AssetStaging.kt` into `filesDir/frontend` and pointed at with
  `WAVR_FRONTEND`.
- `app/src/main/python/wavr_android.py` → the only new Python: applies an
  environment, starts uvicorn, and forwards a manifest to
  `capabilities.recommend()`.
- CPython 3.13 + fastapi / starlette / pydantic / uvicorn / websockets / numpy /
  cryptography, **arm64-v8a only**.

Verify a build actually carries them:

```bash
unzip -l app/build/outputs/apk/debug/app-debug.apk | grep -E "libpython|assets/frontend/index.html"
```

## Installing

```bash
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

The two variants share an `applicationId`, so installing one **replaces** the
other. Debug signing keys differ from release ones; there is no release keystore
in this repo and there never will be (`*.jks`, `*.keystore`, `key.properties` are
git-ignored).

## First run is unconfigured, and that is the point

A fresh install behaves exactly like the old kiosk: it renders
`https://localhost:8000/?core`, starts no service, and boots no interpreter.
Nothing becomes a Core until the operator says so through the panel
(`WavrNative.setRoles(["core","node","client"])`).

This is what keeps the field device safe. That device runs its Core inside a
Termux `proot-distro debian` on port 8000; if this app decided on its own to
become a Core it would fight the running one for that socket.

## What you cannot verify on a build machine

A green Gradle build proves the code compiles and the APK carries what it should.
It proves nothing about the Core actually running. The following need the
physical phone (see ADR-0010):

- CPython boot time and first-request latency on a mid-range SoC.
- Whether the foreground service survives Doze overnight, with and without the
  battery-optimisation exemption.
- Thermal behaviour in a stand: does `THERMAL_STATUS_MODERATE` ever fire, and does
  shedding the camera actually bring it back down.
- `BOOT_COMPLETED` recovery and the 15-minute `JobScheduler` watchdog.
- CameraX bound to the *service* lifecycle with the screen off.
- Real memory footprint of a CPython 3.13 + numpy Core in an Android process.
