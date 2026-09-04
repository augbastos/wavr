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
 * Zero dependencies, zero build. Works as an ES module and as a classic script
 * (it defines `window.Wavr`). Node 18+ has `fetch` and `WebSocket` built in.
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

/** The context/event shape this SDK was written against. */
export const PROTOCOL_VERSION = 1;

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

  _open() {
    if (this._closed) return;
    const url = this._client._wsUrl("/ws/events", this._since);
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
    };
    ws.onmessage = (msg) => {
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
      if (!this._closed) this._retry(null);
    };
  }

  _retry(err) {
    if (err) this._fail(new WavrError(ERROR_NETWORK, String(err), { cause: err }));
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

  _wsUrl(path, since = "") {
    const base = this.baseUrl.replace(/^http/, "ws");
    const q = new URLSearchParams();
    if (since) q.set("since", since);
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

  /** Room names in this Space. */
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

// Classic-script use, matching the frontend's own zero-build idiom.
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
