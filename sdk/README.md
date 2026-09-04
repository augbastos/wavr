# Wavr Spatial SDKs

Three languages, one contract. Everything below is read from a running Core; none
of it needs a build step, an account or a package registry.

```
sdk/javascript/wavr.js          browser + Node 18+, zero dependencies
sdk/python/wavr_sdk/            standard library only
dev.wavr.sdk                    Kotlin, inside the Android app that uses it
```

---

## The thing all three exist to prevent

`occupancy` is **nullable, all the way to the call site.**

```js
if (kitchen.occupancyKnown) show(kitchen.occupancy);   // a number
else                        show("someone is here");   // NOT "0 people"
```

`null` means nothing in that room can count. It does not mean nobody is there,
and a thin HTTP wrapper hands you `undefined` and lets every application invent
its own default — which is always `0`, because `0` is what makes the next line of
code compile.

Kotlin enforces it with the type system; JavaScript and Python carry an explicit
`occupancyKnown` / `occupancy_known` beside the value so there is nothing to
remember.

The same rule runs through everything: a device that never reported a screen has
`display: null`, which is not `false`. An anchor with no coordinates is a
complete anchor. A room with no sensor is unobserved, not empty.

---

## JavaScript / TypeScript

```html
<script type="module">
  import { WavrClient } from "/sdk/javascript/wavr.js";

  const wavr = new WavrClient();           // same origin, if the Core serves you
  await wavr.connect();

  const kitchen = await wavr.context("kitchen");
  console.log(kitchen.capabilities);       // ["presence", "count"]
  kitchen.limitations.forEach(console.log);

  const sub = wavr.subscribe((ev) => render(ev), { room: "kitchen" });
</script>
```

Types come from the hand-written `wavr.d.ts` beside it. It is hand-written on
purpose: this SDK is zero-build, and generating types would mean the file a
developer reads is no longer the file that runs.

**`subscribe` reconnects and resumes.** It remembers the last event's timestamp
and asks for everything after it, so a dropped Wi-Fi link costs latency rather
than information. A reconnect that silently restarted from *now* would be worse
than none: the application looks healthy and is quietly wrong about the room.

`sub.connected` goes `false` during a reconnect. Render from your last known
state rather than blanking — the Core's own dashboard does the same.

**Failures are typed.** `err.kind` is one of `network`, `auth`, `not_found`,
`protocol`, `server`, and `err.retryable` says whether trying again could
plausibly help. A token does not fix itself by retrying.

```
node --test sdk/javascript/test.mjs
```

---

## Python

```python
from wavr_sdk import Wavr

wavr = Wavr("https://192.168.1.10:8443", token="...").connect()

for room in wavr.context():
    print(room.room, room.capabilities)
    for why in room.limitations:
        print("  but:", why)

for event in wavr.events():          # blocks, reconnects, resumes
    print(event["event"], event["room"])
```

Deliberately **not** part of the `wavr` server package. An SDK that requires
installing a Core is not an SDK; somebody writing a script to log occupancy
should not end up with FastAPI, OpenCV and a fusion engine on their machine. Its
CI job installs pytest and nothing else — if that ever needs the Core, the SDK
has acquired a dependency on the server and that is the thing to look at.

`events()` **polls** the Core's event tail every couple of seconds rather than
holding a socket open. A deliberate trade: the tools this serves — scripts,
automation, agents — are not harmed by two seconds, and a stdlib-only install is
worth more to them than the latency. The Core does expose a real push stream at
`/ws/events`; the JavaScript SDK uses it, because an interactive experience
genuinely needs it.

⚠️ `verify_tls=False` exists because a home Core serves a self-signed certificate
over the LAN. Understand what it costs: without verification anything on the
network can impersonate the Core, and this connection carries where people are in
the house. Trusting the Core's certificate once — `ssl.create_default_context(
cafile=...)` through your own opener — is the better fix.

```
python -m pytest sdk/python/tests -q
```

---

## Kotlin / Android

```kotlin
val wavr = WavrClient("http://127.0.0.1:8000")
val kitchen = wavr.context("kitchen")

if (kitchen.can(Capability.COUNT)) {
    kitchen.occupancy?.let { show(it) }     // the compiler makes you handle null
}
```

Lives in the Android app that hosts the Core, and that app **uses it** —
`CoreService`'s permanent notification says what the Core can currently see,
read through this SDK over loopback. An SDK nothing in the repository calls is a
proposal rather than a component, and its API drifts away from the Core's
between the two releases where nobody notices.

Blocking by design. Callers are expected to be on a background thread or an IO
dispatcher; hiding that behind a callback would move the same requirement
somewhere less visible.

Failures are a sealed hierarchy: `WavrException.Auth` is a settings screen and
`WavrException.Unreachable` is a retry, and code that catches `Exception` treats
them identically.

```
cd core-launcher && ./gradlew :app:testDebugUnitTest
```

---

## What every SDK can reach

| | |
|---|---|
| `connect()` | the Space, and the contract version |
| `context()` / `context(room)` | capabilities, occupancy, anchors, devices, **limitations** |
| `anchors()` / `resolveAnchor()` | named places; an external id resolves to a LIST |
| `coverage()` | what each sensor can honestly observe |
| `compatibility(manifest)` | FULLY / PARTIALLY / UNSUPPORTED, with reasons and fallbacks |
| `openSession()` / `observeSession()` | a running experience, and when its room or targets change |
| `subscribe()` / `events()` | the semantic event stream |

### Version negotiation

`connect()` reads the Core's `protocol_version`. If the Core is **newer** than the
SDK, `protocolAhead` is set and nothing is thrown. Refusing would break every
installed application the day somebody updates their Core; ignoring it would let
a field whose meaning changed pass straight through. Surfacing it lets the
application decide, which is the only party that can.

### What no SDK will ever return

**Who.** There is no spatial scope that grants identity, `/api/experience/scopes`
publishes the whole vocabulary so the absence is visible rather than merely true,
and a context carries occupancy counts and device roles — never a name.

An experience that can name the people in a room is a different product with a
different consent conversation attached.

---

## Developing without the sensors

Turn on **Developer mode** in Core settings and run a scenario:

```
POST /api/dev/scenarios/count_appears_and_vanishes/run  {"realtime": true}
```

Eight scenarios, each one a situation that breaks naive code — a headcount that
becomes unavailable, two sensors contradicting each other, a room that empties
only after the 45-second dwell. They run into the real fusion engine in **your**
rooms, and every event they produce carries a `sim:` sensor id that stays visible
all the way to the dashboard.

The reference applications under `experiences/` use these SDKs against a real
Core. Read their source; that is what they are for.
