/**
 * WavrHealth — CONTRACT, v1 (FROZEN 2026-07-12).
 *
 * A minimal, LOCAL-ONLY helper for the shim's device+network health screen. It is a
 * sibling of WavrNet; the WavrNet network-choke contract stays FROZEN. Android
 * implements it today (WavrHealthPlugin.kt); a future iOS side (NWPathMonitor) MUST
 * satisfy this exact surface.
 *
 * Registration line the shim uses (no bundler; plain injected <script>):
 *
 *     const WavrHealth = Capacitor.registerPlugin("WavrHealth");
 *
 * SCOPE DISCIPLINE — what this deliberately is NOT:
 *  - It is NOT a reachability test to the hub. Hub reachability + the TLS/pin leg is
 *    owned by WavrNet.probe({url}) (bare pinned TLS handshake, returns the presented
 *    fingerprint) and by timing a WavrNet.request to /api/status. This plugin adds NO
 *    second reachability path — that would duplicate the one network choke point.
 *  - Its ONLY job is to classify THIS device's local transport so the health screen
 *    can explain WHY the hub is unreachable ("you're on cellular / not on Wi-Fi")
 *    instead of a bare "can't connect". It reads ConnectivityManager transport
 *    capabilities only.
 *
 * PRIVACY: uses ACCESS_NETWORK_STATE (already declared for the sensor). It does NOT
 * read the SSID/BSSID and does NOT request or use ACCESS_FINE/COARSE_LOCATION — the
 * transport TYPE needs no location permission, and reading it here would break the
 * honest Data-Safety posture. No network I/O. Logs nothing.
 */

/** The active local transport of THIS device. 'none' = no validated connection. */
export type WavrHealthTransport =
  | 'wifi'
  | 'cellular'
  | 'ethernet'
  | 'vpn'
  | 'other'
  | 'none';

export interface WavrHealthNetworkInfo {
  /** The active transport type (Wi-Fi/cellular/…). 'none' when offline. */
  transport: WavrHealthTransport;
  /** True when the active network reports validated internet capability. This is a
   *  LOCAL capability flag, NOT proof the hub is reachable (that is WavrNet.probe). */
  online: boolean;
  /** True when the active network is metered (e.g. cellular) — lets the health
   *  screen note that an OTA download will use mobile data. */
  metered: boolean;
}

export interface WavrHealthPlugin {
  /**
   * Classify THIS device's current local transport. Never rejects — resolves
   * { transport:'none', online:false, metered:false } if connectivity state is
   * unreadable. For explaining an unreachable hub, never as a trust decision.
   */
  networkInfo(): Promise<WavrHealthNetworkInfo>;
}
