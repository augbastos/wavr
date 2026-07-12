package dev.wavr.mobile.wavrupdate

import android.app.Activity
import android.content.Context
import android.content.SharedPreferences
import android.util.Log
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin
import java.io.File
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import org.json.JSONObject

/**
 * WavrUpdate — PINNED, WEB-ASSETS-ONLY OTA (CONTRACT, v1). Sibling of WavrNet; the
 * WavrNet contract stays FROZEN.
 *
 * JS registration (the shim): const WavrUpdate = Capacitor.registerPlugin("WavrUpdate");
 * Contract:                    see definitions/wavr-update.d.ts (frozen; iOS-neutral).
 *
 * This bridge is thin: it validates args, delegates the download/verify/safe-untar to
 * [BundleInstaller], and owns the NEXT-LAUNCH activation + AUTO-REVERT bookkeeping.
 *
 * ACTIVATION MODEL (never a live hot-swap — Augusto's guardrail):
 *  - apply() persists Capacitor's own serverBasePath into CapWebViewSettings; on the
 *    NEXT process launch Capacitor's Bridge loads web assets from that path (its
 *    Bridge.loadWebView reads it, guarded by !isNewBinary() — a Play APK update
 *    therefore auto-reverts any stale bundle for free). We never call
 *    setServerBasePath during a live session.
 *  - AUTO-REVERT: apply() also arms a pendingVerify flag + records the previous good
 *    source as a rollback. The shim MUST call markLaunchOk() once the dashboard
 *    renders. If an applied bundle never confirms across launches (its JS never ran),
 *    load() reverts to the rollback source and deletes the bad bundle — a bricked
 *    web bundle self-heals on the next open.
 *
 * WEB-ASSETS-ONLY / PIN / SINGLE-PEER invariants live in BundleInstaller +
 * SafeTarExtractor. This file establishes NO trust and does NO network I/O.
 *
 * LOGGING: machine codes only; never a token or full URL.
 */
@CapacitorPlugin(name = "WavrUpdate")
class WavrUpdatePlugin : Plugin() {

    private val executor: ExecutorService = Executors.newSingleThreadExecutor()

    /**
     * Launch-time auto-revert check. Runs natively regardless of whether the web page's
     * JS ran, so a bundle that fails to render still gets rolled back.
     */
    override fun load() {
        try {
            val prefs = updatePrefs()
            if (!prefs.getBoolean(PENDING_VERIFY, false)) return
            val attempts = prefs.getInt(VERIFY_ATTEMPTS, 0) + 1
            prefs.edit().putInt(VERIFY_ATTEMPTS, attempts).apply()
            if (attempts >= MAX_VERIFY_ATTEMPTS) {
                // The applied bundle launched at least twice without ever confirming:
                // treat it as broken and revert to the last known-good source.
                revertToRollback(prefs, live = true)
            }
        } catch (e: Exception) {
            Log.w(TAG, "auto-revert check failed (${e.javaClass.simpleName})")
        }
    }

    // ---- current ----------------------------------------------------------------------

    /** -> {version, path}. Reflects Capacitor's LIVE server path so an APK-update revert
     *  (isNewBinary clears serverBasePath) is reported honestly as version:null. */
    @PluginMethod
    fun current(call: PluginCall) {
        val activePath = capPrefs().getString(CAP_SERVER_PATH, null)
        val ret = JSObject()
        if (activePath.isNullOrEmpty() || !File(activePath).exists()) {
            ret.put("version", JSONObject.NULL)
            ret.put("path", JSONObject.NULL)
        } else {
            val version = updatePrefs().getString(ACTIVE_VERSION, null)
            ret.put("version", version ?: JSONObject.NULL)
            ret.put("path", activePath)
        }
        call.resolve(ret)
    }

    // ---- download ---------------------------------------------------------------------

    /** {url, sha256, size, version} -> {version, path}. Downloads + verifies + safe-untars
     *  WITHOUT activating. See BundleInstaller for the fail-closed integrity pipeline. */
    @PluginMethod
    fun download(call: PluginCall) {
        val url = call.getString("url")
        val sha256 = call.getString("sha256")
        val size = call.getLong("size")
        val version = call.getString("version")
        if (url.isNullOrBlank() || sha256.isNullOrBlank() || version.isNullOrBlank() || size == null) {
            call.reject("url, sha256, size and version are required", "INVALID_ARGS")
            return
        }
        if (!isSafeVersion(version)) {
            call.reject("version has unsafe characters", "INVALID_ARGS")
            return
        }
        runAsync {
            try {
                val path = BundleInstaller(context).install(
                    BundleInstaller.Opts(url, sha256, size, version)
                )
                val ret = JSObject()
                ret.put("version", version)
                ret.put("path", path)
                call.resolve(ret)
            } catch (e: BundleException) {
                val data = JSObject()
                e.presentedFp?.let { data.put("presentedFp", it) }
                call.reject(e.message ?: "install failed", e.code, data)
            } catch (e: Exception) {
                call.reject("install failed (${e.javaClass.simpleName})", "STORAGE")
            }
        }
    }

    // ---- apply (next-launch) ----------------------------------------------------------

