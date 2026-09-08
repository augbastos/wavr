# The frontend, and where things live

`frontend/index.html` is a shell: markup, styles, and the ordered list of
scripts that compose the product. Every feature lives in `frontend/js/`.

It was not always. Until the modularisation pass it was 18,847 lines, 783 KB of
it inline `<script>`, and one of those blocks was 481 KB — roughly 8,700 lines
carrying twenty-five unrelated product areas in a single scope. The practical
cost was not aesthetic: every question about any feature began with finding it
in a file too large to open, and every change to any feature touched the same
file as every other.

## Where a new feature goes

Find the row your work belongs to. If nothing fits, add a module — that is the
normal outcome for a genuinely new product area, not a failure of the table.

| Domain | Module | What it owns |
|---|---|---|
| Translation | `i18n.js`, `locale-pt.js`, `language.js` | The runtime, the Portuguese catalogue, the selector |
| Formatting | `format.js` | Dates, times and numbers in the reader's conventions |
| API | `api.js` | Every request to the Core: origin, CSRF header, JSON, bearer |
| Connection | `core-connection.js` | Reconnection UI, the DataProvider contract, mode selection, the capability manifest |
| Shared helpers | `shared.js` | Cross-surface date and confidence helpers |
| House indicator | `house-indicator.js` | Home/away, and the screen-reader summary of the map |
| System | `status-panel.js`, `control-plane.js` | `/api/status`, global and per-source on/off |
| Cameras | `cameras.js` | RTSP/ONVIF admin, PTZ, calibration |
| Core Panel lock | `core-lock.js` | The kiosk PIN |
| Narration | `narration.js` | Spoken/ambient narration settings |
| Network | `network.js`, `new-devices.js` | Inventory, alerts, the device taxonomy, "anything I don't recognise?" |
| House status | `house-status.js` | The unified "is everything OK?" signal |
| Trust & privacy | `transparency.js`, `trust.js` | What Wavr knows, and the trust screen |
| Pairing | `pairing.js` | Pairing panel, guest passes, approve-on-Core |
| Identity | `identity.js`, `known-presence.js`, `whoshome.js` | My devices, Bluetooth bonding, who is likely home |
| Peers & nodes | `peers.js`, `nodes.js` | Cross-instance pairing, headless sensor boxes |
| Admin | `core-settings.js`, `space-admin.js` | The screens that replace editing `.env` by hand; the Space model and its named places (anchors) |
| Connectors | `connectors.js` | The single outward-facing integration surface |
| Assistant | `assistant.js` | Engine picker, bounded ask, audit trail |
| Routines | `routines.js` | "When THIS, do THAT" |
| Space | `radar.js`, `housemap.js`, `house3d.js`, `render.js` | The map, its geometry, the 3D view, and the RoomState renderer |
| Shell chrome | `shell-nav.js`, `tooltips.js`, `pwa.js` | Navigation, context help, service-worker registration |
| Devices | `devices.js` | Detected / Catalog / Active |
| Feature surfacing | `features.js` | Privacy controls and the progressive-disclosure layers |
| Developer | `developer.js` | Developer mode, inert until enabled |
| Observability | `runtime.js` | The runtime-presence chip |
| Discovery | `discoveries.js` | The Discovery Inbox |
| Core Panel | `core-panel.js`, `whats-new.js` | The ambient home-status face; release notes |
| Onboarding | `wizard.js` | First-run setup |

## Four rules, and why each one exists

**1. Talk to the Core through `WavrAPI`.** Never `fetch(location.origin + …)`
by hand. `X-Wavr-Local` is the CSRF guard the backend checks on every non-shell
route; it was hand-written at 110 call sites, which is 110 copies of one
security decision. `api.js` composes it, the JSON content type, and the
companion bearer. It returns a plain `Promise<Response>`, so it changes nothing
about how you read the answer.

**2. These are CLASSIC scripts. Do not add `type="module"`.** Top-level
`var`/`function` become properties of `window`, and top-level
`let`/`const`/`class` go into the global lexical record every classic script
shares — which is how `MODE`, `confWord`, `roomOcc` and the `window.__wavr*`
hooks reach across files at all. One `type="module"` breaks every
cross-module reference at once, silently, at load.

**3. The order of the `<script>` tags is a contract.** Several modules chain
onto `window.__wavrRS`, `__wavrInventory` or `__wavrStatus` by wrapping
whatever handler is already installed, so the tag order IS the order those
renderers run in. Nothing throws if you sort the list alphabetically; the page
just renders differently, sometimes wrongly, with nothing to point at.
`backend/tests/test_shell_modules.py` writes the order out and fails on a
change, so making one is a deliberate act.

Function hoisting does **not** cross a `<script>` boundary. Inside the old
monolith, a line that ran at parse time could call something declared four
thousand lines below it. Across two files it cannot. If your module runs
anything at parse time, everything it calls must load earlier.

**4. A new module needs three edits, and the third is the one people forget.**

1. the file in `frontend/js/`, named lowercase with hyphens — the route serving
   `/js/{name}` refuses anything else;
2. a `<script src="js/…">` tag at the right position in `index.html`, plus its
   place in `EXPECTED_ORDER`;
3. **`SHELL` and `SHELL_PATHS` in `frontend/sw.js`.** `Cache.addAll` is
   all-or-nothing: a module the page loads and the worker does not precache
   does not degrade offline launch, it *removes* it — for every screen, not
   just yours. `test_sw_shell.py` compares the two lists and
   `test_offline_launch.py` cuts the network in a real browser and reloads.

No backend edit is needed: one route serves the whole directory.

## What is deliberately still large

`features.js` (114 KB) and `house3d.js` (110 KB) are each a single IIFE with
one shared closure. Splitting them means promoting closure state to globals —
trading encapsulation for a smaller file, which is a worse codebase measured by
anything except file size. They have clear ownership and a clear boundary; that
is what modularisation is for. If either grows a second, genuinely independent
concern, that concern comes out on its own.
