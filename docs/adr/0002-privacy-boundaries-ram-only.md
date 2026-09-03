# ADR-0002 — Strict privacy boundaries: loopback-only, cameras off, live-only targets

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** Augusto (owner-operator)

## Amendments

This ADR's invariants were written before multi-device access and the
multi-provider narrator existed. Two of the seven invariants below have been
**deliberately, explicitly** relaxed by later, superseding decisions. This ADR
is a historical record and is not rewritten — the amendments here point at
what changed and where that decision lives:

- **Invariant 1 ("no environment variable ... can expose the API to the
  LAN")** is superseded by [ADR-0006](0006-authenticated-lan-access.md).
  `WAVR_MULTIDEVICE=1` does exactly that, by design, under an explicit opt-in:
  a same-/24 LAN peer presenting a valid per-device token is admitted (see
  `docs/WAVR-PROTOCOL.md` §4). The default — the flag unset — remains
  byte-identical to this invariant as originally written.
- **Invariant 7 ("the only cloud egress is the opt-in Gemini narrator,
  text-only")** is stale. `backend/wavr/narrator.py` now supports four
  providers — one local/zero-egress (Ollama) and three cloud (OpenAI,
  Anthropic, Gemini) — selected by `WAVR_NARRATE_PROVIDER`, still
  double-opt-in and still text-only (never frames/keypoints/coordinates).
  Separately, other opt-in, individually-gated egress paths now exist behind
  the single "Connectors & Services" master switch
  (`backend/wavr/connector_store.py`'s `egress_allowed()`): e.g. Telegram
  notifications, the Wavr Assistant's cloud engine, and the opt-in internet
  reachability/speed-test checks (`WAVR_HEALTH_RESOLVERS`, the ndt7/M-Lab
  path). Every one of these remains opt-in, individually toggleable, and
  gated behind the same master kill-switch — the *shape* of the invariant
  ("egress is opt-in and disclosed, never silent or default-on") holds; the
  specific claim "only the Gemini narrator" no longer does.

## Context

Wavr senses people inside a home. That makes it, by construction, a privacy-
sensitive system: it handles camera frames, pose keypoints, per-person position,
and vital-sign estimates. The value of the product depends on users trusting that
this data does not escape the machine or accumulate into a surveillance record.
Privacy therefore has to be an **architectural invariant**, not a setting a user
(or a future contributor) can accidentally weaken.

This ADR records the invariants and why each exists, so they are treated as
load-bearing rather than optional.

## Decision

The following boundaries are hard invariants. Where noted, they are **hard-coded,
not configurable** — there is deliberately no knob to relax them.

1. **API is loopback-only.** The backend binds `127.0.0.1` and additionally
   enforces, per request, a **real peer-address check + a `Host` allowlist + a
   CSRF header** (`X-Wavr-Local`). These checks are hard-coded, not read from
   config, so no environment variable or misconfiguration can expose the API to
   the LAN.
2. **Cameras boot OFF on every process start.** Camera sources are registered
   disabled; enabling is a runtime, CSRF-guarded action. Toggle state is
   in-memory only — there is no persisted "ON", so a restart always returns to
   the safe default.
3. **Frames and pose keypoints are never persisted.** Only *derived* signals
   (presence, confidence, per-modality explanation) are written. Raw imagery and
   raw keypoints exist in RAM for the duration of inference and are then gone.
4. **Per-person x/y targets and vital estimates are live-only.** They travel on
   the `/ws/live` WebSocket to the open dashboard and **never touch SQLite or
   MQTT**. A movement/vitals history on disk would be a standing privacy
   liability (where each person was, minute by minute) for no product benefit, so
   it is not written at all.
5. **The public demo makes zero LAN requests.** Off-localhost, the single static
   frontend **self-switches to a simulator** and issues no requests to any
   backend. Portfolio viewers see synthetic data; their network is never touched.
6. **Heavy/sensing deps are lazy optional extras.** `torch`/`cv2`, `pyserial`,
   `paho`, and `genai` are opt-in extras (`[camera]`, `[mmwave]`, `[mqtt]`,
   `[genai]`), imported lazily. A default install cannot silently pull in a
   camera stack or a cloud client.
7. **The only cloud egress is the opt-in Gemini narrator, text-only.** It is
   **double opt-in** and sends text (derived state summaries), never frames,
   keypoints, or coordinates. Everything else stays on the machine.

## Consequences

- **Positive:** The privacy story is verifiable from the architecture, not from
  documentation goodwill. "Where could data leak?" has a short, auditable answer:
  nowhere, except the one text-only narrator the user explicitly enables twice.
- **Positive:** Because targets and vitals are never stored, a stolen disk or a
  leaked SQLite file reveals no location history — the sensitive data simply is
  not there.
- **Trade-off:** Some features are foreclosed by design — e.g. "replay last
  night's movement" or historical vitals charts are impossible without violating
  invariant 4, and we accept that. If such a feature is ever wanted it requires a
  new, explicit ADR that supersedes this one, not a config flag.
- **Contributor rule:** New `SensorSource` implementations inherit these
  invariants. A source that would persist raw data, bind beyond loopback, or add
  non-opt-in egress cannot be merged without superseding this ADR.
