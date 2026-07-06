/**
 * WavrSensor — CONTRACT, v1 (FROZEN 2026-07-06).
 *
 * JS bridge to the native sensor node: an Android dataSync FOREGROUND SERVICE whose
 * 1 Hz sample+POST loop streams telemetry to the paired central — entirely natively.
 * The WebView only starts/stops it and renders status from events; zero JS runs in
 * the loop (anything else stalls under Doze). Android implements it today
 * (WavrSensorPlugin.kt + SensorStreamService.kt + TelemetrySampler.kt); a future iOS
 * side MUST satisfy this exact surface — any change here is a breaking contract
 * change and must be changelogged.
 *
 * Registration line the shim uses (no bundler; plain injected <script>):
 *
 *     const WavrSensor = Capacitor.registerPlugin("WavrSensor");
 *
 * SECURITY INVARIANTS (what this surface deliberately does NOT expose):
 *  - The loop reads {centralUrl, pinnedFp, token} from the native Keystore store
 *    (WavrSecureStorage's SecureKeyStore) and POSTs through the SAME pinned-TLS
 *    client WavrNet uses (PinnedClient) — one trust anchor, one pinned SHA-256
 *    fingerprint, no second HTTP stack. None of those secrets ever cross this
 *    bridge; JS sees counters and machine codes only. The single exception is
 *    `presentedFp` on PIN_MISMATCH — a certificate fingerprint (the same value the
 *    pairing UI displays) for the old-vs-new hard-fail card.
 *  - The ONLY network peer is the stored central URL + "/api/telemetry". A cert
 *    swap fail-closes the service (ERROR/PIN_MISMATCH) — never cleartext, never
 *    trust-all, never a fallback URL. A 401/403 wipes the stored token natively
 *    (mirrors companionAuthFailed) and stops with ERROR/AUTH_REVOKED so the shim
 *    drops to pairing. An unpaired device or a central without TLS is an explicit
 *    error (NOT_PAIRED / NO_TLS), never a cleartext POST.
 *  - ssid/bssid enter the (LAN-only) payload ONLY while ACCESS_FINE_LOCATION is
 *    granted, and this app requests that permission through exactly one path:
 *    requestPermissions({ wifiIdentity: true }) — the wizard's explicit opt-in,
 *    sensor-mode-only. rssi needs no location permission.
 */

/** Matches @capacitor/core's PluginListenerHandle (declared locally so this file
 *  has zero dependencies — the shim is plain JS, no bundler). */
export interface PluginListenerHandle {
  remove(): Promise<void>;
}

/** Streaming state. IDLE = never started or cleanly stopped; ERROR = terminally
 *  stopped (see WavrSensorErrorCode in `lastError`); STREAMING = loop live. */
export type WavrSensorState = 'STREAMING' | 'IDLE' | 'ERROR';

/**
 * Machine codes carried in `lastError` (status events / getStatus()).
 *
 * TERMINAL (service stopped, state === 'ERROR'):
 *  - 'PIN_MISMATCH'  the central presented a certificate whose SHA-256 differs from
 *                    the pinned fingerprint. Fail-closed: streaming stopped, nothing
 *                    sent, no re-pin. `presentedFp` (best-effort) carries the cert
 *                    now presented, for the shim's old-vs-new hard-fail screen.
 *  - 'AUTH_REVOKED'  the central answered 401/403. The stored token has ALREADY been
 *                    wiped natively; the shim must drop to pairing (same handling as
 *                    companionAuthFailed on the viewer path).
 *  - 'NOT_PAIRED'    centralUrl/pinnedFp/token missing or unusable — pair first.
 *  - 'NO_TLS'        the stored central URL is not https:// (central built without
 *                    the [tls] extra). Explicit error; cleartext is refused.
 *  - 'FGS_TIMEOUT'   Android 15+ exhausted the dataSync foreground-service runtime
 *                    budget; the service stopped cleanly. User may restart.
 *  - 'START_FAILED'  the service could not begin streaming (corrupt start intent).
 *
 * TRANSIENT (streaming CONTINUES, `err` incremented):
 *  - 'NETWORK'       transport failure (timeout/refused/reset; not a pin decision).
 *  - 'HTTP_<code>'   non-2xx, non-auth response (e.g. 'HTTP_404' while the central
 *                    does not yet serve /api/telemetry, 'HTTP_429' rate-limited).
 */
export type WavrSensorErrorCode =
  | 'PIN_MISMATCH'
  | 'AUTH_REVOKED'
  | 'NOT_PAIRED'
  | 'NO_TLS'
  | 'FGS_TIMEOUT'
  | 'START_FAILED'
  | 'NETWORK'
  | string; // 'HTTP_<code>'

