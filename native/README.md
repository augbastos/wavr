# native/ — the portable C++ runtime

A second implementation of the parts of Wavr that small, closed and client
devices need, in C++17 with no runtime dependencies:

- **`wavr`**, one executable (static where the platform allows): the CLI, and
  the **Node role** -- enrol with a Core, pin its certificate, stream LD2450
  frames, obey disable/reactivate/revoke -- speaking Node Protocol v1.
- **`libwavr_native`**, a stable C ABI (`include/wavr/wavr.h`, version 1.2) that
  the Android (JNI), Apple (Swift) and desktop (Rust) clients bind to instead of
  re-implementing Wavr semantics. On Android it ships as `libwavr_native_jni.so`.

It is **not** a port of the Core. Fusion, storage, the API and every provider
stay in Python, which remains the canonical definition of every behaviour.

## Why it cannot drift

Each semantic function here has a canonical Python definition, named beside it
in `src/semantics.h` / `src/client_view.h`. `scripts/gen_conformance.py` runs
that Python and writes the answer key to `conformance/*.json`;
`tests/conformance.cpp` checks this library against it through the C ABI, so
the boundary other languages call is the thing under test.
`backend/tests/test_conformance_fixtures_are_current.py` fails when the Python
changes and the fixtures were not regenerated. Covered today: `wavr status`
rendering and exit codes, compute tiers, the vocabulary, LD2450 framing, the
heartbeat state machine, the loopback rule, the client snapshot, and the
client command contract (the command table itself is the Python's, embedded as
the generated `src/client_commands_table.inc`).

The certificate fingerprint format is shared outright with the ESP32 firmware
(`firmware/wavr_node/src/fingerprint_fmt.cpp`).

## The C ABI (1.2)

- `wavr_abi_compatible(major, minor)` first, with the lowest version whose
  functions the caller uses (a client that only reads snapshots asks for 1.1).
  MINOR grows when functions are added; MAJOR changes when any existing one does.
- 1.1: the snapshot (`wavr_snapshot_*`). 1.2: commands and replies --
  `wavr_command_run` (one row of `backend/wavr/client_commands.py`),
  `wavr_probe` (the certificate a Core presents, for trust on first use),
  `wavr_reply_*`, `wavr_command_table`. A network call cannot be repeated just to
  size a buffer, so its result is a handle (`wavr_reply`) read like a snapshot.
- Plain C types; stateful things are opaque handles with create/free
  (`wavr_framer`, `wavr_snapshot`, `wavr_reply`); a handle is used by one thread
  at a time; everything else is reentrant.
- Every output goes into a caller-owned buffer; the return value is the size
  needed (call with `NULL, 0` to size). Nothing is allocated for the caller.
- **No C++ exception crosses the boundary**: every entry point is guarded, and
  a failure inside is `WAVR_ERR_INTERNAL` (or NULL from a constructor).
- `tests/c_consumer.c` is an outside consumer -- plain C, the public header
  only, linked against the shared library -- run by `ctest` on every build.

## CLI

| Command | What | Exit codes |
|---|---|---|
| `wavr status [--url] [--token] [--pin] [--json] [-q] [--ascii]` | Is Wavr running, does anything need you -- same rendering and codes as `python -m wavr.status` | 0 nothing needs you · 1 something does, or could not be checked · 2 no answer |
| `wavr snapshot [--url] [--token] [--pin]` | The native client view model as JSON (`docs/NATIVE-CLIENT.md`) | same as `status` |
| `wavr command NAME [--args JSON] [--url] [--token] [--pin] [--reveal]` | Run one command of the client command contract (`wavr command --list` prints the table). A credential the Core hands out is printed as `***` unless `--reveal` | 0 done · 1 the Core refused it · 2 no answer · 64 malformed call |
| `wavr probe --url https://CORE:PORT` | The certificate fingerprint a Core presents, unverified -- compare it with the Core's own screen before trusting it | 0 a certificate was seen · 2 none |
| `wavr doctor [--url] [--token] [--pin]` | The Core's diagnostic report (as `python -m wavr.doctor`) | 0 printed · 2 no answer · 3 answered without a report |
| `wavr capabilities` | This device's capability manifest | 0 |
| `wavr node enroll --url https://CORE:PORT --code CODE [--state F]` | Enrol, pinning the certificate the Core presents | 0 · 1 refused · 64 usage |
| `wavr node run --sensor ld2450:PORT\|replay:FILE\|stdin [--state F] [--seconds N]` | Run the Node | 0 · 3 revoked (state erased) · 64 usage |
| `wavr node reactivate [--state F]` | The node-initiated way back from disabled | 0 · 1 refused |
| `wavr node status [--state F]` | What the node knows (never the token) | 0 · 64 |
| `wavr version` | Runtime and C ABI version | 0 |

