# Platforms — what runs where, and how we know

The support matrix in prose. The machine-readable version, with compile / unit /
simulation / hardware / UI evidence kept apart per target, is
[`platform-matrix.json`](platform-matrix.json). Every cell says how it was
established; a cell nobody has checked says so. The Core's install routes and their
CI are in [INSTALL.md](INSTALL.md).

## Status words

| Word | Means |
|---|---|
| **VERIFIED** | Ran on that platform and passed its checks. The note says where: real hardware, a CI virtual machine, or WSL2. |
| **BUILDS** | Compiles and links for that target. Proves portability, **not** that it runs. |
| **SIMULATED** | Ran under emulation (QEMU) or against test doubles only. |
| **VENDOR-GATED** | Needs a vendor programme, contract, fee or hardware the project does not have. |
| **UNSUPPORTED** | No viable path, or deliberately out of scope. |

The three roles are ADR-0009's: **Core** (the authoritative runtime — Python),
**Node** (a sensor that reports to a Core — the ESP32 firmware or the native
`wavr` runtime), **Client** (a screen a person looks at).

For native Node installation and hardware checks, see [Node Lite deployment](deploy/node-lite.md).

## Matrix

| Platform | Core (Python) | Node (native `wavr` / firmware) | Client today | Native client |
|---|---|---|---|---|
| Windows x86_64 | VERIFIED — full test suite, development machine | VERIFIED — conformance + C consumer + Node e2e vs a real Core + 10-min chaos soak | Tauri WebView; browser | VERIFIED headless — `clients/desktop` (Slint) compiles and tests against the real DLL; Manage tab; a headless render test paints the status; never shown on a physical screen |
| Windows ARM64 | not checked | BUILDS (Zig) | Tauri WebView (not checked on ARM64) | — |
| Linux x86_64 (Debian, Ubuntu, Fedora, Alpine) | VERIFIED — `install-matrix` CI VMs; Docker image in CI | VERIFIED — conformance under WSL2 (static musl binary) | browser | VERIFIED headless under WSL2 — `clients/desktop` compiles and its tests pass, incl. against the real library and the headless render test |
| Linux arm64 / Raspberry Pi 64-bit | SIMULATED — Debian arm64 under QEMU in CI; never on a Pi | BUILDS; the same binary is VERIFIED on an Android arm64 kernel | browser | — |
| Linux armv7 / Pi 2–3 32-bit | not checked | BUILDS; the same binary is VERIFIED on a phone's 32-bit userspace | browser | — |
| Linux x86 32-bit (old PCs) | not checked | VERIFIED — conformance under WSL2 (32-bit static binary) | browser | — |
| macOS arm64 / x86_64 | not checked (no Mac) | BUILDS (Zig); serial port not implemented | browser | SwiftUI sources in `clients/apple`, never built (needs macOS/Xcode) |
| Android phone arm64 | BUILDS — embedded-CPython APK (ADR-0010), never run on a device; the Core launcher has run on a phone with the Core in a container | VERIFIED — conformance + C consumer (NDK bionic) and Node e2e (musl) on a real phone | Capacitor WebView companion; WebView kiosk | VERIFIED on an emulator — Compose activity in `core-launcher`; unit-tested (parser vs the answer key); `native/tests/android_ui_smoke.py` against a real Core, incl. writes (Watch, approve by code, join); not on a physical phone |
| Android armv7 (32-bit userspace) | not checked | VERIFIED — conformance + C consumer (NDK bionic) on a real phone's 32-bit userspace | as above | BUILDS — same APK |
| Android TV / Google TV | not a Core | BUILDS (same arm64/armv7 libraries; not run on a TV) | none | VERIFIED on an emulator — `TvStatusActivity` renders and D-pad moves focus (TV geometry); no TV hardware |
| Fire TV (Fire OS) | not a Core | BUILDS (as Android) | none | — |
| Fire TV (Vega OS) | — | — | — | UNSUPPORTED — React Native or web only |
| iPhone / iPad | UNSUPPORTED — no background runtime to keep alive (`_can_be_core`) | — | browser | SwiftUI sources in `clients/apple`, never built (needs macOS/Xcode) |
| Apple TV (tvOS) | UNSUPPORTED | — | none — tvOS has no WebView | SwiftUI sources in `clients/apple`, never built |
| Steam Deck | as Linux x86_64 (not checked on a Deck) | as Linux x86_64 | browser | — |
| NAS (Synology/QNAP/TrueNAS) | VERIFIED for the Docker route in CI; no NAS model checked | BUILDS (static x86_64/arm64) | browser | — |
| OpenWrt router, MIPS little-endian (e.g. MT7621) | UNSUPPORTED — too small for the Core | BUILDS (Zig, `mipsel-linux-musleabi`) | — | UNSUPPORTED — no UI |
| OpenWrt router, MIPS big-endian (e.g. ath79) | UNSUPPORTED | BUILDS (Zig, `mips-linux-musleabi`) | — | UNSUPPORTED |
| OpenWrt router, ARM / x86 | UNSUPPORTED | BUILDS (the Linux musl binaries) | — | UNSUPPORTED |
| ESP32 | UNSUPPORTED (micro tier) | BUILDS — `pio run`, three envs; native unit test passes. No board flashed. | — | — |
| Samsung Tizen TV | — | — | — | VENDOR-GATED — partnership outside the US; native C/C++ path not found |
| LG webOS TV / Titan OS | — | — | — | UNSUPPORTED — web apps only |
| Roku | — | — | — | VENDOR-GATED — native NDK is partner-only; otherwise a BrightScript rewrite |
| Hisense VIDAA | — | — | — | VENDOR-GATED — partnership |
| Xbox | — | — | — | VENDOR-GATED — only as a separate UWP app via retail Dev Mode |
| PlayStation / Nintendo | — | — | — | VENDOR-GATED — NDA programmes aimed at games; not pursued |

