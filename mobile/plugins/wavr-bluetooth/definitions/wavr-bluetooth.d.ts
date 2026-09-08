/**
 * WavrBluetooth — CONTRACT, v1 (FROZEN 2026-07-12).
 *
 * A DELIBERATELY TINY, READ-ONLY bridge that exposes THIS phone's already-bonded
 * (paired) Bluetooth devices so the admin can enrich the Core's identity registry
 * with real MAC labels. It is a sibling of WavrNet — the WavrNet network-choke
 * contract stays FROZEN; new native capability = a NEW sibling plugin, never a
 * WavrNet method. Android implements it today (WavrBluetoothPlugin.kt); a future
 * iOS side (CoreBluetooth retrieveConnectedPeripherals / paired accessories) MUST
 * satisfy this exact surface — any change here is a breaking contract change and
 * must be changelogged.
 *
 * Registration line the shim uses (no bundler; plain injected <script>):
 *
 *     const WavrBluetooth = Capacitor.registerPlugin("WavrBluetooth");
 *
 * PRIVACY / PERMISSION POSTURE (what this surface deliberately does NOT do — keeps
 * the Play Data-Safety form honestly "no data collected, no data shared"):
 *  - READS the BONDED list only (BluetoothAdapter.bondedDevices). It NEVER scans,
 *    NEVER discovers nearby strangers, NEVER connects. Bonded devices are the
 *    user's OWN previously-paired hardware — this is enrichment of the user's own
 *    registry, not "fingerprint-and-follow strangers' devices" (ADR-004).
 *  - Requests EXACTLY ONE runtime permission: BLUETOOTH_CONNECT (Android 12 / API
 *    31+). On API 30 and below the legacy install-time BLUETOOTH permission
 *    (maxSdkVersion=30) covers the read and there is no runtime prompt.
 *  - Deliberately NO BLUETOOTH_SCAN and NO ACCESS_FINE/COARSE_LOCATION. Reading the
 *    bonded set needs neither; not declaring them is the honest Data-Safety choice.
 *  - The returned addresses are the REAL bonded MACs (the 02:00:00:00:00:00
 *    redaction Android applies since API 26 hits only the LOCAL adapter address,
 *    not the addresses of bonded peers). No live presence is implied: a bonded MAC
 *    is a LABEL enriching the registry; presence still depends on the Core's radios.
 *
 * LOGGING: this plugin logs nothing — no MAC, no device name.
 */

/** Machine-readable rejection codes (err.code). */
export type WavrBluetoothErrorCode =
  /** Bluetooth is unsupported on this device (no adapter). */
  | 'NO_ADAPTER'
  /** Bluetooth is powered off — ask the user to enable it, then retry. */
  | 'BT_OFF'
  /** BLUETOOTH_CONNECT is not granted (API 31+). Call requestPermissions() first. */
  | 'PERMISSION_DENIED';

/** Capacitor PermissionState, declared locally (the shim is plain JS, no bundler).
 *  'na' = not applicable on this OS version (no runtime prompt below API 31). */
export type WavrBluetoothPermissionState =
  | 'granted'
  | 'denied'
  | 'prompt'
  | 'prompt-with-rationale'
  | 'na';

export interface WavrBluetoothPermissions {
  /** BLUETOOTH_CONNECT — 'na' below Android 12 (legacy install-time BLUETOOTH covers
   *  the read there, so listBonded() works without any runtime prompt). */
  bluetooth: WavrBluetoothPermissionState;
}

/** One already-bonded peer of THIS phone. */
export interface WavrBondedDevice {
  /** The bonded peer's real MAC, uppercase colon-separated ("AA:BB:CC:DD:EE:FF"). */
  address: string;
  /** The peer's advertised name, or "" if the platform withholds it. Never used as
   *  a trust decision — a display label only. */
  name: string;
}

export interface WavrBluetoothPlugin {
  /**
   * The already-bonded devices of THIS phone (BluetoothAdapter.bondedDevices).
   * READ-ONLY: no scan, no discovery, no connect. Rejects with 'PERMISSION_DENIED'
   * (API 31+) when BLUETOOTH_CONNECT is missing, 'BT_OFF' when the adapter is off,
   * 'NO_ADAPTER' when the device has no Bluetooth. Resolves { devices: [] } when the
   * user simply has no bonded devices.
   */
  listBonded(): Promise<{ devices: WavrBondedDevice[] }>;

  /** Read-only permission snapshot; never prompts. */
  checkPermissions(): Promise<WavrBluetoothPermissions>;

  /**
   * Prompt for BLUETOOTH_CONNECT on Android 12+ (a no-op that resolves the current
   * snapshot below API 31, where the read needs no runtime grant). Resolves with the
   * updated snapshot.
   */
  requestPermissions(): Promise<WavrBluetoothPermissions>;
}
