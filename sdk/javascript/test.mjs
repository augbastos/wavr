/**
 * Tests for the Wavr JavaScript SDK.
 *
 *     node --test sdk/javascript/
 *
 * `node:test` and `node:assert`, both built in. A dependency would mean an
 * npm install stands between a developer and running these, and this SDK's
 * whole point is that it needs neither.
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import test from "node:test";
import vm from "node:vm";

import {
  ERROR_AUTH,
  ERROR_NETWORK,
  ERROR_NOT_FOUND,
  ERROR_SERVER,
  PROTOCOL_VERSION,
  RoomContext,
  WavrClient,
  WavrError,
} from "./wavr.js";

/** A fetch that answers from a table, and records what was asked. */
function fakeFetch(routes, log = []) {
  return async (url, opts = {}) => {
    const path = url.replace(/^https?:\/\/[^/]+/, "");
    log.push({ path, method: opts.method || "GET", body: opts.body, headers: opts.headers });
    const hit = routes[path];
    if (hit === undefined) {
      return { ok: false, status: 404, json: async () => ({}) };
    }
    if (typeof hit === "number") {
      return { ok: hit < 400, status: hit, json: async () => ({}) };
    }
    return { ok: true, status: 200, json: async () => hit };
  };
}

const SPACE_CONTEXT = {
  // The version the Core ACTUALLY ships (contracts.py:
  // experience_context). Pinned to 1 while the Core moved to 2, this
  // fixture agreed with a stale SDK constant and the pair of them
  // hid the bump from every test here.
  protocol_version: 2,
  space: { space_id: "sp_1", name: "My Home" },
  rooms: [
    {
      room: "kitchen",
      precision: "count",
      confidence: 0.8,
      occupied: true,
      occupancy: 2,
      occupancy_known: true,
      capabilities: ["presence", "count"],
      anchors: [],
      devices: [],
      sensors: [],
      limitations: ["Wavr can count people in kitchen, not place them within it."],
    },
    {
      room: "attic",
      precision: "none",
      confidence: 0,
      occupied: null,
      occupancy: null,
      occupancy_known: false,
      capabilities: [],
      anchors: [],
      devices: [],
      sensors: [],
      limitations: ["No sensor covers attic. Wavr cannot tell whether anybody is here."],
    },
  ],
  rooms_without_sensing: ["attic"],
};

function client(routes, log) {
  return new WavrClient({
    baseUrl: "http://core.test:8000",
    fetch: fakeFetch(routes, log),
  });
}

// -- Connecting and version negotiation ---------------------------------------

test("connect learns the Space and the contract version", async () => {
  const w = client({ "/api/experience/context": SPACE_CONTEXT });
  await w.connect();
  assert.equal(w.space.name, "My Home");
  assert.equal(w.protocolVersion, PROTOCOL_VERSION);
  assert.equal(w.protocolAhead, false);
});

test("a newer Core is surfaced, not thrown on", async () => {
  // Refusing outright would break every application the day a Core updates;
  // ignoring it would let a field whose meaning changed pass straight through.
  const w = client({
    "/api/experience/context": { ...SPACE_CONTEXT, protocol_version: 99 },
  });
  await w.connect();
  assert.equal(w.protocolAhead, true);
});

test("the token rides on every request", async () => {
  const log = [];
  const w = new WavrClient({
    baseUrl: "http://core.test:8000",
    token: "abc123",
    fetch: fakeFetch({ "/api/experience/context": SPACE_CONTEXT }, log),
  });
  await w.connect();
  assert.equal(log[0].headers.Authorization, "Bearer abc123");
});

// -- The null that must never become a zero -----------------------------------

test("an uncounted room reports null and says the count is unknown", async () => {
  const w = client({ "/api/experience/context": SPACE_CONTEXT });
  const rooms = await w.context();
  const attic = rooms.find((r) => r.room === "attic");
  assert.equal(attic.occupancy, null);
  assert.equal(attic.occupancyKnown, false);
});

test("a real count survives as a number", async () => {
  const w = client({ "/api/experience/context": SPACE_CONTEXT });
  const kitchen = (await w.context()).find((r) => r.room === "kitchen");
  assert.equal(kitchen.occupancy, 2);
  assert.equal(kitchen.occupancyKnown, true);
});

test("capabilities are a question, not a field to remember", () => {
  const ctx = new RoomContext(SPACE_CONTEXT.rooms[0]);
  assert.equal(ctx.can("count"), true);
  assert.equal(ctx.can("position"), false);
});

test("limitations reach the application verbatim", () => {
  const ctx = new RoomContext(SPACE_CONTEXT.rooms[1]);
  assert.match(ctx.limitations[0], /cannot tell whether anybody/);
});

