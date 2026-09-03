# ADR-0010 — The Android Core runtime: embed CPython, do not re-implement the Core

- **Status:** Accepted — 2026-09-03. Partially built and measured (see "What was
  actually executed"); the on-device half is pending the Moto G Power.
- **Deciders:** Augusto (owner-operator)

## Context

`core-launcher/` already exists and is undocumented: a Kotlin kiosk
(`dev.wavr.core`, minSdk 26, targetSdk 35) that is a full-screen `WebView`
pointed at `https://localhost:8000/?core`, plus a `window.WavrNative` JS bridge,
a CameraX→loopback MJPEG source, and an `NsdManager` advertisement of
`_wavr._tcp` on port 8000.

Everything in that sentence is a **client**. The `:8000` it renders and the
`_wavr._tcp` it advertises are not its own — on the one device where this
actually runs (the the field device), they belong to a Python backend running
inside a `proot-distro debian` container under a Magisk-rooted Termux. Uninstall
Termux and the kiosk becomes a spinner advertising a service that does not
exist. The app claims to be a Core and is a browser.

So the question is not "how do we improve the launcher"; it is **what runs the
Core on Android**. Three candidates, and one of them we have already been
running for two months, which makes this the rare architecture decision with
field evidence instead of speculation.

The cost that dominates the decision: the Python Core is **28,709 lines of
runtime code** in `backend/wavr/` (105,272 including tests). It is also where
every invariant lives — `fusion.py`, the ADR-0002 privacy boundaries, `auth.py`,
`devices.py`, the pairing state machine, and the entire conformance surface of
`docs/WAVR-PROTOCOL.md`. Any answer that produces a *second* implementation of
that produces a second set of bugs and a second thing for the protocol spec to
mean.

## Decision

**Embed CPython in the APK with [Chaquopy](https://chaquo.com/chaquopy/) and run
the existing, unmodified `wavr` package inside a foreground service.** The
Android Core is the same Core; Kotlin is the host, the supervisor, and the
sensor driver — never a second brain.

1. **One Python Core, packaged, not ported.** Chaquopy 17.0.0 bundles CPython
   3.13 plus wheels into the APK. `backend/wavr` is added to the app's Python
   source set, so the APK carries the *same files* the desktop runs; there is no
   Android fork of `app.py`, no Android `fusion.py`, and `docs/WAVR-PROTOCOL.md`
   keeps exactly one conformant implementation.
2. **Kotlin owns everything Python cannot see or survive.** The split is by
   *capability*, not by preference:
   - **Lifecycle** — a foreground service (`CoreService`) that owns the
     interpreter, survives the screen going off, and is restarted by the system.
     CPython has no answer to Doze, process death, or `BOOT_COMPLETED`.
   - **Sensors** — CameraX, Bluetooth, Wi-Fi/thermal/battery state. `bleak` and
     `opencv` cannot reach Android's camera or radio stack; the platform APIs
     can. Kotlin drives the hardware and feeds the Core over loopback (the
     existing MJPEG pattern) or the Node ingest API.
   - **Capability truth** — only Android knows this box has a camera and BLE.
     Kotlin *produces* the manifest (`docs/WAVR-PROTOCOL.md` §5); Python still
     *decides* (`capabilities.recommend()`). See decision 6.
3. **`uvicorn`, not `uvicorn[standard]`.** `uvloop`, `httptools` and
   `watchfiles` have no Android wheels and never will without someone
   maintaining a Rust/C cross-build. The base `uvicorn` (asyncio + `h11`) and
   the `websockets` pure-Python implementation are enough for a home Core, and
   they are already what `wavr` declares. The `[camera]` extra (`torch`,
   `opencv`) is deliberately **not** enabled on Android in this phase even
   though Chaquopy's repository carries both — thermals, not availability, are
   the reason (decision 5).
4. **Loopback by default; LAN only when the operator says so.** The service
   starts the Core with `WAVR_BIND=127.0.0.1` and no `WAVR_MULTIDEVICE`, which
   is byte-identical to ADR-0002's default. Enabling LAN mode is one explicit
   operator switch that sets `WAVR_MULTIDEVICE=1`, `WAVR_BIND=0.0.0.0` and turns
   on the self-signed TLS path in `serve.py` — the same opt-in ADR-0006 already
   specifies, expressed as a toggle instead of a shell variable.
5. **Thermal budget is a first-class input, not an afterthought.** A Moto G
   Power in a stand with no airflow will throttle. `CoreService` registers a
   `PowerManager.OnThermalStatusChangedListener` (API 29+) and sheds load in
   defined steps: `MODERATE` halves the camera frame rate, `SEVERE` releases the
   camera entirely, `CRITICAL` additionally reports degradation to the panel.
   The Core keeps serving at every step — we degrade sensing, never the API.
6. **Roles are a runtime choice inside one APK, never a build variant.** A
   device may hold any combination of Core / Node / Client
   ([ADR-0009](0009-space-people-and-device-functions.md) axis 2). The chooser
   is: Kotlin builds a §5.2 manifest from real Android APIs → the Core's own
   `capabilities.recommend()` turns it into a proposal with reasons → the
   operator confirms or overrides. **`recommend()` is not re-implemented in
   Kotlin.** There is one recommendation engine and it lives in Python, so a
   phone and a laptop can never disagree about what a phone is for.
7. **Existing installs are not touched.** Roles start *unconfigured*, and an
   unconfigured app behaves exactly as it does today: client-only, WebView at
   `https://localhost:8000/?core`, no service, no interpreter. The G9's proot
   Core keeps working and the app does not race it for port 8000. Becoming a
   Core is an explicit act.
8. **The Core starts once per process; a restart is a process restart.** This
   was discovered by running it, not by reading it. `wavr.app` builds its stores
   as module-level singletons and closes them in its shutdown handler, so a
   second `start()` in the same interpreter brings uvicorn back up on top of
   closed SQLite handles — `Cannot operate on a closed database`, sensing sources
   crashing, and an HTTP surface that still answers, which is the worst of the
   available failure modes. `importlib.reload` across 128 modules is not a fix.
   So the contract is explicit on both sides: `wavr_android.start()` refuses
   after a stop and returns `restartRequired`, and `CoreService.requestRestart()`
   kills the process so Android rebuilds it. Any setting the running Core cannot
   adopt — LAN mode, the port — is saved and reported as `restartRequired`
   rather than silently ignored or silently applied.

### Zero backend changes, and the one dependency this has on `backend/`

Packaging exposed two places where the Core assumes it was checked out from git.
Neither needed a change from this work:

- **Frontend path.** `app.py` resolved the dashboard as
  `Path(__file__).parents[2]/"frontend"`, which does not exist inside an APK.
  It already honours a `WAVR_FRONTEND` override (`_find_frontend()`), added on
  this branch for the frozen/PyInstaller sidecar. The Android bootstrap sets
  that variable to the extracted asset directory and needs nothing further.
  **This is a hard dependency: without `_find_frontend()` the Android Core
  serves the API and 404s on `GET /`.**
- **Capability truth.** `capabilities.scan_host()` cannot see Android hardware —
  its probes shell out to `lsusb`/`hciconfig`, so a phone with a camera honestly
  reports `camera: null` under the honesty rule of §5.1. Rather than add a
  platform hook to `capabilities.py`, the Android bootstrap builds the manifest
  in Kotlin and calls `CapabilityManifest.from_dict()` + `recommend()` directly.
  Both are already public and already specified to take an *untrusted* manifest
  off the wire, so §5.2's bounded validation applies unchanged. `recommend()`
  stays the single recommendation engine and `backend/` stays untouched.

The Android Core therefore adds **no** Android-only code path to `backend/`, and
`backend/wavr` is packaged byte-identical to the checkout.

## Rejected alternatives

### Native Kotlin re-implementation of a reduced Core

Rejected on maintenance cost, and the cost is not close.

A "reduced" Core is still: the fusion loop, `SensingEvent`/`RoomState` and their
freshness/decay semantics, the device/token store with hashed-at-rest
credentials, the pairing state machine with its rate limiter, the Space/people/
device-function model, the Core registry and leadership rules, TLS cert
generation and fingerprinting, the WebSocket hub and ticket auth, SQLite schema
and its migrations, and the ~180 HTTP routes the frontend calls. That is not a
subset of `backend/wavr` — it is most of its load-bearing half, and a
conservative estimate is 10–15k lines of Kotlin.

The decisive objection is not the initial write; it is that **`fusion.py` would
then exist twice**. Fusion is the file the privacy story rests on and the file
Canon marks untouchable. Two implementations means the sentence "Wavr fuses
these signals this way" stops being true of Wavr and becomes true of two
different programs that agree until they do not. `docs/WAVR-PROTOCOL.md` would
have two conformance targets, and every future ADR would need an "…and the same
in Kotlin" clause.

Worth stating plainly, because it is the real temptation: a native Core would be
*better* on Android in isolation — smaller, faster to start, no interpreter,
no 34 MB APK, native Doze integration. It loses anyway, because Wavr's value is
one explainable brain, not a good phone app.

### Termux + proot-distro (what the G9 runs today)

Rejected as a *product*, kept as a *lab rig*. This is the option with real
evidence, and the evidence is what disqualifies it. To make the G9 Core come up
reliably has so far required, all of it documented from live incidents:

- Magisk root, with "Superuser access" set to **Apps and ADB** — a setting that
  can silently reset on reboot and, when it does, the Core does not start;
- three sideloaded F-Droid apps (Termux, Termux:Boot, Termux:API) whose
  signatures must match each other;
- disabling Android 16's **phantom process killer**
  (`settings put global settings_enable_monitor_phantom_procs false` plus
  `max_phantom_processes`), because otherwise the OS kills Termux's children
  ~70 s after boot;
- a permanent `termux-wake-lock` (measured held for 1 d 13 h, Doze permanently
  `ACTIVE`) to keep the backend alive with the screen off;
- hand-written boot and watchdog shell scripts on `/sdcard`, an atomic
  `mkdir` lock with stale-lock reclamation, and the rule **never
  `pkill -9 proot`** because it corrupts the namespace until a full reboot;
- deployment by copying files into a container rootfs — no package, no version,
  no signature, no rollback.

It also silently loses capability: `socket.AF_PACKET` does not exist in the
proot's Python, so the DHCP sensing source cannot work there at all.

None of that is a criticism of the rig — it is a genuinely good rig and it
proved the thesis that a phone can be a Core. But a product cannot ask a user to
root their phone and disable an OS process reaper, and "sideload three apps then
edit a shell script on your SD card" is not an install. It is also unsignable
and unupdatable: there is no artifact to sign and nothing to bump a version on.
Chaquopy produces an APK, which is a thing you can install, sign, version, and
uninstall cleanly.

## What was actually executed (2026-09-03, this machine)

Measured, not assumed:

- **Chaquopy 17.0.0 resolves on this exact toolchain** — AGP 8.13.0, Gradle
  8.14.3, JDK 17, compileSdk 35. `BUILD SUCCESSFUL`.
- **The base dependency set in `backend/pyproject.toml`, unmodified, installs
  for `arm64-v8a`.** Given the declared floors (`fastapi>=0.110`,
  `uvicorn>=0.29`, `python-dotenv>=1.0`, `websockets>=12`, `numpy>=1.24`) plus
  the `[tls]` extra (`cryptography>=42`), pip resolved:
  `fastapi 0.125.0 · pydantic 1.10.26 · starlette 0.50.0 · uvicorn 0.52.4 ·
  websockets 17.1 · numpy 1.26.2 · cryptography 42.0.8 · anyio 4.15.0`.
- **The `pydantic-core` problem solves itself, at a price.** Pydantic v2's
  `pydantic-core` is a Rust extension with **zero** Android wheels on PyPI
  (checked: 137 files in `pydantic-core 2.48.0`, none `android_*`) and it is
  absent from Chaquopy's native repository. pip therefore backtracks to the
  newest FastAPI that still admits pydantic v1 — pure Python, `py3-none-any` —
  and installs cleanly. The price is a **FastAPI ceiling at 0.125.0** on
  Android while the dev venv floats at 0.139.0. This is survivable *only*
  because Wavr defines **zero** pydantic models and uses **zero**
  `response_model` (verified by grep across `backend/wavr/`); FastAPI is used
  purely as a router with `Depends`. It is still real version skew and must be
  pinned and tested, not hoped about — see "Risks".
- **`numpy 1.26.2` is sufficient.** The entire backend's numpy surface is
  `array, asarray, all, cross, isfinite, linalg, ndarray` — 1.x API throughout,
  nothing added in numpy 2.
- **Both APKs build from this module.** `assembleDebug` → 4.3 MB kiosk, no
  Python, no new native libs, byte-for-byte the same behaviour as before.
  `assembleDebug -PwavrPython=true` → 39 MB Core, `arm64-v8a` only, carrying
  `libpython3.13.so`, **128 `wavr` modules** (exactly the count of `.py` files in
  `backend/wavr`, so the whole Core and not a subset), the `wavr_android`
  bootstrap, `assets/frontend/index.html`, and native extensions for numpy
  (`_multiarray_umath.so`), cryptography (`_rust.so`), cffi and OpenBLAS. Chaquopy
  byte-compiles every module during packaging, so the build would have failed on
  a syntax error in any of the 128 — it did not.
- **The bootstrap was exercised against a real Core**, on the exact resolved
  dependency set: `start()` → uvicorn up in 0.8 s → `GET /healthz` **200**
  `{"ok":true,"version":"0.3.0"}`, `GET /api/status` **200**, `GET /`
  **200, 1,106,963 bytes of dashboard** served out of `WAVR_FRONTEND`; SQLite
  created at the configured app-private path; `stop()` closes the socket.
- **`capabilities.recommend()` works on an Android manifest.** Fed the manifest
  `AndroidCapabilities.kt` produces for a Moto G Power, the *unmodified* engine
  returned `Portable Core + Node + Client`, `prefer_standby: true`, with its
  reasons — including "It runs on battery, so Wavr will treat it as a Core that
  can move or go to sleep rather than the always-on one." The §5.1 honesty rule
  survives the merge: keys Kotlin omitted (`mmwave`, `wifi_csi`) stayed `null`
  rather than being flipped to `false`.
- **APK size, measured:** 39 MB debug, `arm64-v8a` only — 23.9 MB Python stdlib +
  packages in `assets/chaquopy`, 10.3 MB native libs (`libpython3.13`, OpenSSL,
  SQLite, OpenBLAS), 0.65 MB dashboard. A second ABI roughly doubles the native
  half; we ship arm64 only.
- **Licence is clean.** Chaquopy is **MIT** (it dropped its commercial licence
  key in 2021), which composes into AGPL-3.0 without friction. The bundled
  CPython is PSF-2.0. No licence key, no runtime phone-home, nothing to declare.

### Two defects this found, both fixed

Neither would have been caught by reading the code, and one of them was a
privacy regression:

- **An empty environment value must be written, not deleted.** The bootstrap
  originally `pop`-ed empty variables, on the reasoning that removing an override
  is the conservative direction. It is the opposite: `wavr.config` calls
  `dotenv.load_dotenv()` at import, which fills in anything `os.environ` does not
  already define. Deleting `WAVR_MULTIDEVICE` handed the decision to a `.env`
  file — and the Core came up on **HTTPS with LAN mode ON when the caller had
  explicitly asked for loopback**. Writing `""` is both correct and dotenv-proof.
  ADR-0002 §1's default is only a default if nothing else can supply it.
- **The foreground service type has to be re-asserted when the camera opens.**
  Types are asserted by what is actually in use, never speculatively — but that
  means a `specialUse`-only service that later opens a camera is refused by
  Android 14+. The streamer now reports start/stop and the notification is
  re-posted with the new mask.

## Consequences

- **Positive — the duplication is zero.** A fix to `fusion.py` ships to Android
  by rebuilding, not by porting. The protocol keeps one implementation.
- **Positive — SQLite lands in app-private storage** (`filesDir/wavr.db`) by
  setting `WAVR_DB`, which is stricter than the proot Core's SD-card path and
  is wiped on uninstall. No `MODE_WORLD_*`, no external storage, no permission.
- **Positive — the Core becomes installable.** An APK is signable, versionable,
  and uninstallable; no root, no Termux, no shell scripts, no phantom-killer
  surgery.
- **Positive — a path to on-device CV exists.** Chaquopy's repository carries
  `torch`, `opencv-python`, `tflite-runtime` and `scikit-learn`. Not enabled
  now, but the `[camera]` extra is a thermal decision later, not a packaging
  dead end.
- **Trade-off — 39 MB.** Nine times the kiosk APK. Accepted: it is the price of
  not having two Cores, and it is a one-time install on a device the operator
  dedicated to this.
- **Trade-off — the battery exemption is load-bearing for crash recovery, not
  just for latency.** Since Android 12 an app may not start a foreground service
  from the background unless it is exempt from battery optimisation. `BootReceiver`
  is fine (BOOT_COMPLETED is its own exemption) and so is a start from the visible
  panel — but the 15-minute `JobScheduler` watchdog, the thing that resurrects a
  crashed Core while nobody is looking, **only works if the operator granted the
  exemption**. `CoreService.start()` therefore returns a boolean instead of void,
  the refusal is surfaced as `startRefused`/`watchdogCanRestart`, and
  `PowerPolicy.RATIONALE` says this in the operator's own words rather than
  letting them discover it after a silent overnight outage.
- **Trade-off — a second Python on the build machine.** Chaquopy's `pip` step
  needs a host CPython matching the app's (3.13). Contributors who only want the
  kiosk must not pay for that, so the Chaquopy wiring is behind a Gradle
  property (`-PwavrPython=true`) and the default build is unchanged.
- **Trade-off — Doze is now our problem.** Termux solved it with a permanent
  wake-lock. We ask the operator for a battery-optimisation exemption, with the
  reason on screen, and we never demand it: without the exemption the Core still
  runs, it just may be deferred while the screen is off, and the app says so.

## Risks, named

- **FastAPI version skew (highest), and it is MEASURED, not theoretical.** The
  whole backend suite was run against the exact Android-resolved set (fastapi
  0.125.0 / pydantic 1.10.26 / starlette 0.50.0 / numpy 1.26.2) and compared with
  a control on the project's own set. Everything matches except **two assertions
  in `tests/test_api_pair_requests.py`**, and the cause is precise:

  ```
  POST /api/pair-request  {"requester_name": 12345}
      pydantic v2 (desktop) -> 422    pydantic v1 (Android) -> 200
  POST /api/pair-request/status  {"request_id": 123}
      pydantic v2 (desktop) -> 422    pydantic v1 (Android) -> 200
  ```

  Pydantic v1 coerces an int to a str; v2 refuses. So the Android Core is
  measurably **more permissive at its request-validation boundary than the
  desktop Core, on the pairing surface**. It is not a privilege escalation —
  nothing is authorised that was not before, the value simply arrives as
  `"12345"` — but "the same Core everywhere" is this ADR's central claim, and
  here is the one place it is not literally true. That has to be stated, not
  buried.

  Mitigations, in order of honesty: (1) pin the *whole project* to the
  Android-resolvable set so a FastAPI bump that breaks Android fails on the
  desktop first; (2) tighten those two handlers to validate the type themselves
  instead of relying on the framework, which removes the divergence at the source
  and is a small change; (3) the durable fix — delete the dependency. Wavr has
  zero pydantic models, so a Starlette-only routing layer removes pydantic
  entirely; 129 `Depends(` sites and 49 `APIRouter`s make that a project rather
  than a patch, but it is the direction, and it benefits every platform.

  Two other suite failures were investigated and are **not** attributable to this
  decision: `tests/test_netutils.py` hangs inside `asyncio/windows_events.py` on
  Python 3.12 (reproduced identically with the project's own dependency set, and
  that module does not exist on Android), and `test_sources_concurrency.py`
  fails the same way on both sets — a wall-clock assertion plus a repo-local
  `.env` leaking into the default source list.

- **The Kotlin manifest is device-controlled input.** It is written by our own
  code into app-private storage, but the reader assumes hostility anyway: it goes
  through `CapabilityManifest.from_dict`, the same bounded, key-allowlisted,
  booleans-only validator any manifest off the wire gets, and it grants nothing
  (§5.3). The merge preserves the §5.1 honesty rule — an omitted key stays
  unknown and is never flipped to `false`.
- **A foreground service that never stops is a Play-policy conversation.** The
  honest answer is the notification: it says what is running, why, and offers
  Stop. `specialUse` is the correct type for "runs the user's own local server";
  `dataSync` is wrong and, since Android 15, time-capped.
- **Thermal behaviour is unmeasured.** The shedding policy above is designed,
  not observed. It needs a day on the actual Moto G Power in its actual stand.
- **Nothing here has run on a phone.** Everything in "What was actually executed"
  was executed on a build machine: the APKs are built and their contents
  verified, and the Python half was exercised against a real Core on the real
  dependency set — but CPython boot time on a mid-range SoC, Doze survival
  overnight, `BOOT_COMPLETED` recovery, CameraX bound to a *service* lifecycle
  with the screen off, and the memory footprint of CPython + numpy in an Android
  process are all **unverified**. `core-launcher/BUILD.md` lists them as the
  residue that needs the device.

## Privacy invariants — unchanged, and where they are enforced now

[ADR-0002](0002-privacy-boundaries-ram-only.md) is not relaxed by this ADR.

- **Cameras boot OFF.** Unchanged and now doubly true: `CameraMjpegStreamer`
  binds nothing until asked, and the service does not ask.
- **Frames never persist.** The MJPEG path keeps a single latest JPEG in RAM and
  drops it on stop; nothing is written to `filesDir`.
- **Targets and vitals stay live-only.** They travel the same `/ws/live` they
  always did. SQLite moving into app-private storage narrows exposure; it does
  not widen what is stored.
- **Loopback by default.** Decision 4. The Android Core's default bind is
  `127.0.0.1`, exactly as ADR-0002 §1 requires, and LAN is the ADR-0006 opt-in.
- **No new egress.** The APK adds no analytics, no crash reporter, and no
  network peer. The only sockets it opens are the Core's own listener, the
  loopback MJPEG source, and mDNS on the LAN.
