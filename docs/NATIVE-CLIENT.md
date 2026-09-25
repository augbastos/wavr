# Native client contract

How a native Wavr screen gets what it shows. One document, so the Android,
Android TV, desktop and Apple clients stay thin and cannot drift.

## The rule

A native client **renders**; it does not decide. Every verdict it shows -- is
Wavr running, does anything need a person, is a room occupied and how sure, is a
source fresh -- comes from the Core. The client asks the native runtime
(`libwavr_native`, C ABI in `native/include/wavr/wavr.h`) for a **snapshot**
and draws it. A client that computes health, fusion, trust or pairing policy
itself is a bug: move that logic into the Core or the runtime.

## Getting a snapshot

```c
if (!wavr_abi_compatible(WAVR_ABI_VERSION_MAJOR, WAVR_ABI_VERSION_MINOR)) abort_ui();
wavr_snapshot* s = wavr_snapshot_fetch("http://127.0.0.1:8000", token, pin, 6000);
int n = wavr_snapshot_json(s, NULL, 0);          /* size */
char* buf = malloc(n + 1);
wavr_snapshot_json(s, buf, n + 1);               /* the JSON below */
int exit_code = wavr_snapshot_exit_code(s);      /* same as `wavr status` */
wavr_snapshot_free(s);
```

- `pin` NULL/empty: allowed only for a loopback Core (the runtime refuses
  otherwise, before opening a socket). A LAN Core needs the pinned certificate
  fingerprint.
- A Core that does not answer is **not** NULL: it is a snapshot with
  `reachable: false` and `exit_code: 2`. NULL means out of memory.
- Blocking. Call it off the UI thread; a handle is used by one thread at a time.
- `wavr snapshot --json` on the command line prints the same document.

## Schema (version 1)

Canonical definition: `backend/wavr/client_view.py`. Answer key:
`conformance/client_view.json`, generated from it; the native runtime must
reproduce every case exactly.

```jsonc
{
  "schema": 1,
  "reachable": true,              // the Core gave a readable runtime answer
  "error": null,                  // transport sentence when it did not
  "exit_code": 0,                 // 0 nothing needs you, 1 something does / could not check, 2 no answer
  "runtime": {                    // null = no readable answer
    "state": "healthy",           // starting|healthy|updating|paused|degraded|attention|unavailable
    "headline": "…", "space": "Home", "role": "core",
    "uptime_s": 3600, "last_state_age_s": 4.5,
    "findings": [{"key": "sensors", "state": "healthy", "text": "…", "detail": null}]
  },
  "attention": {                  // null = could not be read (never "nothing needs you")
    "total": 1, "blocking": 0, "degraded": 1, "info": 0, "headline": "…",
    "could_not_check": [],
    "items": [{"key": "…", "band": "degraded", "title": "…", "detail": "…",
               "where": "kitchen", "action": "open", "since": "ISO-8601", "count": 1}]
  },
  "rooms": [                      // sorted by room id
    {"room": "office", "occupied": true, "confidence": 0.87, "person_count": 1,
     "precision_level": "position", "explanation": "…", "ts": "ISO-8601",
     "watch": false, "unrecognized": false,
     "sources": [{"modality": "mmwave", "sensor_id": "a", "presence": true,
                  "confidence": 0.9, "age_s": 3, "health": "fresh", "count": 1}]}
  ],
  "rooms_readable": true,
  "privacy": {"watch": false}
}
```

Every field may be `null` (unknown). A client shows unknown as unknown -- never
as `false`, `0` or "healthy". `person_count: null` is "not counted", not zero.
`precision_level` is an enum rung, not a percentage.

**Never in a snapshot:** per-person positions, identities, vitals. The view
model does not carry them at all, Watch on or off.

## SpaceScene

`GET /api/scene` (command `scene.get`) returns schema 1 Space geometry and
RoomState verdicts for native Space renderers (`backend/wavr/space_scene.py`):
sorted levels and rooms, polygons and centroids in house units, bounds keyed by
string level, unplaced room names (the house-level aggregate is not one), the
rooms an attention item names (from its `title_args.room`; an item's `where` is
a screen, not a room), and the attention sources that could not be read. An
absent RoomState has `occupied: null` and `confidence: null`; it does not mean
Empty. `watch` is `true` only where the Watch projection flagged the room.
Bounds for a level without a valid room polygon are `null`.

The route uses the same `presence:read` gate and Watch-projected RoomStates as
`GET /api/state`; a test proves a paired device without that scope is refused
both. It never carries per-person positions, identities or vitals; those are
live-only and consent-gated. Renderers draw from this projection and do not
compute occupancy. The current house document has no camera or sensor anchors,
so the scene does not invent any.

## Commands (the write side)

A native client never builds an HTTP request itself. Each action is one row of
`backend/wavr/client_commands.py` -- a name, the existing Core route it maps
to, and its arguments -- run by the native runtime (`wavr_command_run`, C ABI
1.2; `wavr command NAME --args JSON` on the command line). The runtime checks
only the SHAPE of the arguments and escapes each path argument as one segment;
the Core decides who may do what and whether a value is acceptable.

A reply is always `{ok, status, error, detail, data}`. `error` is one stable
word a client can switch on -- `bad_call` (the call was malformed and never left
the device), `unreachable`, `unauthorized`, `forbidden`, `not_found`,
`conflict`, `invalid`, `locked`, `throttled`, `refused`, `server` -- `detail`
is the Core's own sentence, and `data` is the Core's answer.

Onboarding commands (`pair.request`, `pair.status`, `pair.redeem`) are sent
without the token even when the client holds one. Joining a Space:
`wavr_probe` the Core's certificate and show it to the person; `pair.request`
over a connection pinned to that fingerprint (the reply's `cert_fingerprint`
must equal it); show the reply's `compare_code`, which the person at the Core
types to approve; poll `pair.status` until `approved`; keep the token and the pin.

Held by: `conformance/client_commands.json` (runtime vs Python),
`backend/tests/test_native_clients_speak_the_cores_language.py` (every row names
a real route; tokenless rows are exactly the onboarding routes) and
`native/tests/e2e_commands.py` (the whole write path against a real Core across
the LAN).

## Visual language

Colours and roles come from the design tokens (`design/tokens.json`,
`scripts/gen_tokens.py`): a runtime state maps to `state.<name>`; a client
never picks its own colour for a state.