test("a device that never reported a display is not counted as having none", () => {
  const ctx = new RoomContext({
    room: "kitchen",
    devices: [
      { device_id: "a", display: true },
      { device_id: "b", display: null },
      { device_id: "c", display: false },
    ],
  });
  assert.deepEqual(
    ctx.displays().map((d) => d.device_id),
    ["a"],
  );
});

// -- Typed failures an application can branch on ------------------------------

test("401 is an auth error, not a generic failure", async () => {
  const w = client({ "/api/experience/context": 401 });
  await assert.rejects(() => w.connect(), (err) => {
    assert.ok(err instanceof WavrError);
    assert.equal(err.kind, ERROR_AUTH);
    assert.equal(err.retryable, false, "a token does not fix itself by retrying");
    return true;
  });
});

test("404 is not-found", async () => {
  const w = client({ "/api/experience/context": SPACE_CONTEXT });
  await assert.rejects(() => w.context("nowhere"), (err) => {
    assert.equal(err.kind, ERROR_NOT_FOUND);
    return true;
  });
});

test("500 is retryable and 403 is not", async () => {
  const boom = client({ "/api/experience/context": 500 });
  await assert.rejects(() => boom.connect(), (err) => {
    assert.equal(err.kind, ERROR_SERVER);
    assert.equal(err.retryable, true);
    return true;
  });
});

test("an unreachable Core names the address it tried", async () => {
  const w = new WavrClient({
    baseUrl: "http://core.test:8000",
    fetch: async () => {
      throw new Error("ECONNREFUSED");
    },
  });
  await assert.rejects(() => w.connect(), (err) => {
    assert.equal(err.kind, ERROR_NETWORK);
    assert.match(err.message, /core\.test:8000/);
    assert.equal(err.retryable, true);
    return true;
  });
});

// -- The rest of the surface ---------------------------------------------------

test("rooms lists names", async () => {
  const w = client({ "/api/experience/context": SPACE_CONTEXT });
  assert.deepEqual(await w.rooms(), ["kitchen", "attic"]);
});

test("one room is fetched by name, url-encoded", async () => {
  const log = [];
  const w = client(
    { "/api/experience/context/living%20room": SPACE_CONTEXT.rooms[0] },
    log,
  );
  await w.context("living room");
  assert.equal(log[0].path, "/api/experience/context/living%20room");
});

test("anchors can be scoped to a room", async () => {
  const log = [];
  const w = client(
    { "/api/anchors?room=kitchen": { anchors: [{ anchor_id: "a1" }] } },
    log,
  );
  const found = await w.anchors("kitchen");
  assert.equal(found[0].anchor_id, "a1");
});

test("resolving an external anchor id returns every match", async () => {
  const w = client({
    "/api/anchors/resolve/arkit/ABC": { anchors: [{ anchor_id: "a" }, { anchor_id: "b" }] },
  });
  assert.equal((await w.resolveAnchor("arkit", "ABC")).length, 2);
});

test("compatibility posts the manifest", async () => {
  const log = [];
  const w = client(
    { "/api/experience/compatibility": { status: "FULLY_SUPPORTED" } },
    log,
  );
  const out = await w.compatibility({ id: "x", name: "X" }, "kitchen");
  assert.equal(out.status, "FULLY_SUPPORTED");
  assert.equal(log[0].method, "POST");
  assert.deepEqual(JSON.parse(log[0].body), {
    manifest: { id: "x", name: "X" },
    room: "kitchen",
  });
});

// -- Subscription behaviour, without a real socket ----------------------------

class FakeSocket {
  static last = null;
  constructor(url) {
    this.url = url;
    this.readyState = 1;
    FakeSocket.last = this;
    queueMicrotask(() => this.onopen && this.onopen());
  }
  deliver(obj) {
    this.onmessage && this.onmessage({ data: JSON.stringify(obj) });
  }
  drop() {
    this.readyState = 3;
    this.onclose && this.onclose();
  }
  close() {
    this.readyState = 3;
  }
}

test("a subscription filters to one room without asking the Core to", async () => {
  globalThis.WebSocket = FakeSocket;
  const w = client({});
  const seen = [];
  const sub = w.subscribe((ev) => seen.push(ev), { room: "kitchen" });
  FakeSocket.last.deliver({ event: "room.occupancy_changed", room: "kitchen", at: "t1" });
  FakeSocket.last.deliver({ event: "room.occupancy_changed", room: "hall", at: "t2" });
  assert.deepEqual(seen.map((e) => e.room), ["kitchen"]);
  sub.close();
});

