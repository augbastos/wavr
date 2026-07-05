/**
 * WavrSecureStorage — CONTRACT, v1 (FROZEN 2026-07-05).
 *
 * A Keystore-backed secret store for the shim's three paired-central secrets. The
 * shim (mobile/src/wavr-mobile-shim.js) — and nothing else — talks to it. Android
 * implements it today (WavrSecureStoragePlugin.kt + SecureKeyStore.kt: AES-256-GCM
 * over the AndroidKeyStore). The future iOS side (Keychain via the same plugin API)
 * MUST satisfy this exact surface; any change here is a breaking contract change and
 * must be changelogged.
 *
 * Registration line the shim uses (no bundler; plain injected <script>):
 *
 *     const SecureStorage = Capacitor.registerPlugin("WavrSecureStorage");
 *
 * Keys the shim stores (see wavr-mobile-shim.js):
 *   "wavr.centralUrl"  — "https://<ip>:<port>" of the paired central
 *   "wavr.pinnedFp"    — the SHA-256 fingerprint captured & verified at pairing
 *   "wavr.token"       — the companion bearer token (revoked via remove())
 *
 * INVARIANTS: values are encrypted at rest under a key that never leaves the
 * platform keystore (never plaintext, never @capacitor/preferences, never
 * localStorage). The native side NEVER logs a key, a value, or the token. Storage
 * is local-only; this plugin performs no network I/O.
 *
 * DURABILITY (load-bearing): set() and remove() perform a BLOCKING commit and
 * resolve ONLY after the write reaches disk — because index.html calls
 * location.reload() immediately after a successful pair. An awaited set() therefore
 * cannot resolve before its value is durable.
 *
 * ERROR MODEL: get() NEVER rejects on a missing (or undecryptable) entry — it
 * resolves { value: null }. Rejections carry a machine code in `err.code`:
 *   'INVALID_ARGS' — missing/blank key, or set() called with a null value.
 *   'STORAGE'      — the durable write/delete failed, or a KeyStore/crypto error.
 * Rejection messages carry an exception class name at most — never a key or value.
 */

/** Machine-readable rejection codes (err.code). */
export type WavrSecureStorageErrorCode =
  /** Missing/blank `key`, or set() called with a null `value`. */
  | 'INVALID_ARGS'
  /** commit() failed, or a KeyStore/crypto error occurred while sealing/persisting. */
  | 'STORAGE';

export interface WavrSecureStoragePlugin {
  /**
   * Read the decrypted value for `key`. Resolves { value: null } when the key is
   * absent OR (defensively) when the stored blob cannot be decrypted — NEVER
   * rejects on a miss.
   */
  get(options: { key: string }): Promise<{ value: string | null }>;

  /**
   * Encrypt and DURABLY persist `value` under `key` (blocking commit). The promise
   * resolves only once the bytes are on disk; it rejects with code 'STORAGE' if the
   * write fails rather than resolving silently. An empty-string value is valid;
   * only a null/omitted value rejects with 'INVALID_ARGS'. Resolves with {}.
   */
  set(options: { key: string; value: string }): Promise<void>;

  /**
   * Durably delete `key`. Idempotent: removing an absent key resolves. Resolves
   * with {}; rejects with 'STORAGE' only if the durable delete fails.
   */
  remove(options: { key: string }): Promise<void>;
}