"Not checked" means nobody has run it, in either direction.

## Device tiers

The tier is computed by `capabilities._compute_tier` (Python, canonical) and
by `native/` against the same answer key (`conformance/compute_tier.json`).
Unknown RAM degrades to LOW rather than guessing high.

| Tier | Rule | Can be a Core? | Suited to |
|---|---|---|---|
| HIGH | ≥ 7000 MB RAM and ≥ 4 CPUs | yes | Core with cameras (person detection) |
| MEDIUM | ≥ 1800 MB (a Pi with ≥ 900 MB is promoted here for its permanent power) | yes | Core without camera inference |
| LOW | ≥ 450 MB (`MIN_CORE_RAM_MB`), or RAM unknown | yes, just | Core with a few sources; better as a Node |
| MICRO | below 450 MB, or an ESP32 | no | Node only |

What a role costs, measured on the development machine
(`benchmarks/results/2026-09-23-*`; compare only with the same machine):

| Role | Install | Resident memory idle | CPU idle |
|---|---|---|---|
| Python Core, base install | 25.4 MB of packages (77.2 MB before numpy left the base) | ~79 MB RSS, ~64 MB private, 5 threads | ~0.2% of one core, also while sweeping the LAN |
| Native Node (`wavr node run`) | one 1.0–2.7 MB binary, by target | ~6–9 MB RSS | well under 1% of one core |

## How weak can a device be and still help?

Four profiles, in the existing tier vocabulary. Numbers are MEASURED on one PC or
a phone unless marked otherwise; the raw results for the native Node are not committed.

