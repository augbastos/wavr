/**
 * Wavr Spatial SDK — JavaScript / TypeScript.
 *
 * What an application needs from Wavr, without needing to know what a modality
 * is, what fusion does, or which sensor answered.
 *
 *     import { WavrClient } from "./wavr.js";
 *
 *     const wavr = new WavrClient({ baseUrl: "https://192.168.1.10:8443" });
 *     await wavr.connect();
 *
 *     const kitchen = await wavr.context("kitchen");
 *     if (kitchen.can("count")) { ... }
 *     for (const why of kitchen.limitations) console.log(why);
 *
 *     const sub = wavr.subscribe(ev => { ... });
 *
 * ## Three things this SDK does that a fetch wrapper would not
 *
 * **It refuses to let `null` become `0`.** `occupancy` is `null` when nothing in
 * the room can count, and `context.occupancyKnown` exists so an application
 * never has to decide what a missing number means. A thin wrapper hands you
 * `undefined` and every app invents its own default — which is always `0`,
 * because `0` makes the rendering code simpler.
 *
 * **It reconnects without losing events.** The socket sends the timestamp of the
 * last event it saw, so a dropped Wi-Fi link costs latency, not information. A
 * reconnect that silently restarts from "now" is worse than no reconnect at all:
 * the app looks healthy and is quietly wrong about the room.
 *
 * **It notices when the Core is newer than the SDK.** `protocolAhead` is
 * surfaced rather than thrown on. Refusing outright would break every
 * application the day a Core updates; ignoring it would let a field whose
 * meaning changed pass straight through.
 *
 * ## How to load it
 *
 * Zero dependencies, zero build — but it is an ES MODULE. `import` it, or load
 * it with `<script type="module">`. It canNOT be loaded as a classic script:
 * the `export` statements below are a syntax error outside a module, so a plain
 * `<script src="wavr.js">` fails before one line of it runs.
 *
 * This paragraph used to claim both, in a repository whose own frontend is
 * dozens of classic scripts — which is precisely the reader who would have
 * tried it and got an empty page with one console error. Documenting the
 * capability was cheaper than building it and bought nothing: the three
 * reference experiences here all `import` the file, and making it classic-safe
 * would mean either dropping the exports they use or shipping a second build,
 * which is the build step this SDK exists without.
 *
 * The `window.Wavr` block at the end is therefore not a fallback. It publishes
 * the namespace once the module has run, so a page's own classic scripts and
 * the devtools console can reach the same classes without a second copy.
 *
 * Node 18+ has `fetch` and `WebSocket` built in.
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

/** The context/event shape this SDK was written against. */
export const PROTOCOL_VERSION = 2;

/** Reconnect backoff. Jittered so a house full of experiences reconnecting after
 * a router reboot does not arrive in lockstep — the same reasoning the Core
 * applies to its own sources. */
const RECONNECT_BASE_MS = 500;
const RECONNECT_CAP_MS = 30000;
const RECONNECT_JITTER = 0.25;

/** Failure kinds an application can branch on without parsing a message. */
export const ERROR_NETWORK = "network";
export const ERROR_AUTH = "auth";
export const ERROR_NOT_FOUND = "not_found";
export const ERROR_PROTOCOL = "protocol";
export const ERROR_SERVER = "server";

export class WavrError extends Error {
  constructor(kind, message, { status = 0, cause = null } = {}) {
    super(message);
    this.name = "WavrError";
    this.kind = kind;
    this.status = status;
    this.cause = cause;
  }
  /** Whether retrying the same call could plausibly succeed. Auth and
   * not-found will not fix themselves; a network blip might. */
  get retryable() {
    return this.kind === ERROR_NETWORK || this.status >= 500;
  }
}

/**
 * One room, as an application sees it.
 *
 * A class rather than the raw JSON so the two questions an app actually asks —
 * "can you do X here" and "what can't you tell me" — are methods instead of
 * conventions each app reimplements.
 */