Unpinned requests are allowed to a loopback Core only; plain HTTP likewise.
`--sensor stdin` reads raw LD2450 bytes from any program that can reach the
radar (a serial bridge, `nc`); they are framed here and parsed only by the Core.
Tuning for tests and slow links: `--telemetry-ms` (floor 50),
`--heartbeat-ms` and `--disabled-heartbeat-ms` (floor 100).

The Node keeps its bearer token in its state file (`--state`, default
`wavr-node.json` in the working directory): owner-only (0600) from birth and
replaced atomically on Linux, Android and macOS; on Windows it inherits the
directory's permissions, so keep it in a per-user directory. Repeated failures
are logged once and then summarised at most once a minute.

## Build

Dependencies are fetched at configure time, pinned by version and SHA-256:
mbedTLS 3.6.7 LTS (Apache-2.0) and nlohmann/json 3.12.0 (MIT). Offline:
`-DWAVR_MBEDTLS_TARBALL=<path>` and `-DWAVR_JSON_HPP=<path>`.

```sh
# This machine (GCC, Clang or MinGW)
cmake -S native -B build/native -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build/native && ctest --test-dir build/native --output-on-failure

# Any other target, from any host, with Zig as the cross compiler
cmake -S native -B build/native-aarch64 -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_TOOLCHAIN_FILE="$PWD/native/cmake/zig-toolchain.cmake" \
  -DZIG=/path/to/zig -DZIG_TARGET=aarch64-linux-musl

# Android (bionic): the JNI library for the app, and the CLI
cmake -S native -B build/android-arm64 -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_TOOLCHAIN_FILE=$ANDROID_NDK/build/cmake/android.toolchain.cmake \
  -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-26
# then copy libwavr_native_jni.so to core-launcher/app/src/main/jniLibs/arm64-v8a/
```

Zig's Linux targets and MinGW link statically (one file, no runtime to match;
the Windows DLL needs only system DLLs). A glibc host build stays dynamic,
because a static glibc binary still loads NSS at run time. Release builds are
stripped and carry no build-machine paths (`-ffile-prefix-map`).

## Test it

```sh
ctest --test-dir build/native                                  # answer key + C consumer
python native/tests/e2e_node.py --wavr build/native/wavr       # Node lifecycle vs a real Core
python native/tests/e2e_node.py --wavr build/aarch64/wavr --adb SERIAL   # the same, on a phone
python native/tests/soak_node.py --wavr build/native/wavr --minutes 10   # chaos soak
python native/tests/e2e_commands.py --wavr build/native/wavr   # command contract vs a real Core, across the LAN
```

`e2e_commands.py` starts a throwaway multidevice Core on this machine's LAN
address and plays both sides: the Core's own screen (loopback) and a new device
(the LAN address). It checks the certificate probe, approve-on-the-Core pairing
(a wrong code is refused), that the device token works over pinned TLS and a
wrong pin stops the request before the token is sent, that a `user` cannot
switch Watch or approve anything while a `central` can switch Watch, that a
path-shaped argument stays one path segment, that no LAN device can approve a
sensor Node, and that a revoked token reads nothing.

`soak_node.py` runs a fake Core that cycles through garbage bodies, HTTP 500,
stale-sequence 409s, stalls longer than the node's timeout, the Core gone, a
different certificate, disable, and finally revocation -- and fails if a
request ever reaches a server with the wrong certificate, if telemetry is sent
while the node knows it is disabled, if the node does not exit 3 on revocation,
or if memory, handles or threads grow.

### Fuzzing

`fuzz/targets.cpp` holds one libFuzzer target per boundary that reads bytes the
runtime did not produce: HTTP responses, the snapshot inputs, `wavr status`
inputs, LD2450 bytes, the node state file, URLs, heartbeat answers. Each checks
invariants beyond "does not crash" (valid JSON of the fixed schema out; no
positions or identities through the view model; a network error never revokes).

```sh
# clang: real libFuzzer
cmake -S native -B build/fuzz -DWAVR_FUZZ=ON -DWAVR_LIBFUZZER=ON -DWAVR_SANITIZE=ON -DCMAKE_BUILD_TYPE=Debug
# GCC/MinGW: the seeded mutation driver in fuzz/driver.cpp (not coverage-guided)
cmake -S native -B build/fuzz -DWAVR_FUZZ=ON -DWAVR_SANITIZE=ON -DCMAKE_BUILD_TYPE=Debug
python scripts/gen_fuzz_corpus.py corpus        # seeds from conformance/
./build/fuzz/fuzz_snapshot --seconds 60 corpus/fuzz_snapshot
```

## Packaging (local only)

`scripts/package_native.py --out dist TARGET=BUILD_DIR ...` writes one archive
per target with the binaries, the header, the licence, third-party notices, a
`manifest.json` (version, ABI, file hashes, build command), a CycloneDX SBOM,
and `SHA256SUMS` -- after scanning every file for build paths, user names and
private keys, and refusing to package if one is found. Nothing is uploaded.

See `docs/PLATFORMS.md` and `docs/platform-matrix.json` for what has been
built, run and tested where.
