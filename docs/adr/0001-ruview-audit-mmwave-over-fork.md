# ADR-0001 — Don't fork RuView; use mmWave LD2450 for real per-person position

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** Augusto (owner-operator)
- **Last reviewed:** 2026-09-09 — see [Update, 2026-09-09](#update-2026-09-09).
  Everything in Context below is written in the present tense and describes
  **2026-07-02**. Several of those statements are no longer true of the upstream
  project. They are kept as written because an ADR is the record of a decision
  made on the evidence available at the time; the update section says what has
  since changed.

## Context

*(As audited on 2026-07-02.)*

RuView (`github.com/ruvnet/RuView`) advertised through-wall 3D human pose
estimation from WiFi CSI. On paper it was exactly the kind of upstream sensing
engine Wavr is designed to orchestrate as a plugin, so before committing to it
as the source of per-person position and posture we ran a source-code audit
rather than trusting the README.

The audit found a gap between what the project's front page promised and what
the running code did:

- The `persons` field emitted by the live `/ws/sensing` WebSocket — the only
  path a consumer like Wavr would actually integrate against — was **always
  `null`**. No per-person geometry reached the wire.
- The "pose" advertised on the site ran on a **separate route** and was a
  **simulated gait heuristic**, not inference over real CSI. Its stated accuracy
  was roughly **2.5% PCK@20** — effectively noise. That figure is upstream's
  own, published in their `docs/huggingface/MODEL_CARD.md`.
- A **real trained model** (~82% by their own numbers) existed, but as a
  HuggingFace artifact **not wired into any live endpoint**, so integrating it
  would have meant building and operating the serving path ourselves.

What RuView delivered at that point, verified in the code, was **room-level
presence and breathing rate via a genuine FFT pipeline**. Heart rate was present
but not working.

## Decision

**We will not fork RuView.** Forking would mean adopting and maintaining a large
codebase whose headline feature (through-wall pose) was a stub, for a payoff
that did not exist at the WebSocket boundary we integrate against.

Instead:

1. Wavr keeps RuView at arm's length. Its working capability (presence +
   breathing) can be consumed later through a thin `RuViewSource` that treats
   RuView as an **external service** over its existing WebSocket — only *if* the
   CSI hardware is ever actually run. The fusion weights will reflect its true,
   room-level confidence, not its advertised one.
2. For **real per-person x/y position**, we adopt an **mmWave radar
   (HLK-LD2450)** as the honest, cheap path. It reports tracked targets with
   coordinates directly, with no model to train or serve, and fits the existing
   `SensorSource` seam. It is a **UART/TTL module**, so reaching a PC also needs
   a USB-TTL adapter — see `docs/deploy/bring-up-and-expansion.md` for the parts
   list. (Parser and source are already written and mock-tested — see
   `backend/wavr/sources/mmwave.py`.)

## Consequences

- **Positive:** We avoid inheriting maintenance of a stubbed feature and a
  serving path we did not build. Per-person position comes from a sensor that
  actually measures it, cheaply. Wavr's "integration over hype" stance is
  upheld: we consume what upstream *actually* does, and the weights tell the
  truth.
- **Positive (signalling):** Auditing a dependency's running code before
  adopting it — and walking away when the front page oversells — is the
  dependency due-diligence this project holds itself to. This ADR is the record
  of that call.
- **Negative / trade-off:** WiFi CSI's genuine appeal (no line of sight, no
  camera) is deferred. If we later want through-wall pose, it remains open
  research, not a dependency we can pull off a shelf.
- **Follow-up:** Should RuView wire its trained model into a live endpoint,
  revisit the arm's-length `RuViewSource` and re-weight accordingly.

---

## Update, 2026-09-09

This section exists because the Context above is a snapshot, and a snapshot
written in the present tense reads as a current claim about somebody else's
active project. RuView has roughly 92.9k stars, is MIT-licensed, and was pushed
to on the day this update was written. Statements about it need a date attached,
and the ones below were checked against the GitHub API rather than re-read from
the same source that produced them.

**What has changed since the audit:**

- **The audited code is archived.** What this ADR examined now lives under
  `archive/v1/` in the upstream repository, marked there as superseded. The
  current sensing server reports a person **count** rather than the null
  `persons` array described above, so the first bullet in Context describes code
  the project itself has retired.
- **Heart rate is no longer an open issue.** The original text said it was
  "tracked as an open issue upstream". Checked on 2026-09-09:
  `repo:ruvnet/RuView heart in:title state:open` returns **0**, and the
  corresponding issues are **closed**. That sentence was verifiable and wrong,
  and it is exactly the kind of statement this repository should never make
  about somebody else's work without re-checking it.
- **The accuracy numbers moved.** Upstream now publishes a higher figure for the
  on-device single-radio path and a much higher torso-PCK figure for a
  benchmark-trained model. The 2.5% above is upstream's own number, but it is
  from 2026-07 and must not be quoted as current.
- **The source of the 2.5% figure was cited wrongly.** The original text said
  RuView's developers "admit it in a repo ADR". It is in their
  `docs/huggingface/MODEL_CARD.md`. When the whole force of a sentence is "this
  is not our accusation, they say it themselves", pointing at the wrong document
  removes the only thing that made it fair.
- **One detail was never verifiable and has been removed.** The original text
  described the gait heuristic as built "from sine/trig functions". The mock
  generator in the archived tree is explicit that it is synthetic, but it uses
  `random.uniform`; no trigonometric construction was found. A specific,
  checkable detail invented about a third party's code is worse than a vague
  one.
- **The word "marketing" is gone**, replaced by "front page". The original
  framing described a gap between promotion and code; upstream today publishes
  its own table of what is real and what is not, and the older word no longer
  describes them fairly.

**What has not changed:** the decision. Wavr does not fork RuView, and the
reasoning holds independently of all of the above — an arm's-length integration
over a WebSocket, weighted for what the source actually measures, is the right
shape whether or not upstream's pose path improved.

**If this is revisited:** re-audit against a named commit and record the SHA. An
undated audit of a moving project turns into a false statement on its own, with
nobody having edited it.