    /** {version} -> {}. Arms the staged bundle for the NEXT launch; never hot-swaps. */
    @PluginMethod
    fun apply(call: PluginCall) {
        val version = call.getString("version")
        if (version.isNullOrBlank() || !isSafeVersion(version)) {
            call.reject("a valid version is required", "INVALID_ARGS")
            return
        }
        val dir = File(File(context.filesDir, WEB_DIR), version)
        if (!File(dir, "index.html").isFile) {
            call.reject("that version is not staged", "INVALID_ARGS")
            return
        }
        try {
            val cap = capPrefs()
            // Record the CURRENT source as the rollback target BEFORE overwriting it.
            val prevPath = cap.getString(CAP_SERVER_PATH, "") ?: ""
            val prevVersion = updatePrefs().getString(ACTIVE_VERSION, "") ?: ""
            cap.edit().putString(CAP_SERVER_PATH, dir.path).apply()   // Capacitor reads this next launch
            updatePrefs().edit()
                .putString(ACTIVE_VERSION, version)
                .putString(ROLLBACK_PATH, prevPath)
                .putString(ROLLBACK_VERSION, prevVersion)
                .putBoolean(PENDING_VERIFY, true)
                .putInt(VERIFY_ATTEMPTS, 0)
                .apply()
            call.resolve()
        } catch (e: Exception) {
            call.reject("could not persist activation (${e.javaClass.simpleName})", "STORAGE")
        }
    }

    /** -> {}. The shim calls this once the dashboard renders; confirms the bundle is healthy. */
    @PluginMethod
    fun markLaunchOk(call: PluginCall) {
        try {
            updatePrefs().edit()
                .putBoolean(PENDING_VERIFY, false)
                .remove(VERIFY_ATTEMPTS)
                .remove(ROLLBACK_PATH)
                .remove(ROLLBACK_VERSION)
                .apply()
        } catch (_: Exception) {
        }
        call.resolve()
    }

    /** -> {}. Manual escape hatch: revert to bundled assets next launch, drop staged bundles. */
    @PluginMethod
    fun reset(call: PluginCall) {
        try {
            capPrefs().edit().putString(CAP_SERVER_PATH, "").apply()
            updatePrefs().edit().clear().apply()
            deleteRecursively(File(context.filesDir, WEB_DIR))
        } catch (e: Exception) {
            call.reject("reset failed (${e.javaClass.simpleName})", "STORAGE")
            return
        }
        call.resolve()
    }

    // ---- internals --------------------------------------------------------------------

    /**
     * Revert serverBasePath to the recorded rollback source, clear the pending flag, and
     * delete the failed bundle. When [live] and a Bridge is available, also recover the
     * CURRENT (broken) session immediately — the rollback path if it still exists, else
     * the APK's bundled "public" assets.
     */
    private fun revertToRollback(prefs: SharedPreferences, live: Boolean) {
        val rollbackPath = prefs.getString(ROLLBACK_PATH, "") ?: ""
        val rollbackVersion = prefs.getString(ROLLBACK_VERSION, "") ?: ""
        val badVersion = prefs.getString(ACTIVE_VERSION, null)

        capPrefs().edit().putString(CAP_SERVER_PATH, rollbackPath).apply()
        prefs.edit()
            .putString(ACTIVE_VERSION, rollbackVersion)
            .putBoolean(PENDING_VERIFY, false)
            .remove(VERIFY_ATTEMPTS)
            .remove(ROLLBACK_PATH)
            .remove(ROLLBACK_VERSION)
            .apply()

        if (badVersion != null && isSafeVersion(badVersion)) {
            deleteRecursively(File(File(context.filesDir, WEB_DIR), badVersion))
        }
        Log.w(TAG, "OTA bundle failed to confirm — reverted to the previous source")

        if (live) {
            try {
                val bridge = getBridge() ?: return
                val rollbackDir = File(rollbackPath)
                if (rollbackPath.isNotEmpty() && rollbackDir.exists()) {
                    bridge.setServerBasePath(rollbackPath)      // recover this session
                } else {
                    bridge.setServerAssetPath(BUNDLED_ASSET_PATH) // fall back to APK assets
                }
            } catch (e: Exception) {
                Log.w(TAG, "live revert reload failed (${e.javaClass.simpleName})")
            }
        }
    }

    private fun capPrefs(): SharedPreferences =
        context.getSharedPreferences(CAP_PREFS, Activity.MODE_PRIVATE)

    private fun updatePrefs(): SharedPreferences =
        context.getSharedPreferences(UPDATE_PREFS, Activity.MODE_PRIVATE)

    /** Filesystem-safe version token (also the on-disk dir name). */
    private fun isSafeVersion(v: String): Boolean =
        v.isNotEmpty() && v.length <= 64 && v.all { it.isLetterOrDigit() || it == '.' || it == '_' || it == '-' }

    private fun deleteRecursively(f: File) {
        if (f.exists()) f.walkBottomUp().forEach { it.delete() }
    }

    private fun runAsync(block: () -> Unit) {
        try {
            executor.execute(block)
        } catch (_: RejectedExecutionException) {
        }
    }

    override fun handleOnDestroy() {
        executor.shutdown()
        super.handleOnDestroy()
    }

    companion object {
        private const val TAG = "WavrUpdate"
        private const val WEB_DIR = "wavr-web"

        // Capacitor's own next-launch web-source store (com.getcapacitor.plugin.WebView).
        private const val CAP_PREFS = "CapWebViewSettings"
        private const val CAP_SERVER_PATH = "serverBasePath"
        // Capacitor's default bundled web assets live under assets/public.
        private const val BUNDLED_ASSET_PATH = "public"

        // Our own bookkeeping — deliberately separate from Capacitor's prefs.
        private const val UPDATE_PREFS = "WavrUpdatePrefs"
        private const val ACTIVE_VERSION = "activeVersion"
        private const val PENDING_VERIFY = "pendingVerify"
        private const val VERIFY_ATTEMPTS = "verifyAttempts"
        private const val ROLLBACK_PATH = "rollbackPath"
        private const val ROLLBACK_VERSION = "rollbackVersion"
        private const val MAX_VERIFY_ATTEMPTS = 2
    }
}
