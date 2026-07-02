# ADR-0004 — Defensive-only network sensing; the offensive direction is rejected

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** Augusto (owner-operator)

## Context

One of Wavr's modalities is network sensing: it scans the LAN to infer device
presence as a weak, house-level occupancy hint. Network sensing is inherently
**dual-use** — the same broad capability area contains both benign inventory and
genuine attack tooling — so we have to state, on the record, which side of that
line Wavr sits on and why.

What Wavr does today is narrow and passive: it scans **only the LAN the host is
already joined to**, reads which known devices are present, and uses that as a
defensive occupancy signal. It does **not** use monitor mode, packet injection,
exploitation, credential capture, or any form of data exfiltration.

A tempting "evolution" has been floated: let Wavr **enter a WiFi network and
surveil it / access its data** — i.e. turn the sensing story into an offensive
one. This ADR rejects that direction explicitly so it is never quietly adopted.

## Decision

**Wavr is defensive-only.** Its network sensing is limited to **defensive
inventory of the LAN the host already belongs to.** The following are out of
scope and will not be added:

- monitor-mode / promiscuous capture of networks the host is not authorized on,
- packet injection or deauthentication,
- exploitation of any device or service,
- credential capture or data exfiltration,
- joining or surveilling networks the operator does not own/administer.

**The proposed "enter a WiFi and surveil / access its data" offensive evolution
is rejected**, for two independent reasons:

1. **It would be illegal.** Unauthorized access to a computer system is a
   criminal offence in virtually every jurisdiction — e.g. Ireland's **Criminal
   Justice (Offences Relating to Information Systems) Act 2017**, the US
   **Computer Fraud and Abuse Act (CFAA)**, and equivalent computer-misuse law
   almost everywhere. Building this into Wavr would be building a crime.
2. **It would invert Wavr's identity.** Wavr's entire value proposition is
   privacy-first, on-device, nothing-leaves-the-LAN sensing (see ADR-0002). A
   tool that reaches *into other people's networks* is the exact opposite of
   that. The two cannot coexist in one product without the privacy-first
   identity becoming a lie.

Authorized penetration testing is a **legitimate discipline** — done with the
target owner's consent, within scope. But it is a **separate project** with its
own consent model, rules of engagement, and safeguards. It is **never bundled
into Wavr**.

## Consequences

- **Positive:** Wavr stays lawful and coherent. Contributors and users have an
  unambiguous answer to "is this a hacking tool?" — no.
- **Positive:** The privacy-first brand (ADR-0002) is protected from the one
  feature request that would most directly contradict it.
- **Trade-off:** Wavr will decline otherwise-interesting "network superpowers"
  features. That is intentional; the line is a feature, not a limitation.
- **Boundary:** If offensive security work is ever pursued, it lives in a
  distinct, clearly-scoped, consent-gated project — not here, and not as a Wavr
  extra.
