/**
 * WavrUpdate — CONTRACT, v1 (FROZEN 2026-07-12). PINNED, WEB-ASSETS-ONLY OTA.
 *
 * A sibling of WavrNet; the WavrNet network-choke contract stays FROZEN. Android
 * implements it today (WavrUpdatePlugin.kt + BundleInstaller.kt + SafeTarExtractor.kt);
 * a future iOS side MUST satisfy this exact surface.
 *
 * Registration line the shim uses (no bundler; plain injected <script>):
 *
 *     const WavrUpdate = Capacitor.registerPlugin("WavrUpdate");
 *
 * WHAT IT DOES: downloads a new WEB-ASSET bundle (a .tar.gz of frontend/) from the
 * paired central, over the SAME pinned TLS as WavrNet (PinnedClient — exactly one
 * SHA-256 fingerprint trusted, never an unpinned fetch), verifies its SHA-256 while
 * streaming, SAFE-UNTARS it, and activates it on the NEXT app launch. It never
 * hot-swaps mid-session and it auto-reverts a bundle that fails to render.
 *
 * SECURITY INVARIANTS (Augusto signed off on these guardrails):
 *  - PINNED TRANSPORT: the download uses PinnedClient with the pinned fingerprint
 *    read natively from the Keystore (WavrSecureStorage). There is no unpinned code
 *    path. A cert change fails closed with 'PIN_MISMATCH' (+ presentedFp), exactly
 *    like WavrNet. The Bearer token is read natively from the Keystore and NEVER
 *    crosses this bridge.
 *  - SINGLE PEER: the bundle URL's host:port MUST equal the stored central's. Any
 *    other host is refused ('INVALID_ARGS') — the app's only network peer is the
 *    paired central, no egress beyond it.
 *  - INTEGRITY: the downloaded bytes are size-capped (zip-bomb guard) and their
 *    SHA-256 must equal the manifest's before ANYTHING is unpacked. Mismatch =>
 *    'HASH_MISMATCH', nothing installed.
 *  - SAFE UNTAR: rejects '..'/absolute paths, symlink/hardlink/device entries, over-
 *    count and over-size (per entry + total), and REQUIRES a root index.html. Any
 *    entry that is not a plain web asset is refused ('UNSAFE_BUNDLE').
 *  - WEB ASSETS ONLY: the bundle may carry index.html / sw / manifest / icons / css /
 *    js / fonts / images / vendor ONLY. It can NEVER carry wavr-mobile-shim.js,
 *    wavr-lib.js, native code, or any non-web file — the code that establishes the
 *    pin ships via the APK, never over the channel it secures. A bundle containing
 *    any of those is rejected wholesale.
 *  - NEXT-LAUNCH + AUTO-REVERT: apply() persists Capacitor's serverBasePath for the
 *    next launch (never a live hot-swap). The shim MUST call markLaunchOk() once the
 *    dashboard renders; if a newly applied bundle never confirms across launches it
 *    is automatically reverted to the previous good source. A Play-Store APK update
 *    also auto-reverts any stale web bundle (Capacitor's own isNewBinary()).
 *
 * The version is expected to increase monotonically; the shim compares against
 * current() before offering an update.
 */

/** Machine-readable rejection codes (err.code). */
export type WavrUpdateErrorCode =
  /** Missing/malformed args, a non-https URL, or a bundle URL whose host:port is not
   *  the stored central's (the app's only permitted peer). */
  | 'INVALID_ARGS'
  /** The central presented a certificate whose SHA-256 differs from the pinned one.
   *  err.data.presentedFp (best-effort) carries the cert now presented. Fail closed. */
  | 'PIN_MISMATCH'
  /** Transport failure (unreachable/timeout/reset; not a pin decision). */
  | 'NETWORK'
  /** A non-2xx response from the central (code embeds the status, e.g. 'HTTP_404'). */
  | string
  /** The download exceeded the declared size cap (zip-bomb guard) before completing. */
  | 'SIZE_EXCEEDED'
  /** The downloaded bytes' SHA-256 did not match the manifest — nothing unpacked. */
  | 'HASH_MISMATCH'
  /** The archive contained an unsafe path/entry type, a non-web asset, a forbidden
   *  file (shim/lib/native), or lacked a root index.html. Nothing installed. */
  | 'UNSAFE_BUNDLE'
  /** No paired central (centralUrl/pinnedFp/token missing or unusable) — pair first. */
  | 'NOT_PAIRED'
  /** A local filesystem/persistence failure while staging or activating. */
  | 'STORAGE';

/** The currently-active web bundle. version:null => running the APK's bundled assets. */
export interface WavrUpdateCurrent {
  version: string | null;
  /** Filesystem path of the active bundle, or null when running bundled assets. */
  path: string | null;
}

export interface WavrUpdateDownloadOptions {
  /** Absolute https:// URL of the bundle on the paired central (from the manifest).
   *  Its host:port MUST equal the stored central's — any other host is refused. */
  url: string;
  /** The bundle's expected SHA-256 (hex; ':'/whitespace tolerated), from the manifest.
   *  Verified over the downloaded .tar.gz bytes before anything is unpacked. */
  sha256: string;
  /** The bundle's exact byte size, from the manifest — the download size cap. */
  size: number;
  /** The new version string (used as the on-disk staging directory name). */
  version: string;
}

export interface WavrUpdatePlugin {
  /**
   * The currently-active web bundle. Reflects Capacitor's live server path, so after
   * a Play APK update auto-reverts a stale bundle this honestly reports version:null.
   */
  current(): Promise<WavrUpdateCurrent>;

  /**
   * Download, verify, and SAFE-UNTAR a bundle into private storage WITHOUT activating
   * it. Uses PinnedClient (pinned fingerprint + Bearer token read natively from the
   * Keystore). Resolves { version, path } once the verified bundle is staged. Rejects
   * with the codes above; nothing is left half-installed on any rejection.
   */
  download(options: WavrUpdateDownloadOptions): Promise<{ version: string; path: string }>;

  /**
   * Activate a previously-downloaded version on the NEXT launch (persists Capacitor's
   * serverBasePath; never a live hot-swap). The current session is unchanged. Rejects
   * 'INVALID_ARGS' if that version was not staged, 'STORAGE' on a persistence failure.
   */
  apply(options: { version: string }): Promise<void>;

  /**
   * The shim MUST call this once the dashboard has rendered after an applied update,
   * to confirm the new bundle is healthy. Without it, an applied bundle that fails to
   * render is auto-reverted on a later launch. Idempotent; a no-op when nothing is
   * pending.
   */
  markLaunchOk(): Promise<void>;

  /**
   * Revert to the APK's bundled assets on the next launch and delete staged bundles.
   * The manual escape hatch. Resolves when persisted.
   */
  reset(): Promise<void>;
}