export class RoomContext {
  constructor(raw) {
    this.raw = raw || {};
    this.room = this.raw.room || "";
    this.precision = this.raw.precision || "none";
    this.confidence = this.raw.confidence ?? 0;
    this.occupied = this.raw.occupied ?? null;
    /** `null` when nothing here can count. NEVER 0 — see the module docstring. */
    this.occupancy = this.raw.occupancy ?? null;
    this.occupancyKnown = this.raw.occupancy_known === true;
    this.capabilities = this.raw.capabilities || [];
    this.anchors = this.raw.anchors || [];
    this.devices = this.raw.devices || [];
    this.sensors = this.raw.sensors || [];
    /** Plain sentences naming what this room cannot answer right now. */
    this.limitations = this.raw.limitations || [];
  }
  can(capability) {
    return this.capabilities.includes(capability);
  }
  /** Anchors in this room, optionally by name. */
  anchor(name) {
    return name
      ? this.anchors.find((a) => a.name === name) || null
      : this.anchors;
  }
  /** Devices here that can show something. Only devices that SAID they have a
   * display — one that never reported is not counted as a no. */
  displays() {
    return this.devices.filter((d) => d.display === true);
  }
}

function backoffMs(attempt, rand = Math.random) {
  const nominal = Math.min(
    RECONNECT_BASE_MS * Math.pow(2, Math.max(0, attempt - 1)),
    RECONNECT_CAP_MS,
  );
  return Math.max(100, nominal * (1 + RECONNECT_JITTER * (2 * rand() - 1)));
}

/** A live subscription. Returned rather than an event-emitter mixin so closing
 * one is unambiguous — an app that forgets leaves a socket reconnecting for the
 * life of the page. */
class Subscription {
  constructor(client, handler, { onError = null, since = "" } = {}) {
    this._client = client;
    this._handler = handler;
    this._onError = onError;
    this._since = since;
    this._closed = false;
    this._attempt = 0;
    this._ws = null;
    this._timer = null;
    this._open();
  }

  /**
   * Open the socket, with a ticket when the caller has a token.
   *
   * The Core refuses every non-loopback WebSocket that does not carry a
   * redeemed single-use ticket, and this used to build the URL without one. So
   * `subscribe()` worked on the Core's own machine and failed on every phone,
   * tablet and third-party application on the LAN — which is every real
   * caller. The socket closed with 1008 on each attempt, the retry passed
   * `null` as the error, `_fail` therefore never ran and the `onError` the
   * reference pages register never fired: an experience that looked connected
   * and was permanently stale, with nothing anywhere saying why. All three
   * shipped reference pages broke exactly this way off-box.
   *
   * A ticket is SINGLE-USE, so one is minted per attempt rather than cached —
   * a reconnect that reuses a spent ticket is refused just like no ticket.
   *
   * Async, and it re-checks `_closed` after the await on purpose: an
   * application that calls `close()` while the mint is in flight must not end
   * up with a socket afterwards.
   */
  async _open() {
    if (this._closed) return;
    let ticket = "";
    if (this._client.token) {
      try {
        const body = await this._client._post("/api/ws-ticket", {});
        ticket = (body && body.ticket) || "";
      } catch (err) {
        // A refused mint is the refusal the socket would have given, arriving
        // earlier and carrying a reason. Report it and back off rather than
        // opening a socket that is certain to be closed.
        this._retry(err);
        return;
      }
    }
    if (this._closed) return;
    const url = this._client._wsUrl("/ws/events", this._since, ticket);
    let ws;
    try {
      ws = new WebSocket(url);
    } catch (err) {
      this._retry(err);
      return;
    }
    this._ws = ws;
    ws.onopen = () => {
      this._attempt = 0;
      this._everOpened = true;
    };
    ws.onmessage = (msg) => {
      // A frame arrived, so the connection demonstrably worked, whatever
      // `onopen` did or did not fire. `_everOpened` decides whether a later
      // close is an ordinary drop (quiet) or a Core that never let us in
      // (reported), and inferring it from the handshake alone would misreport
      // a working stream on any transport that does not raise `onopen`.
      this._everOpened = true;
      let ev;
      try {
        ev = JSON.parse(msg.data);
      } catch {
        return; // a frame that is not JSON is not an event
      }
      // Remembered BEFORE the handler runs: a handler that throws must not cost
      // us the position, or the next reconnect replays what was already seen.
      if (ev && ev.at) this._since = ev.at;
      try {
        this._handler(ev);
      } catch (err) {
        this._fail(new WavrError(ERROR_PROTOCOL, "event handler threw", { cause: err }));
      }
    };
    ws.onerror = () => {
      /* onclose follows; retrying there keeps one path */
    };
    ws.onclose = () => {
      this._ws = null;
      if (this._closed) return;
      // A close carries no error object, and passing `null` here meant a
      // socket the Core refuses on every single attempt reported nothing at
      // all — the loop just went round for ever. A DROPPED connection is
      // ordinary and stays quiet; never having connected is a configuration
      // problem the application has to be told about.
      this._retry(this._everOpened
        ? null
        : new WavrError(ERROR_NETWORK,
                        "the Core closed the event stream without opening it"));
    };
  }