| Profile | Runs | Tier | Binary / install | Resident memory | CPU | Disk writes | Needs from the OS | Gives Wavr |
|---|---|---|---|---|---|---|---|---|
| **ESP32 Node** | `firmware/` | MICRO | one firmware image | on-chip SRAM | — | NVS: `press_count` on a button press | nothing (bare metal) | an LD2450 radar or PIR in a room — BUILDS only, no board flashed |
| **Native Node** | `wavr node run` | MICRO or any | 1.0–2.7 MB, one file, by target | ~6–9 MB RSS, flat over a 10-min chaos soak | ~0.3% of one core at 10 requests/s (accelerated soak); 0.05–0.3% at the default 1 s / 30 s cadence | the state file (< 1 KB) every 60 s and on a state change; error lines rate-limited | a Linux, Android, Windows or macOS kernel; TCP; a writable file; a serial port if it hosts a radar | radar evidence from any box with a USB port; `stdin` takes bytes from any program that reaches the sensor |
| **Core, base install** | `python -m wavr.serve` | LOW (≥ 450 MB) | 25.4 MB of packages + Python | ~79 MB RSS, ~64 MB private, 5 threads | ~0.2% of one core, sweeping the LAN included | none while rooms are steady | Python 3.11+ | fusion, API, dashboard, every non-camera provider |
| **Core with cameras** | + `[camera]` or `[camera-lite]` extra | HIGH | ~1 GB (`[camera]`, torch); ~213 MB (`[camera-lite]`, ONNX) | ~400 MB (torch) / ~125 MB (ONNX) per detector process | per frame ~47 ms, either path | none (frames are never persisted) | as above | person detection |

Degradation is explicit: a device below the Core's floor (`MIN_CORE_RAM_MB`,
450 MB) is offered the Node role, never a "lite" Core; a Node that loses its
Core keeps heartbeating and its sensor's evidence decays in fusion; a Node
whose certificate pin no longer matches refuses to send anything. The minimum
RAM for a whole device running the native Node is not measured -- the process
itself needs ~6 MB, and the OS decides the rest.

## Routers, NAS and embedded Linux

- **OpenWrt-class routers**: the Node only. The static MIPS (LE and BE),
  ARM and x86 binaries BUILD; none has run on a router. OpenWrt's own guidance
  for devices that install software is ≥ 16 MB flash and ≥ 128 MB RAM; the
  router binaries are 1.6–2.5 MB. A Core does not belong on a router.
- **NAS (Synology, QNAP, TrueNAS)**: the Core, through the Docker route (built
  and run in CI; no NAS model tried), or the Node as a static binary.
- **Small ARM boards, thin clients, old mini PCs**: the Core if RAM ≥ 450 MB
  (the base install), otherwise the Node. The arm64 and armv7 binaries have run
  on a phone's kernel, never on an SBC.
- **Yocto/Buildroot**: the static musl binaries need nothing but a Linux kernel.
- **Old PCs**: the Windows build needs only system DLLs (UCRT: Windows 10/11
  ship it; 7/8 need the UCRT update, not tried). A 32-bit x86 Linux build passes
  the answer key under WSL2.

## Capability manifest: who probes what

A key a probe did not actually determine is **absent** (unknown), never
`false` — permission denied is not absence. The Python Core probes more keys
than the native runtime; the native runtime omits what it cannot know.

| Key | Python (`capabilities.py`) | Native (`native/src/capabilities.cpp`) |
|---|---|---|
| `battery` / `permanent_power` | Windows power status; Linux sysfs — unknown if any supply is unreadable | same; Android and Apple: unknown |
| `serial_port_support` | pyserial importable | Windows and Linux (incl. Android): true by construction; Apple: unknown |
| `wifi`, `ethernet`, `ble`, `bluetooth`, `camera`, `gpu`, `docker`, `display`, `mdns`, `network_scan`, `raw_socket`, `gps`, `accelerometer` | probed (each may still answer unknown) | not probed (absent) |
| `mmwave`, `microphone`, `uwb`, `nfc`, `usb`, `wifi_csi` | always unknown — no probe that does not risk a false claim | absent |
| RAM, CPU count, platform, arch | probed | probed; CPU count reads the machine's online CPUs, not the process's affinity |
