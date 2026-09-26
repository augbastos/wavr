# CI tiers and cost model

GitHub Actions are the release gate. Prepare
a temporary `release/vX.Y.Z` branch and open a pull request into `main` before
tagging. A `release/*` PR runs every job in `tests.yml`, `native.yml` and
`clients.yml`, even when its diff does not touch a platform's files;
`docker.yml`, `firmware.yml` and `install-matrix.yml` keep their path filters
and run only when their files change. The tag-triggered `release.yml` then builds
packages and creates a draft release for human review. A manual dispatch can
exercise the native and client tiers without a release PR.

## What runs when

| Tier | Trigger | Checks |
| --- | --- | --- |
| Fast | Every PR and push to `main` | Python suite, generated conformance and design files, publication gate, companion and SDK tests, native host build and CTest. Browser tests run when their observable inputs change, and always on `release/*` PRs. |
| Platform | PR changing `native/`, `clients/`, `core-launcher/`, or `conformance/`; every `release/*` PR; manual dispatch | Android debug assembly and JVM unit tests, Slint desktop Cargo tests on Linux and Windows, native Zig cross builds and Android NDK builds. A diff that cannot be calculated runs these checks. |
| Heavy | `release/*` PR or manual dispatch | Native ASan/UBSan, libFuzzer, and the guarantee mutation checks. |
| Packaging | `v*` tag (or manual dispatch for a dry build) | Python distribution and Windows installer build/smoke; only a tag can create a draft release. |

Docker, firmware and installer-matrix workflows retain their own path filters.
SCPE and its seal run on PRs. The maintenance auto-merge workflow has its own
branch and integrity gates; a `release/*` branch is outside its accepted branch
pattern. The release PR should be reviewed with all applicable workflow results,
not only the `tests` check.

The platform workflows use an unfiltered PR event and a fail-open changed-file
job because GitHub's event-level `paths` filter cannot express "matching files
OR a head branch named `release/*`". Main pushes run the fast tier; a release
PR validates the broader matrix before merge.

## Cost model

**Estimate from step counts, not billed minutes.** These are rough runner-time
ranges for a warm cache and ordinary PR, not measurements or a guarantee. Cache
misses, package downloads, Chromium, Gradle, Zig, QEMU and installer work can
substantially extend them. Actions minutes are free on a public repository;
the gate is still kept small for queue time and useful signal.
Measure after the first run with `gh run view --json jobs`.

| Job | Runner OS | What it proves | Expensive step | Triggers | Estimate from steps (runner min) |
| --- | --- | --- | --- | --- | --- |
| `tests/what-changed` | Linux | Browser selection | Full checkout | PR, main, manual | 1–2 |
| `tests/test` | Linux | Python, generated files, publication, companion | Python suite | PR, main, manual | 4–10 |
| `tests/browser` | Linux | Dashboard, 3D, links, offline Chromium | Browser install and tests | Relevant change, release PR | 8–15 |
| `tests/guarantees` | Linux | Guards fail under mutation | Repeated pytest | Every PR and push | 5–15 |
| `tests/sdk-python` | Linux | Independent Python SDK | pytest setup | PR, main, manual | 1–3 |
| `tests/sdk-javascript` | Linux | Dependency-free JS SDK | Node setup | PR, main, manual | 1–2 |
| `tests/sdk-kotlin` | Linux | Android JVM unit tests (a required check on `main`) | Gradle/Android compile | PR, main, manual | 5–12 |
| `native/changes` | Linux | Platform diff decision | Full checkout | PR, manual | 1–2 |
| `native/host` | Linux | C++ host build, answer key, C consumer | CMake dependency fetch/build | PR, main, manual | 3–8 |
| `native/cross` | Linux | Eight Zig target builds | Zig download and cross compilation | Relevant PR, release PR, manual | 8–20 |
| `native/android` | Linux | NDK JNI and CLI compilation | Two ABI builds | Relevant PR, release PR, manual | 4–10 |
| `native/sanitizers` | Linux | ASan/UBSan runtime tests | Instrumented build | Release PR, manual | 4–10 |
| `native/fuzz` | Linux | Seven parser fuzz targets | Build and 30 seconds per target | Release PR, manual | 5–12 |
| `clients/changes` | Linux | Platform diff decision | Full checkout | PR, manual | 1–2 |
| `clients/android` | Linux | Kiosk assembly and JVM tests | Gradle/Android compile | Relevant PR, release PR, manual | 5–15 |
| `clients/desktop-linux` | Linux | Slint Cargo tests | Rust dependency compile | Relevant PR, release PR, manual | 5–15 |
| `clients/desktop-windows` | Windows | Slint Cargo tests on Windows | Rust dependency compile | Relevant PR, release PR, manual | 6–18 |
| `docker/build-and-smoke` | Linux | Image, dashboard, persistence, non-root | Docker build | Docker inputs | 4–10 |
| `firmware/build` | Linux | ESP32 variants and native logic | PlatformIO toolchain | Firmware inputs | 5–15 |
| `install-matrix/shellcheck` | Linux | Installer shell syntax | Package setup | Installer/backend inputs | 1–3 |
| `install-matrix/install` (five legs) | Linux | Install/start/uninstall per distro and arm64 | Package install, QEMU on arm64 | Installer/backend inputs | 4–12 per leg |
| `scpe/verify` | Reusable workflow | Disclosure check | External workflow | PR | 1–5 (external steps unknown) |
| `scpe-seal/seal` | Reusable workflow | Trusted disclosure seal | External workflow | SCPE completion | 1–5 (external steps unknown) |
| `jules-automerge/merge` | Linux | Maintenance PR integrity decision | API checks | Tests completion | 1–2 |
| `release/verify` | Linux | Suite on tag | Python suite | Tag, manual | 4–10 |
| `release/python-dist` | Linux | Wheel/sdist and checksums | Package build | After verify | 1–3 |
| `release/windows-desktop` | Windows | Core sidecar and unsigned installers | PyInstaller and Tauri build | After verify | 10–30 |
| `release/windows-desktop-smoke` | Windows | MSI/NSIS install and smoke | Installer round trip | After Windows build | 5–15 |
| `release/draft-release` | Linux | Draft artifact assembly | Artifact transfer | Tag after all builds | 1–3 |

No macOS runner is used. A successful cross compilation is a compile check,
not evidence that the binary ran on the target device.