  _retry(err) {
    if (err) {
      this._fail(err instanceof WavrError
        ? err
        : new WavrError(ERROR_NETWORK, String(err), { cause: err }));
    }
    this._attempt += 1;
    const wait = backoffMs(this._attempt);
    this._timer = setTimeout(() => this._open(), wait);
  }

  _fail(error) {
    if (this._onError) {
      try {
        this._onError(error);
      } catch {
        /* an error handler that throws must not kill the socket */
      }
    }
  }

  /** True while the socket is open. `false` during a reconnect — which is why
   * an application should render from its last known state rather than
   * blanking, exactly as the Core's own dashboard does. */
  get connected() {
    return !!this._ws && this._ws.readyState === 1;
  }

  close() {
    this._closed = true;
    if (this._timer) clearTimeout(this._timer);
    if (this._ws) this._ws.close();
    this._ws = null;
  }
}

export class WavrClient {
  /**
   * @param {object} opts
   * @param {string} [opts.baseUrl] Core address. Defaults to the page's own
   *   origin, which is right when the experience is served by Wavr itself.
   * @param {string} [opts.token] Bearer token for a paired credential.
   * @param {function} [opts.fetch] Injectable transport, for tests.
   */
  constructor({ baseUrl = "", token = "", fetch: fetchFn = null } = {}) {
    this.baseUrl = (baseUrl || _defaultOrigin()).replace(/\/+$/, "");
    this.token = token;
    this._fetch = fetchFn || _globalFetch();
    this.space = null;
    this.protocolVersion = null;
    /** True when the Core speaks a newer contract than this SDK. Surfaced, not
     * thrown: see the module docstring. */
    this.protocolAhead = false;
  }

  _headers() {
    const h = { Accept: "application/json" };
    if (this.token) h.Authorization = `Bearer ${this.token}`;
    // The Core's CSRF guard for same-origin browser calls. Harmless elsewhere.
    h["X-Wavr-Local"] = "1";
    return h;
  }

  /**
   * The stream URL, with the ticket in the query string.
   *
   * A browser cannot put a header on a WebSocket handshake, which is the whole
   * reason the Core accepts a single-use ticket here instead of the bearer
   * token every other call carries.
   */
  _wsUrl(path, since = "", ticket = "") {
    const base = this.baseUrl.replace(/^http/, "ws");
    const q = new URLSearchParams();
    if (since) q.set("since", since);
    if (ticket) q.set("ticket", ticket);
    const qs = q.toString();
    return `${base}${path}${qs ? "?" + qs : ""}`;
  }

