# External spatial providers

What Wavr can take spatial evidence from, what it cannot, and — for each of the
latter — the exact thing that is missing.

This document exists because the honest answer differs a lot between vendors, and
a compatibility list that flattened them into "supported / not supported" would
be misleading in both directions.

---

## How anything gets in

There is one inbound contract, and every integration is an adapter onto it.

```
POST /api/providers/<provider_id>/observations
{
  "observations": [
    {"room": "kitchen", "present": true, "confidence": 0.8, "sensor_id": "ap-3"}
  ]
}
```

A provider must be **registered** first, and the registration is what makes the
declaration enforceable:

```
PUT /api/providers/external/acme_rtls
{"label": "Acme RTLS", "reach": "lan", "modality": "ble", "ceiling": "room"}
```

`reach` has no default. A provider that does not say how far it reaches cannot be
enabled, because the alternative — resolving an unstated value to something
comfortable — is a privacy failure that nobody notices.

`ceiling` is capped against what the declared modality can support in `fusion`. A
declaration may lower a ceiling and never raise one, so an adapter cannot buy
`position` by asking for it.

**Why this shape rather than four vendor adapters.** Every enterprise positioning
API listed below is behind a partner agreement. An adapter written against a
schema nobody here has seen would compile, pass its own invented fixtures, and
fail on first contact with the real thing — while looking finished. So Wavr
defines the shape it accepts; the twenty lines that translate one vendor into it
are written by whoever holds the credentials.

---

## Categories

| | Meaning |
|---|---|
| **A** | Publicly implementable now. Built. |
| **B** | Implementable; needs credentials for live validation only. |
| **C** | Proprietary. No public schema, so no adapter — only the generic path. |
| **D** | No meaningful integration surface today. |

---

## A — built and tested

### Home Assistant · `home_assistant`

**Inbound.** Binary sensors an operator has mapped to a Wavr room become
`SensingEvent`s and go through the same fusion as a camera. `wavr/ha_presence.py`.

Four rules that are not obvious and are each load-bearing:

- The room comes from the **mapping**, never the payload. An HA area renamed at
  three in the morning must not relocate presence.
- `unavailable` and `unknown` are **not** `off`. They produce no event at all, so
  a dead sensor cannot become positive evidence of an empty room.
- A motion sensor maps to `pir`, which is outside `TRUSTED_ABSENCE_MODALITIES` —
  its `off` cannot clear a room. Somebody reading a book stops triggering a PIR
  long before they leave.
- Nothing is polled that nobody mapped. Zero mappings means zero requests.

HA keeps its own clock, so `last_changed` goes through `wavr/timebase.py`.

**Reach:** `lan`. Nothing about the home leaves the premises.

### OpenXR runtimes · `openxr`

**Identity, fully.** `XR_EXT_spatial_persistence` gives a stable `XrUuid` per
anchor, and Wavr maps those onto its own anchors. A headset recognising its own
anchor can ask what that place is called and what Wavr knows about the room —
no geometry, no alignment, working on the first run.

**Coordinates, once aligned.** OpenXR's `LOCAL` origin is wherever the session
started, so a transform is *solved* from at least two anchors both systems can
see (`wavr/spatial_align.py`). One anchor gives translation and leaves yaw
unknown; Wavr refuses rather than rendering a room at an arbitrary angle.

⚠️ `XR_SPATIAL_PERSISTENCE_SCOPE_LOCAL_ANCHORS_EXT` persists per device, per user
**and per application** — so those UUIDs are namespaced by app in Wavr's mappings.
Two apps using the same UUID mean two different places.

**Blocked:** runtime validation against a real headset. Everything above is tested
against fixtures; nothing has run on hardware.

### Any positioning system, through the generic contract

The `POST /observations` path above. Category A because it is Wavr's own contract,
not a guess at somebody else's.

---

## B — implementable, blocked only on credentials

### Cisco Spaces · `cisco_spaces`

Cisco publishes a **Partner Firehose API**: a real-time stream of location and
presence with zone entry/exit events. The full schema is behind partner
enrolment at `partners.dnaspaces.io`, requested through a Cisco representative.

**What can be built without it:** nothing vendor-specific, honestly. The shape of
the stream is known from public material; the field names are not, and inventing
them would produce an adapter that looks complete and is wrong.

**What to do today:** register it as an external provider and post to the generic
contract from a small translator. `reach: "cloud"` — Cisco Spaces is a cloud
service, and a household needs to see that.

**Blocked:** the partner agreement, then the schema, then live validation.

### Cognitive Systems (WiFi Motion) · Origin Wireless · nami

All three sell Wi-Fi sensing, all three deliver it through carrier or OEM
agreements rather than a public developer API.

**What can be built:** the generic path, and it fits well — these systems produce
room-level presence and motion, which is exactly `modality: "wifi_csi"`,
`ceiling: "room"`.

**Blocked:** commercial access. There is no developer signup to complete.

---

## C — proprietary, no public schema

Nothing is implemented for these beyond the generic contract, and that is the
honest state rather than a gap to be filled by guessing.

If you hold credentials for one, the integration is:

1. `PUT /api/providers/external/<id>` with the reach and ceiling that vendor
   genuinely supports.
2. A translator that reads their stream and posts Wavr-shaped observations.
3. Pair it as a device with `presence:write` — an adapter reports, it does not
   administer.

Step 3 matters. A credential that could also re-register its own provider could
declare itself `position`-capable and start asserting coordinates.

---

## D — no integration surface

**acoAR** and similar AR-content products consume spatial context rather than
producing it. They belong on the *other* side of Wavr: the Experience SDK, not
the provider contract. `sdk/javascript`, `sdk/python`, `dev.wavr.sdk`.

---

## Apple · `apple_spatial`

Its own row, because the split is unusual.

**Core side: built and tested.** Nearby Interaction readings parse into evidence,
ARKit anchors map onto Wavr anchors, and the alignment uses the same solver as
OpenXR. `wavr/apple.py`.

Two rules that shape it:

- `NIDiscoveryToken` is generated **per session** and is not a device identity.
  Storing one as "this is Augusto's phone" would hold a dead reference by the
  next session — and would confidently attribute a stranger's phone to Augusto if
  a token were ever reused.
- A UWB distance without a direction is a **sphere**. Nearby Interaction only
  produces a direction when devices roughly face each other with the app in the
  foreground, which is much rarer than a demo implies. Wavr keeps the distance,
  which is genuinely useful, and produces no coordinate.

**Blocked:** the iOS half. It needs Xcode to build and a device to validate, and
nothing in this repository has run on an iPhone. `describe()` says so in its own
notes rather than in a footnote here.

---

## What every provider is checked against

Wherever an integration lands, these hold:

- **The precision ceiling is enforced**, not recorded. A `room`-ceiling provider
  posting a count has the count dropped, and the response says which observation
  and why.
- **An unknown room is refused**, with the known ones listed. A typo must not
  conjure a room that appears on the dashboard and exists nowhere in the house.
- **Absence carries no mass.** A provider reporting "nobody here" contributes
  nothing to the merge, exactly like every first-party source.
- **Clocks are foreign.** Every external timestamp goes through `timebase`, which
  corrects ordinary drift and refuses to correct a timezone-sized skew — it
  reports that instead, because it is a misconfiguration somebody has to fix.
- **Reach is visible.** Every provider's reach appears on the privacy screen, and
  `leaves_the_home` is the one question a household actually asks.