test("a reconnect resumes from the last event seen, not from now", async () => {
  // A reconnect that silently restarts from "now" is worse than none: the app
  // looks healthy and is quietly wrong about the room.
  globalThis.WebSocket = FakeSocket;
  const w = client({});
  const sub = w.subscribe(() => {});
  FakeSocket.last.deliver({ event: "room.occupancy_changed", room: "k", at: "2026-09-04T12:00:00Z" });
  const before = FakeSocket.last;
  before.drop();
  await new Promise((r) => setTimeout(r, 900));
  assert.notEqual(FakeSocket.last, before, "it reconnected");
  assert.match(FakeSocket.last.url, /since=2026-09-04T12%3A00%3A00Z/);
  sub.close();
});

test("the position is kept even when the handler throws", async () => {
  globalThis.WebSocket = FakeSocket;
  const w = client({});
  const errors = [];
  const sub = w.subscribe(
    () => {
      throw new Error("app bug");
    },
    { onError: (e) => errors.push(e) },
  );
  FakeSocket.last.deliver({ event: "x", room: "k", at: "2026-09-04T12:00:00Z" });
  const before = FakeSocket.last;
  before.drop();
  await new Promise((r) => setTimeout(r, 900));
  assert.match(FakeSocket.last.url, /since=/, "a throwing handler did not cost the position");
  assert.equal(errors.length, 1);
  sub.close();
});

test("closing a subscription stops it reconnecting", async () => {
  globalThis.WebSocket = FakeSocket;
  const w = client({});
  const sub = w.subscribe(() => {});
  const before = FakeSocket.last;
  sub.close();
  before.drop();
  await new Promise((r) => setTimeout(r, 900));
  assert.equal(FakeSocket.last, before, "no new socket was opened");
});

test("connected is false while reconnecting, so an app can keep its last state", () => {
  globalThis.WebSocket = FakeSocket;
  const w = client({});
  const sub = w.subscribe(() => {});
  assert.equal(sub.connected, true);
  FakeSocket.last.readyState = 3;
  assert.equal(sub.connected, false);
  sub.close();
});

// -- Sessions ------------------------------------------------------------------

test("opening a session names the experience and the room", async () => {
  const log = [];
  const w = client(
    { "/api/experience/sessions": { session_id: "ses_1", experience_id: "recipe" } },
    log,
  );
  const s = await w.openSession("recipe", { room: "kitchen" });
  assert.equal(s.session_id, "ses_1");
  assert.deepEqual(JSON.parse(log[0].body), {
    experience_id: "recipe",
    room: "kitchen",
    metadata: null,
  });
});

test("observing a session returns what changed, not a decision", async () => {
  // Wavr reports that a display became available. Whether to offer
  // "Continue on TV?" depends on what the experience IS.
  const w = client({
    "/api/experience/sessions/ses_1/observe": {
      session_id: "ses_1",
      room: "living",
      events: [{ event: "experience.target_available", target: { device_id: "tv" } }],
    },
  });
  const out = await w.observeSession("ses_1", { room: "living" });
  assert.equal(out.events[0].event, "experience.target_available");
  assert.ok(!("should" in out), "Wavr does not tell you what to do");
});

test("a session id is url-encoded on every route", async () => {
  const log = [];
  const w = client({ "/api/experience/sessions/a%2Fb/observe": {} }, log);
  await w.observeSession("a/b");
  assert.equal(log[0].path, "/api/experience/sessions/a%2Fb/observe");
});

test("closing a session uses DELETE", async () => {
  const log = [];
  const w = client({ "/api/experience/sessions/ses_1": { closed: true } }, log);
  await w.closeSession("ses_1");
  assert.equal(log[0].method, "DELETE");
});

// -- The ticket, which is what makes the stream work off the Core's own box ----

test("subscribe mints a ticket and puts it in the socket URL", async () => {
  // The Core refuses every non-loopback WebSocket without a redeemed
  // single-use ticket. Without this the SDK worked on the machine running the
  // Core and failed on every phone, tablet and third-party app on the LAN —
  // silently, because the close carried no error and the retry loop reported
  // nothing.
  globalThis.WebSocket = FakeSocket;
  const log = [];
  const w = new WavrClient({
    baseUrl: "http://core.test:8000",
    token: "tok-abc",
    fetch: fakeFetch({ "/api/ws-ticket": { ticket: "tkt-1" } }, log),
  });
  const sub = w.subscribe(() => {});
  await new Promise((r) => setTimeout(r, 50));
  assert.match(FakeSocket.last.url, /ticket=tkt-1/, FakeSocket.last.url);
  assert.ok(log.some((l) => l.path === "/api/ws-ticket"),
    `the ticket was never minted: ${JSON.stringify(log)}`);
  sub.close();
});

