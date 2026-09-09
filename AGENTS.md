# AGENTS.md — the contract for anything automated working in this repository

This file is **model-neutral and canonical**. Any coding agent, any vendor, any harness
reads this one. Vendor-specific files (`CLAUDE.md` and friends) are thin adapters that
point here and add nothing load-bearing; if one of them ever contradicts this file, this
file wins and the adapter is the bug.

It is written for an agent, but nothing in it is agent-specific. A human contributor who
reads only this file will not be missing anything.

---

## What Wavr is

A local, privacy-first, explainable home presence and network dashboard. It fuses network
scanning, BLE, camera and mmWave signals into **per-room occupancy with a confidence
number and an explanation of where that number came from**.

Licensed [AGPL-3.0-or-later](LICENSE). Third-party components and their licences are in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

**Privacy is the product, not a feature of it.** The invariants below are load-bearing.
Breaking one is not a style regression — it makes a claim the README makes untrue.

## The layout

| Directory | What it is |
|---|---|
| `backend/` | FastAPI + SQLite Core. The fusion, the API, the storage, the tests. |
| `frontend/` | The dashboard. Vanilla JS, **no build step** — `index.html` opens directly. |
| `desktop/` | Tauri v2 (Rust) shell around the loopback Core. See ADR-0007. |
| `mobile/` | Capacitor Android companion app, plus its Kotlin plugins. |
| `core-launcher/` | Android kiosk / Core launcher. A *client* by default; becoming a Core is an explicit act. |
| `site/` | The marketing site. **Never deployed.** |
| `scripts/` | Launchers, generators, and `publication_gate.py`. |
| `docs/adr/` | Architecture decisions, including the ones that supersede each other. |

## Read first

- `PRODUCT.md` — what Wavr is and its design principles.
- `docs/WAVR-PROTOCOL.md` — the wire contract: transport, auth, roles, pairing, nodes.
- `docs/FRONTEND-MODULES.md` — where every frontend module lives and why the load order
  is pinned.
- `docs/adr/0002-privacy-boundaries-ram-only.md` — **the** privacy contract, and it has an
  amendments section: invariants 1 and 7 were superseded deliberately. Read the amendments
  before quoting the invariants.
- `docs/adr/0006-authenticated-lan-access.md` — the opt-in that relaxes loopback-only.
- `docs/adr/0007-desktop-shell.md` — the Tauri architecture.
- `docs/seams.md` — the extension seams.

**There is no roadmap document.** `docs/ROADMAP.md` was deleted. Several ADRs and a few
module docstrings still cite it and its spec letters (A, B2, F2, F3…). Those references are
dead. Do not go looking for the file, and do not recreate it to satisfy a dangling
reference — delete the reference instead.

## Verified commands

Each of these was run against this tree, not remembered.

Each line is independent and starts from the **repository root** — read as a
sequence they do not chain, which is how the pytest line came to be written after
a `cd backend` that made its own path wrong.

```powershell
cd backend; pip install -e .[dev]; python -m wavr.serve   # loopback 127.0.0.1:8000
python -m pytest backend/tests -q                          # full suite; all hardware mocked
cd desktop; npm run dev                                    # Tauri dev (needs Rust MSVC + Node 18+)
powershell scripts/wavr-desktop.ps1                        # zero-Rust launcher (backend + browser)
python scripts/publication_gate.py                         # pre-publication safety check
```

The browser tests inside the suite drive a real Chromium and take the largest
share of the wall clock. To skip them while iterating, `--ignore` the modules
that import `playwright`.

`frontend/index.html` opens directly with no build step. Off localhost it self-switches to
the simulator.

Optional extras, all lazy on purpose: `dev`, `camera`, `mqtt`, `genai`, `mmwave`, `ble`,
`mdns`, `mcp`, `tls`.

## Invariants — never violate