/** Snapshot returned by getStatus() and pushed on every 'status' event (1 Hz while
 *  streaming, plus every state transition). */
export interface WavrSensorStatus {
  running: boolean;
  state: WavrSensorState;
  /** Successful POSTs since the last start. */
  sent: number;
  /** Failed ticks since the last start (transient errors only; terminal errors stop). */
  err: number;
  /** Machine code of the most recent failure; absent while healthy. */
  lastError?: WavrSensorErrorCode;
  /** Only with lastError === 'PIN_MISMATCH': the fingerprint the server is NOW
   *  presenting (SHA-256, uppercase colon-separated — same format as WavrNet). */
  presentedFp?: string;
}

/** Permission snapshot. 'granted'|'denied'|'prompt'|'prompt-with-rationale' follow
 *  Capacitor's PermissionState; 'na' = not applicable on this OS version. */
export type WavrSensorPermissionState =
  | 'granted'
  | 'denied'
  | 'prompt'
  | 'prompt-with-rationale'
  | 'na';

export interface WavrSensorPermissions {
  /** POST_NOTIFICATIONS — 'na' below Android 13 (no runtime prompt there). */
  notifications: WavrSensorPermissionState;
  /** ACCESS_FINE_LOCATION — requested ONLY via requestPermissions({wifiIdentity:true}). */
  location: WavrSensorPermissionState;
  /** Battery-optimization exemption (not a runtime permission): 'granted'|'denied'. */
  batteryExemption: 'granted' | 'denied';
}

export interface WavrSensorPlugin {
  /**
   * Start streaming as `name` (the payload's `device` field, e.g. "s25-ultra";
   * 1–64 chars). Resolves {running:true} when the foreground service is DISPATCHED —
   * the STREAMING transition (or a terminal ERROR such as NOT_PAIRED) arrives via
   * 'status' within the first tick. Re-start while running supersedes the live loop
   * (monotonic run-id — no double-send) with the new name.
   * Rejects: 'INVALID_ARGS' (bad name), 'SERVICE' (OS refused the foreground start).
   */
  start(options: { name: string }): Promise<{ running: true }>;

  /** Stop streaming. Idempotent; the final IDLE status arrives as a 'status' event. */
  stop(): Promise<{ running: false }>;

  /** Current status snapshot (also correct right after a WebView reload while the
   *  service keeps streaming — the service outlives the page). */
  getStatus(): Promise<WavrSensorStatus>;

  /** Read-only permission snapshot; never prompts. */
  checkPermissions(): Promise<WavrSensorPermissions>;

  /**
   * Prompt for POST_NOTIFICATIONS (Android 13+) and — ONLY when wifiIdentity is
   * explicitly true — ACCESS_FINE_LOCATION (the Wi-Fi identity opt-in; the app's
   * single location-request path, sensor-mode-only). Resolves with the updated
   * snapshot. A denial degrades (Wi-Fi fields go null); it never breaks streaming.
   */
  requestPermissions(options?: { wifiIdentity?: boolean }): Promise<WavrSensorPermissions>;

  /**
   * Open the MIUI/HyperOS autostart manager on Xiaomi-family devices (Xiaomi/Redmi/
   * Poco). opened:false means it fell back to App Info (already opened best-effort) —
   * the wizard should show manual instructions. Never rejects.
   */
  openOemAutostart(): Promise<{ oem: 'samsung' | 'xiaomi' | 'other'; opened: boolean }>;

  /**
   * Request the battery-optimization exemption: the direct system dialog when
   * available ('dialog'), else the optimization list ('list'), else App Info
   * ('appInfo'), else nothing ('none'). Never rejects.
   */
  openBatteryExemption(): Promise<{
    opened: boolean;
    surface: 'dialog' | 'list' | 'appInfo' | 'none';
  }>;

  /** Open this app's App Info screen (battery/permissions live under it). */
  openAppInfo(): Promise<{ opened: boolean }>;

  /**
   * 'status' — pushed on every state transition and once per tick while streaming.
   * The shim's listener must handle: lastError==='AUTH_REVOKED' → token is already
   * wiped, drop to pairing; lastError==='PIN_MISMATCH' → raise the certificate-changed
   * hard-fail screen (presentedFp for old-vs-new), exactly like WavrNet's PIN_MISMATCH.
   */
  addListener(
    eventName: 'status',
    listenerFunc: (status: WavrSensorStatus) => void,
  ): Promise<PluginListenerHandle>;

  removeAllListeners(): Promise<void>;
}
