package dev.wavr.mobile.securestorage

import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin
import org.json.JSONObject

/**
 * WavrSecureStorage — Keystore-backed secret store for the shim (CONTRACT, v1).
 *
 * The shim (mobile/src/wavr-mobile-shim.js) reaches this via exactly:
 *
 *     const SecureStorage = Capacitor.registerPlugin("WavrSecureStorage");
 *
 * and uses ONLY these three methods, for the three keys "wavr.centralUrl",
 * "wavr.pinnedFp", "wavr.token":
 *
 *     get({ key })          -> { value: string | null }   // null on miss; NEVER rejects on a miss
 *     set({ key, value })   -> {}                          // DURABLE: on disk before it resolves
 *     remove({ key })       -> {}
 *
 * All crypto lives in [SecureKeyStore] (AES-256-GCM over the AndroidKeyStore). This
 * class is a thin bridge: validate args, delegate, shape the JS result.
 *
 * THREADING: Capacitor dispatches @PluginMethod handlers off the WebView/UI thread
 * by default, so the blocking commit() in set()/remove() does not touch the UI
 * thread; the payloads (a URL, a 64-hex fingerprint, a bearer token) are tiny
 * regardless. Correctness does not depend on the thread: set()/remove() resolve
 * ONLY after SecureKeyStore's commit() returns true, so an awaited call cannot
 * resolve before the write is durable.
 *
 * LOGGING: this bridge logs nothing. Rejections carry a machine code and a class
 * name at most — never the key name, never the value/token.
 */
@CapacitorPlugin(name = "WavrSecureStorage")
class WavrSecureStoragePlugin : Plugin() {

    // Bound lazily on first call, by which point getContext() is set.
    private val store by lazy { SecureKeyStore(getContext()) }

    /** get({key}) -> {value: string|null}. Absent OR undecryptable => {value:null}. Never rejects on a miss. */
    @PluginMethod
    fun get(call: PluginCall) {
        val key = call.getString("key")
        if (key.isNullOrBlank()) {
            call.reject("key is required", "INVALID_ARGS")
            return
        }
        val value = store.get(key)                 // null on absence or defensive decrypt failure
        val ret = JSObject()
        // Explicit JSON null (not a removed key) so the JS side sees {value:null}.
        ret.put("value", value ?: JSONObject.NULL)
        call.resolve(ret)
    }

    /** set({key, value}) -> {}. Persists DURABLY (commit) before resolving; rejects if the write fails. */
    @PluginMethod
    fun set(call: PluginCall) {
        val key = call.getString("key")
        val value = call.getString("value")        // "" is valid; only null is rejected
        if (key.isNullOrBlank() || value == null) {
            call.reject("key and value are required", "INVALID_ARGS")
            return
        }
        try {
            if (store.set(key, value)) call.resolve()
            else call.reject("failed to persist value", "STORAGE")   // commit() returned false
        } catch (e: Exception) {
            // class name only — never the key name, never the value
            call.reject("secure write failed (${e.javaClass.simpleName})", "STORAGE")
        }
    }

    /** remove({key}) -> {}. Idempotent (absent key resolves); rejects only if the durable delete fails. */
    @PluginMethod
    fun remove(call: PluginCall) {
        val key = call.getString("key")
        if (key.isNullOrBlank()) {
            call.reject("key is required", "INVALID_ARGS")
            return
        }
        try {
            if (store.remove(key)) call.resolve()
            else call.reject("failed to remove value", "STORAGE")
        } catch (e: Exception) {
            call.reject("secure remove failed (${e.javaClass.simpleName})", "STORAGE")
        }
    }
}
