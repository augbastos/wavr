# ADR-0001 — Don't fork RuView; use mmWave LD2450 for real per-person position

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** Augusto (owner-operator)

## Context

RuView (`github.com/ruvnet/ruview`) advertises through-wall 3D human pose
estimation from WiFi CSI. On paper it is exactly the kind of upstream sensing
engine Wavr is designed to orchestrate as a plugin, so before committing to it
as the source of per-person position/posture we ran a source-code audit rather
than trusting the README.

The audit found a large gap between the marketing and the running code:

- The `persons` field emitted by the live `/ws/sensing` WebSocket — the only
  path a consumer like Wavr would actually integrate against — is **always
  `null`**. No per-person geometry ever reaches the wire.
- The "pose" advertised on the site runs on a **separate route** and is a gait
  **simulation heuristic** built from sine/trig functions, not inference over
  real CSI. Its measured accuracy is roughly **2.5% PCK@20** — effectively
  noise. This is not our accusation: RuView's own developers admit it in a repo
  ADR.
- A **real trained model** (~82% by their own numbers) does exist, but only as a
  loose HuggingFace artifact that is **disconnected from the server** — it is not
  wired into any live endpoint, so integrating it would mean building and
  operating the serving path ourselves.

What RuView actually delivers today, verified in the code, is **room-level
presence and breathing rate via a genuine FFT pipeline**. Heart-rate is present
but broken (tracked as an open issue upstream).

## Decision

**We will not fork RuView.** Forking would mean adopting and maintaining a large
codebase whose headline feature (through-wall pose) is a stub, for a payoff that
does not exist at the WebSocket boundary we integrate against.

Instead:

1. Wavr keeps RuView at arm's length. Its working capability (presence +
   breathing) can be consumed later through a thin `RuViewSource` that treats
   RuView as an **external service** over its existing WebSocket — only *if* the
   CSI hardware is ever actually run. The fusion weights will reflect its true,
   room-level confidence, not its advertised one.
2. For **real per-person x/y position**, we adopt an **mmWave radar (HLK-LD2450,
   ~€15, USB serial)** as the honest, cheap path. It reports tracked targets with
   coordinates directly, no model to train or serve, and fits the existing
   `SensorSource` seam. (Parser + source are already written and mock-tested;
   see ROADMAP.)

## Consequences

- **Positive:** We avoid inheriting maintenance of a stubbed feature and a
  serving path we did not build. Per-person position comes from a sensor that
  actually measures it, for the price of a coffee. Wavr's "integration over hype"
  stance is upheld: we consume what upstream *actually* does, and the weights
  tell the truth.
- **Positive (signalling):** Auditing a dependency's running code before adopting
  it — and walking away when the README oversells — is exactly the
  dependency due-diligence expected of a senior engineer. This ADR is the record
  of that call.
- **Negative / trade-off:** WiFi CSI's genuine appeal (no line of sight, no
  camera) is deferred. If we later want through-wall pose, it remains open
  research, not a dependency we can pull off a shelf.
- **Follow-up:** Should RuView wire its 82% model into a live endpoint, revisit
  the arm's-length `RuViewSource` and re-weight accordingly.
