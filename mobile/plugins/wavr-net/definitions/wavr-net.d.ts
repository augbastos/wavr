/**
 * WavrNet — CONTRACT B, v1 (FROZEN 2026-07-05).
 *
 * The app's single native network choke point: pinned self-signed HTTPS + WSS to
 * the user's LAN central, failing closed on any certificate change. The shim (and
 * nothing else) talks to it. Android implements it today (WavrNetPlugin.kt +
 * PinnedClient.kt, OkHttp); the future iOS side (URLSession challenge-delegate
 * pinning + URLSessionWebSocketTask) MUST satisfy this exact surface — any change
 * here is a breaking contract change and must be changelogged.
 *
 * Registration line the shim uses (no bundler; plain script):
 *
 *   const WavrNet = Capacitor.registerPlugin("WavrNet");
 *
 * Fingerprint format everywhere in this contract: SHA-256 of the DER certificate,
 * UPPERCASE HEX, COLON-SEPARATED — e.g. "AB:CD:...". This is byte-for-byte the
 * backend's `wavr.tls.cert_fingerprint()` and what the loopback dashboard's
 * pairing panel displays. Implementations normalize before comparing (strip ":"
 * and whitespace, uppercase), so passing a bare-hex pin also works — but the
 * COLON-SEPARATED form is what plugin methods RETURN.
 *
 * Error model: rejected calls carry a machine code. With Capacitor's JS bridge the
 * rejection is a CapacitorException-like object: `err.code` is the code below and
 * `err.data` (when present) carries extras — for "PIN_MISMATCH", `err.data.presentedFp`
 * is a best-effort read of the certificate the server is NOW presenting, so the
 * shim can render old-vs-new on the hard-fail screen. There is NO silent re-pin:
 * the plugin never stores fingerprints; trust is whatever `pinnedFp` the caller
 * passes on each call.
 */

/** Matches @capacitor/core's PluginListenerHandle (declared locally so this file
 *  has zero dependencies — the shim is plain JS, no bundler). */
export interface PluginListenerHandle {
  remove(): Promise<void>;
}

/** Machine-readable rejection codes (err.code). */
export type WavrNetErrorCode =
  /** The server presented a certificate whose SHA-256 differs from pinnedFp.
   *  Hard fail; err.data.presentedFp / event.presentedFp shows the new cert's
   *  fingerprint when it could be re-read. The shim must show the
   *  "certificate changed" screen — never retry silently, never re-pin. */
  | 'PIN_MISMATCH'
  /** Any other transport failure (unreachable, timeout, reset, non-pin TLS
   *  protocol failure). Retryable at the shim's discretion. */
  | 'NETWORK'
  /** Missing/malformed parameters — including a non-https/wss URL (cleartext is
   *  refused) or a pinnedFp that is not 64 hex chars after normalization. */
  | 'INVALID_ARGS'
  /** sendSocket() to a socketId that is not open. */
  | 'UNKNOWN_SOCKET'
  /** sendSocket() on a closing socket or over the outgoing buffer limit. */
  | 'SEND_FAILED';

export interface WavrNetRequestOptions {
  /** Absolute https:// URL on the paired central (the app's only network peer). */
  url: string;
  /** HTTP method; default "GET". */
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE' | 'HEAD';
  /** Request headers, e.g. { Authorization: "Bearer <token>", "Content-Type":
   *  "application/json" }. Never logged by the native side. */
  headers?: Record<string, string>;
  /** Request body as a string (the Wavr API is JSON-over-string). */
  body?: string;
  /** The SHA-256 fingerprint captured at pairing time. THE trust anchor for this
   *  call: only a server presenting exactly this certificate is spoken to. */
  pinnedFp: string;
}

export interface WavrNetResponse {
  /** HTTP status code (4xx/5xx RESOLVE — only transport/pin failures reject). */
  status: number;
  /** Response headers; multi-valued headers are joined with ", ". */
  headers: Record<string, string>;
  /** Response body decoded as text. */
  body: string;
}

export interface WavrNetOpenSocketOptions {
  /** Absolute wss:// URL on the paired central, e.g.
   *  "wss://192.168.1.10:8443/ws/live?ticket=..." — the single-use ticket from
   *  POST /api/ws-ticket rides in the query string (a Bearer header cannot ride
   *  a WebSocket handshake). No Origin header is set; the central's LAN
   *  companion path does not require one. */
  url: string;
  /** Same pinning semantics as request(); the WSS handshake fails closed on any
   *  other certificate (rejects AND emits wavrNetError "PIN_MISMATCH"). */
  pinnedFp: string;
  /** Optional extra handshake headers. Never logged. */
  headers?: Record<string, string>;
}

/** wavrNetMessage — one incoming frame (the central's RoomState JSON as text). */
export interface WavrNetMessageEvent {
  socketId: string;
  data: string;
}

/** wavrNetClose — the socket closed (clean close, or code 1006 after an error). */
export interface WavrNetCloseEvent {
  socketId: string;
  code: number;
  reason: string;
}

/** wavrNetError — the socket failed. code "PIN_MISMATCH" means the WSS handshake
 *  saw a non-pinned certificate; presentedFp (best-effort) is that cert's
 *  fingerprint. `message` is an exception class name only — the native side never
 *  puts URLs, headers, or tokens in event payloads or logs. */
export interface WavrNetErrorEvent {
  socketId: string;
  code: WavrNetErrorCode;
  message: string;
  presentedFp?: string;
}

export interface WavrNetPlugin {
  /**
   * Read the SHA-256 fingerprint of the leaf certificate the server at `url`
   * presents, WITHOUT trusting it and WITHOUT sending any application data
   * (native side performs a bare TLS handshake — no HTTP is spoken — then
   * closes). For out-of-band human verification at pairing time against the
   * fingerprint on the central's trusted loopback dashboard. Calling probe()
   * establishes NO trust and stores nothing.
   */
  probe(options: { url: string }): Promise<{ fingerprint: string }>;

  /**
   * Pinned HTTPS request to the central. Rejects with code "PIN_MISMATCH"
   * (+ err.data.presentedFp when readable) if the server's certificate is not
   * exactly the pinned one; "NETWORK" for other transport failures. HTTP error
   * statuses (401/403/...) RESOLVE normally — the shim's fetch adapter decides.
   */
  request(options: WavrNetRequestOptions): Promise<WavrNetResponse>;

  /**
   * Open a pinned native WSS socket. Resolves with {socketId} once the socket is
   * OPEN (resolution == the browser 'open' event). Frames/close/errors arrive
   * via the events below. A pin failure during the handshake rejects this call
   * with "PIN_MISMATCH" AND emits wavrNetError with the same code.
   */
  openSocket(options: WavrNetOpenSocketOptions): Promise<{ socketId: string }>;

  /** Send a text frame. */
  sendSocket(options: { socketId: string; data: string }): Promise<void>;

  /** Close (default code 1000). Idempotent: an unknown socketId resolves quietly. */
  closeSocket(options: { socketId: string; code?: number; reason?: string }): Promise<void>;

  addListener(
    eventName: 'wavrNetMessage',
    listenerFunc: (event: WavrNetMessageEvent) => void,
  ): Promise<PluginListenerHandle>;
  addListener(
    eventName: 'wavrNetClose',
    listenerFunc: (event: WavrNetCloseEvent) => void,
  ): Promise<PluginListenerHandle>;
  addListener(
    eventName: 'wavrNetError',
    listenerFunc: (event: WavrNetErrorEvent) => void,
  ): Promise<PluginListenerHandle>;

  removeAllListeners(): Promise<void>;
}
