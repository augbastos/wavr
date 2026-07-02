# ADR-0003 — Wavr is not a medical device; vitals are experimental and non-diagnostic

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** Augusto (owner-operator)

## Context

Some Wavr sensing modalities produce health-adjacent outputs: breathing rate and
(experimentally) heart-rate estimates from CSI/radar, and posture states that
could be framed as "fall detection". These are attractive demos, and it is
tempting to describe them in clinical language ("monitors vitals", "detects
cardiac arrest", "eldercare safety system").

That framing is legally and ethically loaded. Under **EU MDR 2017/745**, a
product **intended** for the diagnosis, prevention, monitoring, prediction, or
treatment of disease is a **regulated medical device**. That intent — how it is
described and marketed — is itself part of what triggers regulation. A regulated
device requires **CE marking, a clinical evaluation, a quality-management system,
post-market surveillance, and liability cover**. All of that is squarely out of
scope for a solo, open-source, portfolio/research project, and none of Wavr's
estimates have been clinically validated.

## Decision

**Wavr is explicitly NOT a medical device and makes NO clinical or life-safety
guarantees.** We keep the software firmly outside the medical-device intent
boundary:

1. **Breathing and heart-rate estimates are labelled EXPERIMENTAL and
   NON-DIAGNOSTIC** wherever they appear (UI, docs, API). They are signal-
   processing outputs for interest and demonstration, not measurements to act on
   medically.
2. **"Fall detection" and any care-home / eldercare scenario are RESEARCH
   DEMOS**, not certified safety systems. They illustrate what multi-modal fusion
   *could* do; they are not to be relied on for anyone's safety.
3. **No clinical or life-safety claims are made or supported.** We do not claim
   Wavr "detects cardiac arrest", "monitors patients", "prevents falls", or any
   equivalent. Language that implies diagnosis or life-safety is treated as a bug.

## Consequences

- **Positive (legal):** By never asserting medical *intent*, Wavr stays outside
  the MDR trigger. The author is not exposed to medical-device liability or
  certification obligations they cannot meet.
- **Positive (signalling):** Knowing where the regulatory line is — and choosing
  to stay on the safe side of it rather than overclaiming — signals engineering
  and product maturity, not a lack of ambition.
- **Trade-off:** Wavr cannot be marketed or deployed as a safety or care product
  in its current form. Crossing that line is not a copy change; it is a different
  project with a company, a QMS, clinical validation, and certification behind it
  (see ROADMAP "Needs a company + regulatory work").
- **Enforcement:** Copy and API fields that describe vitals or falls must carry
  the experimental / non-diagnostic framing. A PR that introduces clinical or
  life-safety phrasing contradicts this ADR and should be rejected or must
  supersede it.
