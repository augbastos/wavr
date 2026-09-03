# ADR-0009 — Space as first-class; person role, device function, and device auth role as three independent axes

- **Status:** Accepted (shipped on `feat/spatial-layer`)
- **Deciders:** Augusto (owner-operator)
- **Date:** not separately recorded; lands in the same implementation pass as the
  Capability Manifest, Core coordination, and Discovery Inbox work described in
  `docs/spatial-layer-briefing.md`.

## Context

Wavr had no first-class notion of the physical environment it senses — no
Space. Everything was implicitly "this house," so nothing could be named,
joined by a second Core, or transferred to a new owner.

Worse, a single existing value did double duty: `Device.role == "central"`
meant, at once, *"this credential may administer Wavr"* and *"this box is a
hub."* That conflation makes three ordinary, true sentences impossible to
express in the data model:

- "Augusto is the Owner, and these four devices are his."
- "The tablet is a Client only; the laptop is Core **and** Node **and**
  Client."
- "Make Ana an Admin" — without touching a single device's credential.

A fourth pressure point, added by the Capability Manifest work
(`backend/wavr/capabilities.py`): once a device can report what it *can*
physically do (has a camera, runs on battery, has enough RAM), something has
to hold what an operator *decided* it *should* do (be a Node in the kitchen; be
a Client only). That is a third concept again, distinct from both "who may
administer" and "what may this credential reach."

## Decision

**Space becomes first-class**, and the three previously-conflated concepts
become three independent axes, stored and reasoned about separately
(`backend/wavr/space_store.py`, `backend/wavr/core_registry.py`):

1. **Person role** (`people` table) — what a *human* may do: `owner | admin |
   user | guest`, each resolving to a capability set
   (`space_store.PERSON_CAPABILITIES` — a vocabulary of its own, e.g.
   `space:transfer`, `people:manage`, `device:manage`, `node:manage`,
   `core:manage`, deliberately distinct from `auth.SCOPES`). Capability-based
   rather than a fixed enum of privileges, so an arbitrary future org role
   (facilities, IT, contractor) can be added later as a named capability set
   without a schema change.
2. **Device function** (`device_functions` table) — what *job* a box does for
   the Space: `core | node | client`, freely and simultaneously combinable.
   "Core + Node + Client" on one laptop is the ordinary single-machine home
   setup, and the model must not force a device to pick one.
3. **Device auth role** (`backend/wavr/devices.py`, **UNTOUCHED**) — what a
   *credential* may reach over the HTTP API: `central | user | agent | guest`,
   still exactly the thing `auth.py` enforces, unchanged by this ADR.

Axis 3 is **deliberately left exactly as it is**. It is the tested,
already-audited security surface, and widening it to also carry person-role or
device-function semantics would be the easiest way to turn an onboarding
convenience into a privilege bug. Instead, a single, narrow, one-directional
bridge — `space_store.device_role_for_person()` — *derives* axis 3 from axis 1
at the moment a device pairs (an Owner's or Admin's new device pairs as
`central`; a User's pairs as `user`; a Guest's pairs as `guest`). The bridge
feeds the old, tested gate; it never bypasses or replaces it, and it never
grants a credential role a human could not already have been granted by hand
before this model existed.

A **Capability Manifest** (a device's self-reported "what can I physically
do") is a fourth, related but distinct concept, already separated out in
`capabilities.py` and specified at the protocol level in
`docs/WAVR-PROTOCOL.md` §5. It informs a *recommendation* for axis 2 (device
function); it grants nothing on any axis by itself.

Legacy installations (a pre-Space `wavr.db` with paired Devices) are
*adopted*, not migrated destructively: every existing `central` Device is
associated with one synthesized Owner, with function `client`; every other
device is left unassociated rather than guessed at. No credential is
reissued, no token invalidated, no device role changed (`SpaceStore.adopt_legacy`).

## Rejected alternatives

**Extending `devices.VALID_ROLES`.** The cheaper-looking fix — add `owner`/
`admin` values directly onto the credential role, so a device's row said both
who its human was and what it could reach — was rejected. It reproduces the
exact conflation this ADR exists to undo, and it means every future
person-role addition (a "facilities" role, a "contractor" role) requires
touching the tested auth-role enum and every route that switches on it,
instead of only adding a capability set on axis 1.

**Replacing the auth model.** Building a new, unified RBAC/ABAC system from
scratch to express all three concepts in one model was also rejected. The
actual gap was the *container* (a Space) and the *missing axes* (person role,
device function) — not a flaw in the existing credential-reach mechanism,
which is a small, already-audited surface (`auth.py`'s role/scope resolution,
exercised by its own test suite and by the Wavr Pass scope work). Rewriting a
working, tested security boundary to solve a modeling gap elsewhere would have
traded a contained, additive change for a full re-audit of the entire access
surface, for no functional gain.

## Consequences

- **Positive:** the three sentences in Context are now directly expressible:
  a Person row with role `owner`, a `device_functions` row per box recording
  its combined jobs, and axis 3 continuing to gate every HTTP route exactly as
  before.
- **Positive:** zero regression risk to the tested auth surface — axis 3's
  code, tests, and every route gate that reads `request.state.role` are
  byte-identical to before this ADR. The new axes are additive tables
  (`space`, `people`, `person_devices`, `device_functions`,
  `device_capabilities`), not a rewrite of `devices.py` or `auth.py`.
- **Positive:** a person's role can change (`set_person_role`) without
  silently changing what any of their already-issued credentials can reach —
  the API layer surfaces "these devices keep their old access until you
  revoke them" explicitly, rather than implying a demotion took effect
  everywhere at once.
- **Trade-off:** there are now three places to reason about "what can this
  device/person do," not one. A reviewer checking a new feature's access
  control must check the right axis (or axes) rather than a single role
  field; `docs/WAVR-PROTOCOL.md` §10 exists specifically so that check has one
  canonical place to start.
- **Enforcement:** a future org role, device job, or capability MUST be added
  to the relevant axis's own fixed vocabulary (`space_store.PERSON_ROLES` /
  `PERSON_CAPABILITIES`, `capabilities.DEVICE_FUNCTIONS`) — never expressed by
  widening `devices.VALID_ROLES` or by giving a manifest/discovery claim
  authority it was not designed to carry (§5.3, §10.2 of the protocol spec).
