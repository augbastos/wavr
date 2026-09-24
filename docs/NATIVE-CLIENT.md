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

## Visual language

Colours and roles come from the design tokens (`design/tokens.json`,
`scripts/gen_tokens.py`): a runtime state maps to `state.<name>`; a client
never picks its own colour for a state.