1. **The API is loopback-only by default, and LAN access is one explicit opt-in.** With
   `WAVR_MULTIDEVICE` unset: `127.0.0.1` bind, peer check, Host allowlist, `X-Wavr-Local`
   CSRF header, and any non-loopback caller gets 403 **in code** — so the guard holds even
   under `--host 0.0.0.0`. Set it (ADR-0006, which explicitly supersedes ADR-0002
   invariant 1) and `serve.py` binds `WAVR_BIND` over local-TLS HTTPS/WSS; a same-`/24`
   peer presenting a valid per-device token is admitted, capped by its person's current
   role. Loopback is always `root`. Peers and nodes REQUIRE multidevice, and `app.py`
   refuses to start otherwise. What must never happen is the default drifting, or a second
   path to the LAN that is not this flag.
2. **Cameras boot OFF** on every process start. Enabling is runtime-only and never
   persisted.
3. **Camera frames and pose keypoints are never written to disk.** Only derived signals —
   occupancy, confidence, explanation — persist. Enforced by
   `backend/tests/test_a_frame_never_reaches_the_disk.py`, which reads the vision path and
   refuses any call that puts bytes outside the process. It was a sentence with nothing
   behind it until that test was written.
4. **Per-person x/y targets and vitals are live-only** over `/ws/live`. Never SQLite, never
   MQTT. No movement history on disk, by decision.
5. **The off-localhost frontend is a simulator with zero network requests.** Never wire it
   to a real backend.
6. **Heavy sensing dependencies stay lazy optional extras** — torch, cv2, pyserial, paho,
   bleak, genai. The default install must not require them, and CI never installs them.
7. **No external network request at runtime** from the dashboard or the companion. This is
   why third-party JS is vendored rather than loaded from a CDN — see
   THIRD-PARTY-NOTICES.md.

### Never commit

`wavr.db*`, `.env`, `house.json` (a real floor plan), `local_token`,
`docs/competitive-analysis/` (real network PII), `local.properties`, any keystore.

All are gitignored, and `scripts/publication_gate.py` asks git rather than taking the
sentence's word for it. That check exists because the sentence was **false** when first
written: `mobile/android/.gitignore` ships the Android Studio template, which comments its
keystore lines out, and nothing covered the repository root — five of seven paths were
unprotected. A leaked signing key cannot be un-leaked.

Do not force-add any of them, and note that `.gitignore` does nothing for a file already
in the index.

## How work is judged here

These are not preferences. Each one is the shape of a defect that has actually shipped in
this repository.

- **A guarantee needs a producer.** A claim in a README, a docstring or an ADR needs
  something that fails when it stops being true. An invariant nothing enforces is a
  sentence, not a guarantee. This is the single most common defect class in this codebase.
- **A test without a control is an opinion with terminal output.** A test that cannot fail
  for the reason it names proves nothing. Before trusting a green test, break the thing it
  claims to protect and confirm it goes red — several tests in this repository were found
  to be incapable of failing for the reason they named, and `scripts/check_guarantees.py`
  exists because of them. (This paragraph used to give a count. A number nothing recomputes
  is the same defect one paragraph further down, so it is gone.)
- **Local green and clean-checkout red is a real defect**, not an environment quirk. If it
  passes only because of something already on the machine, it does not pass.
- **Measure, do not guess.** Run the command, read the file, check the actual state.
  Several confident hypotheses in this repository's history were killed by one measurement.
- **Report faithfully.** A failing test is reported as failing, with its output. A skipped
  step is reported as skipped. Do not report a count of passing tests as if it were
  evidence — say what passes, what fails, and paste the error.

## Working style

- **Commit messages, comments, documentation and identifiers are in English**, always,
  including in files that are only read internally.
- **Explain the failure, not just the change.** A commit message should let somebody
  understand what went wrong without reading the diff.
- **No new mandatory dependency** without a reason the lazy-extras pattern cannot cover.
- **Do not revert or absorb unrelated work in progress.** The tree may carry uncommitted
  work; `git checkout <path>` on a file with uncommitted changes has destroyed work here
  before.
- **Route by domain.** Fusion maths, camera pipeline, spatial geometry, network discovery
  and the mobile shell each have their own constraints, documented in the module that owns
  them. Read the owner before changing it from the outside.

## Before publication

`python scripts/publication_gate.py` reports blockers (things whose disclosure cannot be
undone) and passages that are true today but become false the moment the repository is
public. It proves the absence of *known* mistakes, not the absence of mistakes — it is one
input to the decision, never the decision.