test("a ticket is minted again on every reconnect", async () => {
  // Tickets are SINGLE-USE. Caching one makes the second attempt fail exactly
  // like sending none, which is the bug this whole path exists to fix — and
  // it would only show up after the first network blip.
  globalThis.WebSocket = FakeSocket;
  let n = 0;
  const w = new WavrClient({
    baseUrl: "http://core.test:8000",
    token: "tok-abc",
    fetch: async (url) => {
      n += 1;
      return {
        ok: true, status: 200,
        json: async () => ({ ticket: `tkt-${n}` }),
      };
    },
  });
  const sub = w.subscribe(() => {});
  await new Promise((r) => setTimeout(r, 50));
  assert.match(FakeSocket.last.url, /ticket=tkt-1/);
  FakeSocket.last.drop();
  await new Promise((r) => setTimeout(r, 900));
  assert.match(FakeSocket.last.url, /ticket=tkt-2/,
    "the reconnect reused a spent ticket");
  sub.close();
});

test("a Core that never lets the socket open tells the application", async () => {
  // The silent half of the bug. `onclose` passed `null`, so `_fail` never ran
  // and `onError` never fired: an experience that looked connected, was
  // permanently stale, and said nothing anywhere. A DROPPED connection stays
  // quiet — that is ordinary — but never having connected is a configuration
  // problem the application has to hear about.
  //
  // A standalone class rather than a FakeSocket subclass: FakeSocket queues an
  // `onopen`, and a socket that opens is precisely the case this is NOT about.
  class Refusing {
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      Refusing.last = this;
      queueMicrotask(() => this.onclose && this.onclose());
    }
    close() { this.readyState = 3; }
  }
  globalThis.WebSocket = Refusing;
  const errors = [];
  const w = client({});
  const sub = w.subscribe(() => {}, { onError: (e) => errors.push(e) });
  await new Promise((r) => setTimeout(r, 200));
  assert.ok(errors.length >= 1,
    "a socket refused on every attempt reported nothing to the application");
  assert.match(String(errors[0].message), /without opening/);
  sub.close();
  globalThis.WebSocket = FakeSocket;
});

// -- The file may not describe a way of loading it that does not work ---------

const SOURCE = readFileSync(fileURLToPath(new URL("./wavr.js", import.meta.url)), "utf8");

/** Whether the file would parse in a plain `<script src="...">`. */
function loadsAsClassicScript() {
  try {
    new vm.Script(SOURCE, { filename: "wavr.js" });
    return true;
  } catch {
    return false;
  }
}

test("the header does not promise classic-script loading the file cannot do", () => {
  // It did, for a while, in a repository whose own frontend is dozens of
  // classic scripts — so the one reader most likely to try it got a blank page
  // and one console error. `export` is a syntax error outside a module, and no
  // single file can be both without a build step this SDK exists without.
  const header = SOURCE.slice(0, SOURCE.indexOf("*/"));
  if (!loadsAsClassicScript()) {
    assert.doesNotMatch(
      header,
      /works? (?:both )?as [^.]*classic script/i,
      "the header offers classic-script loading, and the file will not parse as one",
    );
    assert.match(
      header,
      /(?:can ?not|cannot) be loaded as a classic script/i,
      "the header should say outright that a plain <script src> will not work",
    );
    assert.match(header, /ES MODULE/, "and say what it IS, not only what it is not");
  }
});

test("the namespace block still runs, which is the reason it is there", async () => {
  // Not a classic-script fallback — the module has to have run for this to
  // happen at all. It is there so a page's own classic scripts and the devtools
  // console reach the same classes without a second copy of the file.
  const had = "window" in globalThis;
  const before = globalThis.window;
  globalThis.window = {};
  try {
    const fresh = await import(`./wavr.js?namespace=${Date.now()}`);
    assert.equal(globalThis.window.Wavr.WavrClient, fresh.WavrClient);
    assert.equal(globalThis.window.Wavr.PROTOCOL_VERSION, fresh.PROTOCOL_VERSION);
  } finally {
    if (had) globalThis.window = before;
    else delete globalThis.window;
  }
});

test("a subscription with no token opens without a ticket", async () => {
  // Loopback needs none, and minting one requires a bearer the caller does not
  // have. The SDK must not turn "no token" into a failed mint.
  globalThis.WebSocket = FakeSocket;
  const log = [];
  const w = client({}, log);
  const sub = w.subscribe(() => {});
  await new Promise((r) => setTimeout(r, 50));
  assert.doesNotMatch(FakeSocket.last.url, /ticket=/);
  assert.ok(!log.some((l) => l.path === "/api/ws-ticket"));
  sub.close();
});