  async _get(path) {
    let res;
    try {
      res = await this._fetch(`${this.baseUrl}${path}`, {
        headers: this._headers(),
      });
    } catch (err) {
      throw new WavrError(ERROR_NETWORK, `cannot reach Wavr at ${this.baseUrl}`, {
        cause: err,
      });
    }
    return this._body(res, path);
  }

  async _post(path, payload) {
    let res;
    try {
      res = await this._fetch(`${this.baseUrl}${path}`, {
        method: "POST",
        headers: { ...this._headers(), "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch (err) {
      throw new WavrError(ERROR_NETWORK, `cannot reach Wavr at ${this.baseUrl}`, {
        cause: err,
      });
    }
    return this._body(res, path);
  }

  async _body(res, path) {
    if (res.status === 401 || res.status === 403) {
      throw new WavrError(ERROR_AUTH, `not authorised for ${path}`, {
        status: res.status,
      });
    }
    if (res.status === 404) {
      throw new WavrError(ERROR_NOT_FOUND, `${path} not found`, { status: 404 });
    }
    if (!res.ok) {
      throw new WavrError(ERROR_SERVER, `Wavr returned ${res.status} for ${path}`, {
        status: res.status,
      });
    }
    try {
      return await res.json();
    } catch (err) {
      throw new WavrError(ERROR_PROTOCOL, `${path} did not return JSON`, {
        cause: err,
      });
    }
  }

  /**
   * Reach the Core, learn the Space, and check the contract version.
   *
   * Version negotiation happens here rather than lazily so an application finds
   * out at startup, where it can still decide what to do, instead of at the
   * moment it reads a field that changed meaning.
   */
  async connect() {
    const body = await this._get("/api/experience/context");
    this.space = body.space || null;
    this.protocolVersion = body.protocol_version ?? null;
    this.protocolAhead =
      typeof this.protocolVersion === "number" &&
      this.protocolVersion > PROTOCOL_VERSION;
    return this;
  }

  /** Every room's context at once. */
  async spaceContext() {
    return this._get("/api/experience/context");
  }

  /**
   * Room names in this Space — the rooms on its floor plan.
   *
   * Two things this list is not, and an application that assumes either will
   * misbehave on a real install:
   *
   * **It can be empty.** A Core with no floor plan drawn yet has no rooms, so
   * `rooms()` returns `[]` while the house is being sensed perfectly well.
   * That is a setup step, not a fault — render it as one.
   *
   * **It does not cover every room an event can name.** Wavr's LAN presence is
   * house-level and reports under a pseudo-room (`casa`) that belongs to no
   * floor, so it never appears here and `context("casa")` is a 404. Treat a
   * room from {@link subscribe} as a label to display, and look it up only
   * through this list.
   */
  async rooms() {
    const body = await this.spaceContext();
    return (body.rooms || []).map((r) => r.room);
  }

  /** One room's context, or the whole Space as `RoomContext[]`. */
  async context(room = "") {
    if (room) {
      return new RoomContext(
        await this._get(`/api/experience/context/${encodeURIComponent(room)}`),
      );
    }
    const body = await this.spaceContext();
    return (body.rooms || []).map((r) => new RoomContext(r));
  }

  /** Named places, optionally in one room. */
  async anchors(room = "") {
    const qs = room ? `?room=${encodeURIComponent(room)}` : "";
    const body = await this._get(`/api/anchors${qs}`);
    return body.anchors || [];
  }

  /** Which Wavr anchors an external system's id refers to. A list: two runtimes
   * can bind the same id, and picking one would hide the collision. */
  async resolveAnchor(providerId, externalId) {
    const body = await this._get(
      `/api/anchors/resolve/${encodeURIComponent(providerId)}/${encodeURIComponent(externalId)}`,
    );
    return body.anchors || [];
  }

  /** What each sensor can honestly observe, and which rooms nothing watches. */
  async coverage() {
    return this._get("/api/coverage");
  }

  /** Whether this Space can support an experience. Not permission — a person
   * grants that. */
  async compatibility(manifest, room = "") {
    return this._post("/api/experience/compatibility", { manifest, room });
  }

  /** The spatial scopes an experience may request. */
  async scopes() {
    return this._get("/api/experience/scopes");
  }

  /**
   * Open a session: a name for "this experience is running, in this room".
   *
   * Wavr will tell you when that room changes or when a screen appears in it.
   * It does NOT move your application's state anywhere — that is yours to
   * carry, and a platform that pretended otherwise would lose somebody's
   * half-finished form on the way to the television.
   */
  async openSession(experienceId, { room = "", metadata = null } = {}) {
    return this._post("/api/experience/sessions", {
      experience_id: experienceId,
      room,
      metadata,
    });
  }

  /**
   * Re-evaluate a session, and get back what changed.
   *
   * The handoff primitive. It reports that a display became available; whether
   * to offer "Continue on TV?" is your decision, because it depends on what
   * your experience IS — a recipe follows somebody to a screen, a private
   * message does not.
   */
  async observeSession(sessionId, { room = "" } = {}) {
    return this._post(
      `/api/experience/sessions/${encodeURIComponent(sessionId)}/observe`,
      { room },
    );
  }

  /** Add a device to a session. Not an authorization — a session grants
   * nothing, and checking pairing here would put a second, weaker gate beside
   * the real one. */
  async joinSession(sessionId, deviceId) {
    return this._post(
      `/api/experience/sessions/${encodeURIComponent(sessionId)}/devices`,
      { device_id: deviceId },
    );
  }

  async closeSession(sessionId) {
    let res;
    try {
      res = await this._fetch(
        `${this.baseUrl}/api/experience/sessions/${encodeURIComponent(sessionId)}`,
        { method: "DELETE", headers: this._headers() },
      );
    } catch (err) {
      throw new WavrError(ERROR_NETWORK, `cannot reach Wavr at ${this.baseUrl}`, {
        cause: err,
      });
    }
    return this._body(res, "/api/experience/sessions");
  }

  /**
   * Subscribe to semantic spatial events.
   *
   * Reconnects on its own, resuming from the last event seen. Pass
   * `{ room: "kitchen" }` to receive only that room's events — filtered here
   * rather than at the Core, because the stream is small and a per-subscriber
   * server filter is state the Core would have to keep correct forever.
   *
   * An event's `room` is not guaranteed to be one of {@link rooms}: Wavr's
   * house-level LAN presence reports under a pseudo-room (`casa`) that is on no
   * floor plan, and `context()` answers 404 for it. Feeding an event's room
   * straight back into `context()` therefore has to handle `ERROR_NOT_FOUND`.
   */
  subscribe(handler, { room = "", since = "", onError = null } = {}) {
    const filtered = room
      ? (ev) => {
          if (ev && ev.room === room) handler(ev);
        }
      : handler;
    return new Subscription(this, filtered, { onError, since });
  }
}

function _defaultOrigin() {
  if (typeof location !== "undefined" && location.origin) return location.origin;
  return "http://127.0.0.1:8000";
}

function _globalFetch() {
  if (typeof fetch === "function") return fetch.bind(globalThis);
  throw new WavrError(
    ERROR_NETWORK,
    "no fetch available — pass one via { fetch } (Node 18+ has it built in)",
  );
}

// The namespace, published for a page's classic scripts and for the devtools
// console once this module has run. NOT a classic-script entry point — see the
// loading note at the top: `export` above makes that impossible in one file.
if (typeof window !== "undefined") {
  window.Wavr = {
    WavrClient,
    RoomContext,
    WavrError,
    PROTOCOL_VERSION,
    ERROR_NETWORK,
    ERROR_AUTH,
    ERROR_NOT_FOUND,
    ERROR_PROTOCOL,
    ERROR_SERVER,
  };
}
