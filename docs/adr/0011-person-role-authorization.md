# ADR-0011 — Person-role authorization: narrowing-only, resolved per request

- **Status:** Accepted — shipped on `feat/spatial-layer`.
- **Deciders:** Augusto (owner-operator)

## Context

[ADR-0009](0009-space-people-and-device-functions.md) separated "what a human
may do" (person role: `owner|admin|user|guest`) from "what a credential may
reach" (device auth role: `central|user|agent|guest`), joined by exactly one
bridge — `space_store.device_role_for_person()` — that derived a NEW device's
credential role from its owner's person role at the moment it paired. That ADR
was explicit that the bridge was one-directional and mint-time only: a
person's role could change (`set_person_role`) without touching any credential
already issued, and the API layer told the admin so directly, rather than
implying a demotion had taken effect everywhere.

That left a real gap. "Ana is now a Guest" (demoted from User) meant nothing
happened to Ana's phone, which kept `user`-level access until someone
remembered to separately revoke it — an administrative action a household
operator has no particular reason to think of, and one the UI did not prompt
for. The person-role model existed to let an operator answer "what can Ana do"
in one place; if changing the answer there didn't change what Ana's devices
could actually do, the model was recording a decision without enforcing it.

The fix has to reckon with two failure directions that are NOT symmetric:

- **Under-enforcing a demotion** is a live security hole: a person removed
  from `admin` keeps administering until someone thinks to chase down every
  device they ever paired.
- **Over-enforcing a promotion** is a different, more surprising failure: if
  promoting Ana to Admin silently widened a `user`-role token that is already
  sitting on a phone she lent a houseguest last week, "make Ana an Admin"
  would retroactively hand elevated access to a device the operator never
  looked at.

A single, undifferentiated rule ("credential role always tracks person role")
would fix the first problem by creating the second.

## Decision

**Resolve a device's effective role as the narrower of its issued role and its
person's current role, fresh on every authenticated request**
(`auth.narrower_role`, `auth._apply_person_cap`, wired at `access_for_scoped`'s
one call site in `app.py` — both the ordinary HTTP role/scope gate and the MCP
tool-scope gate go through it):

```
effective role = min(role the credential was issued, role the person holds today)
```

1. **Narrowing only.** Demotion takes effect on the very next request — no
   revoke, no window, no relying on an admin to separately act. Promotion
   never widens a credential already in the wild; reaching a wider role
   requires deliberately re-pairing the device. This is asymmetric on purpose:
   it closes the under-enforcement hole in Context without opening the
   over-enforcement one.
2. **`agent` is exempt.** An `agent` credential is a bounded machine principal
   (Wavr Pass, Phase 2A) — it has no person and is not a human's device, so
   there is no person-role rung to cap it by. `_apply_person_cap` returns the
   credential's role unchanged whenever `device.role == "agent"`, before it
   ever looks up a person.
3. **A dangling association denies, it does not degrade.** If the person a
   device is associated with is no longer in the Space, `_apply_person_cap`
   returns `None` — the caller is denied outright, exactly like an
   out-of-subnet peer or an unknown token. It does **not** fall back to the
   credential's originally-issued role. `space_store.remove_person` is
   expected to revoke that person's devices already; this is the fail-closed
   backstop for the case where it did not, or has not yet.
4. **A device with no person is untouched.** Every device paired before this
   mechanism existed, every Node (a separate credential space entirely,
   §7 of the protocol spec), and every device an operator never associated
   with a person resolves exactly as it did before — `_apply_person_cap`
   short-circuits to `device.role` unchanged the moment `person_id` is empty
   or `person_role_fn` is not injected. This is what makes the change
   additive rather than a behavior change for the majority of existing
   installs.
5. **A storage fault fails closed.** `app.py`'s `_person_role` catches
   `sqlite3.Error` and returns `None` rather than propagating — a database
   hiccup denies the request rather than serving it at the un-narrowed,
   original role. It is better to reject a legitimate request during a
   transient fault than to silently widen access because a lookup failed.

## Rejected alternatives

**Making the person role authoritative in both directions** (credential role
always equals, not just caps, the person's current role) was rejected for the
reason named in Context: it would retroactively widen a credential the moment
its owner is promoted, regardless of which specific device that credential is
on, when it was last used, or who is currently holding it. A person's role is
a statement about a human; a device's credential is a statement about one
specific piece of hardware that human is not necessarily still in possession
of. Collapsing the two directions treats "Ana is trustworthy" and "this
particular phone, wherever it is right now, should have Ana's current level of
access" as the same fact. They are not, and the asymmetry in Context exists
specifically because getting this wrong in the widening direction is a worse
failure than getting it wrong in the narrowing direction is a delay.

**Reissuing every affected token on a role change** (server-side rotation of
every device credential belonging to a demoted or promoted person) was
considered and rejected as unnecessary complexity: it would require tracking
which devices belong to which person specifically to invalidate them
transactionally, duplicate work the per-request cap already does for free
without touching a single stored token, and it would still face the same
over-enforcement question for promotion — a reissued, widened token is exactly
as surprising as a live one silently widening in place.

## Consequences

- **Positive:** the gap in Context is closed. Demoting Ana from Admin to Guest
  takes effect on her very next request, from every device she has ever
  paired, without the operator needing to separately hunt down and revoke
  each one.
- **Positive:** zero behavior change for the common case. A device with no
  person association — which describes every device paired before this
  feature existed, plus every Node and most `agent` credentials — resolves
  identically to before.
- **Trade-off:** a person's role is no longer purely additive information
  sitting next to the credential system; it now participates in every
  authorization decision for an associated device. A reviewer auditing "what
  can this device do" must check both what it was issued (§4.3 of the
  protocol spec) and what its associated person currently holds (§10.1.1) —
  one more thing to check, in exchange for one fewer way a demotion can be
  forgotten.
- **Consistency note:** this ADR revises one sentence of ADR-0009's
  Consequences — "a person's role can change... without silently changing
  what any of their already-issued credentials can reach" was true of the
  mint-time bridge ADR-0009 introduced, and remains true of that bridge in
  isolation (§10.1 of the protocol spec). It is no longer true of the system
  as a whole now that the per-request cap in this ADR sits alongside it.
  ADR-0009 is left as written, as the historical record of what was decided
  at the time; this ADR is the record of what changed and why.
- **Enforcement:** `docs/WAVR-PROTOCOL.md` §10.1.1 states this decision as a
  set of MUST/MUST NOT protocol requirements; any future implementation of
  the Wavr Protocol's person/device model is expected to conform to that
  section, not to ADR-0009's original mint-time-only description alone.
