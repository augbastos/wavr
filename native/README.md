# native/ — the portable C++ runtime

A second implementation of the parts of Wavr that small and closed devices need,
in C++17 with no runtime dependencies:

- **`wavr`**, one static executable: `status` (the same answer as
  `python -m wavr.status`), `capabilities` (this device's manifest), and the
  **Node role** — enrol with a Core, pin its certificate, stream LD2450 frames,
  obey disable/reactivate/revoke — speaking the current Node Protocol v1.
- **`libwavr_native`**, a stable C ABI (`include/wavr/wavr.h`) that a Kotlin,
  Swift or C# client binds to instead of re-implementing Wavr semantics.

It is **not** a port of the Core. Fusion, storage, the API and every provider
stay in Python, which remains the canonical definition of every behaviour.

## Why it cannot drift

Each semantic function here has a canonical Python definition, named beside it
in `src/semantics.h`. `scripts/gen_conformance.py` runs that Python and writes
the answer key to `conformance/*.json`; `tests/conformance.cpp` checks this
library against it, through the C ABI wherever one exists, so the boundary other
languages call is the thing under test. `backend/tests/test_conformance_fixtures_are_current.py`
fails when the Python changes and the fixtures were not regenerated.

The one piece of logic shared outright rather than re-implemented is the
certificate fingerprint format, compiled from `firmware/wavr_node/src/fingerprint_fmt.cpp`
so the ESP32 node and this runtime print the same string an operator compares by eye.

## Build

Dependencies are fetched at configure time, pinned by version and SHA-256:
mbedTLS 3.6.7 LTS (Apache-2.0) and nlohmann/json 3.12.0 (MIT). Offline:
`-DWAVR_MBEDTLS_TARBALL=<path>` and `-DWAVR_JSON_HPP=<path>`.

```sh
# This machine (GCC or Clang)
cmake -S native -B build/native -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build/native && ctest --test-dir build/native --output-on-failure

# Any other target, from any host, with Zig as the cross compiler
cmake -S native -B build/native-aarch64 -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_TOOLCHAIN_FILE=native/cmake/zig-toolchain.cmake \
  -DZIG=/path/to/zig -DZIG_TARGET=aarch64-linux-musl
```

The Zig Linux targets link statically against musl: one file, no libc to match,
and the same file runs on an Android device's shell. A host build against glibc
stays dynamic, because a static glibc binary still loads NSS at run time.

Run the answer key on another device by passing the fixture directory:
`wavr_conformance /path/to/conformance`.

The Node keeps its bearer token in its state file (`--state`, default
`wavr-node.json` in the working directory). On Linux, Android and macOS it is
written owner-only (0600) and replaced atomically; on Windows it inherits the
directory's permissions, so keep it in a per-user directory.

## Verify against a real Core

```sh
python native/tests/e2e_node.py --wavr build/native/wavr            # this machine
python native/tests/e2e_node.py --wavr build/aarch64/wavr --adb SERIAL   # an Android phone
```

Starts a multidevice Core on loopback in a throwaway directory and drives the
binary through enrol → telemetry into fusion → a wrong certificate pin →
disable → reactivate → revoke, checking each step on the Core's side, with a
control before the first frame. With `--adb` the phone reaches the Core through
`adb reverse` on one port; nothing is exposed on the network.

## What has been checked, and how

Status words are the ones `docs/PLATFORMS.md` defines. "Builds" proves the code
is portable to a target; it proves nothing about running there.

| Target | Build | Conformance | Node e2e vs real Core |
|---|---|---|---|
| Windows x86_64 (MinGW-w64) | yes | passes | passes |
| Linux x86_64 (musl, static) | yes | passes (WSL2) | not run |
| Android arm64 (aarch64 musl, static) | yes | passes on a phone | passes on a phone |
| Android / Linux armv7 (musl, static) | yes | passes on a phone (32-bit userspace) | not run |
| Linux x86 32-bit (musl, static) | yes | passes (WSL2) | not run |
| Linux / OpenWrt MIPS, little- and big-endian (musl, static) | yes | not run (no device, no emulator) | not run |
| Linux x86_64, glibc + GCC (dynamic) | yes | passes (WSL2), also under ASan + UBSan | not run |
| Windows ARM64 (Zig) | yes | not run (no device) | not run |
| macOS arm64 / x86_64 (Zig) | yes | not run (no Mac) | not run |

Found and fixed by running on real devices rather than by reading: a 32-bit
build reporting 2 of 8 CPUs (musl counts the process's affinity; the manifest
now counts the machine's online CPUs, as Python does), and a phone reported as
having **no** battery because SELinux refuses every read under
`/sys/class/power_supply` — fixed in the Python as well, since it had the same bug.

## Cost

`benchmarks/native_footprint.py` measures it; results describe one machine.
On the development machine: a stripped Windows binary of about 2.5 MB (about
1.6 MB for Linux arm64/x86_64), `wavr status` answering in tens of milliseconds
where the Python CLI takes hundreds, and the Node role holding about 6 MB of RSS
at well under 1% of one core while streaming.
