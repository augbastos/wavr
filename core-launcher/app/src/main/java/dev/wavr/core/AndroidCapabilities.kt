package dev.wavr.core

import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.hardware.display.DisplayManager
import android.os.BatteryManager
import android.os.Build
import org.json.JSONObject

/**
 * The Android half of a Capability Manifest (`docs/WAVR-PROTOCOL.md` §5).
 *
 * WHAT THIS DELIBERATELY IS NOT: a second capability scanner. `capabilities.py`
 * already probes the host, and on Android it runs *in this very process* under
 * Chaquopy, where `/proc/meminfo`, `os.cpu_count()` and `detect_platform()` all
 * work correctly (that module already special-cases Android, by name). Rewriting
 * that in Kotlin would create exactly the duplication ADR-0010 refuses.
 *
 * This class supplies ONLY the facts the Python probe cannot reach from inside a
 * sandboxed app process: the hardware feature flags, which live behind
 * [PackageManager.hasSystemFeature] and nowhere else. `wavr_android.py` merges
 * these over the Python scan and hands the result to the Core's own
 * `recommend()` — one manifest, one recommendation engine.
 *
 * THE HONESTY RULE (§5.1) IS ENFORCED HERE, and it is the whole reason this file
 * is careful rather than short. Every value is a TRISTATE: `true` proven present,
 * `false` proven absent, `null`/omitted **unknown**. A probe that throws resolves
 * to `null`, never to `false`. Keys this class cannot honestly answer — `mmwave`,
 * `wifi_csi`, `gpu`, `docker`, `raw_socket`, `network_scan`, `mdns`,
 * `serial_port_support` — are simply **not emitted**, so the Python scan's answer
 * survives untouched. Emitting `false` for "I did not look" is the one failure
 * mode §5.1 exists to prevent.
 */
object AndroidCapabilities {

    /** Bumped only when the shape changes; matches `CapabilityManifest.protocol_version`. */
    private const val PROTOCOL_VERSION = 1

    /**
     * Build the supplement.
     *
     * @param runtimeCanHostCore true when a Python runtime is actually bundled in
     *   THIS APK. Only Kotlin knows that, and it is the difference between a
     *   device that can be a Core and one that can only ever be a Node + Client.
     */
    fun manifest(context: Context, runtimeCanHostCore: Boolean): JSONObject {
        val app = context.applicationContext
        val pm = app.packageManager
        val caps = JSONObject()

        // -- Connectivity -------------------------------------------------
        put(caps, "wifi", feature(pm, PackageManager.FEATURE_WIFI))
        put(caps, "ethernet", feature(pm, PackageManager.FEATURE_ETHERNET))
        put(caps, "ble", feature(pm, PackageManager.FEATURE_BLUETOOTH_LE))
        put(caps, "bluetooth", feature(pm, PackageManager.FEATURE_BLUETOOTH))
        put(caps, "nfc", feature(pm, PackageManager.FEATURE_NFC))
        // FEATURE_UWB is API 31+. On 26..30 we cannot distinguish "no UWB" from
        // "no way to ask", so the answer is unknown, not no.
        put(
            caps, "uwb",
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                feature(pm, PackageManager.FEATURE_UWB)
            } else null
        )

        // -- Sensing hardware ---------------------------------------------
        put(caps, "camera", feature(pm, PackageManager.FEATURE_CAMERA_ANY))
        put(caps, "microphone", feature(pm, PackageManager.FEATURE_MICROPHONE))
        put(caps, "gps", feature(pm, PackageManager.FEATURE_LOCATION_GPS))
        put(caps, "accelerometer", feature(pm, PackageManager.FEATURE_SENSOR_ACCELEROMETER))
        // mmwave: a USB serial radar could be attached and we would not know.
        // wifi_csi: no Android API exposes CSI to an app, but a USB adapter could.
        // Both stay UNKNOWN — omitted entirely, so the Python probe's answer wins.

        // -- Compute / power ------------------------------------------------
        put(caps, "usb", feature(pm, PackageManager.FEATURE_USB_HOST))
        put(caps, "display", hasDisplay(app))
        put(caps, "battery", hasBattery(app))
        // permanent_power is a fact about the ROOM, not the device: "plugged in
        // right now" is not "always on mains". Only the operator knows, so it is
        // reported only once they have said so, and never guessed from the
        // charger state.
        val prefs = CorePrefs(app)
        if (prefs.mainsAnswered) put(caps, "permanent_power", prefs.declaredMainsPowered)
        // gpu: every phone has one; none of them expose it the way `nvidia-smi`
        // does, and "can this run model inference" is not answered by its
        // existence. Unknown.

        val functions = org.json.JSONArray().apply {
            if (runtimeCanHostCore) put("core")
            put("node")
            put("client")
        }

        return JSONObject().apply {
            put("platform", "android")
            put("arch", Build.SUPPORTED_ABIS.firstOrNull().orEmpty().take(32))
            put("os_version", "Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})".take(64))
            put("model", "${Build.MANUFACTURER} ${Build.MODEL}".trim().take(96))
            put("functions_supported", functions)
            put("capabilities", caps)
            // ram_mb is emitted because ActivityManager.MemoryInfo.totalMem is
            // strictly better than /proc/meminfo on Android (it reports the
            // figure the OS actually budgets), but it is a HINT: wavr_android.py
            // only uses it when the Python scan came back with nothing.
            ramMb(app)?.let { put("ram_mb", it) }
            put("cpu_count", Runtime.getRuntime().availableProcessors())
            put("protocol_version", PROTOCOL_VERSION)
            // Deliberately NO compute_tier: `capabilities._compute_tier()` owns
            // that classification and must keep owning it, or a phone and a
            // laptop start disagreeing about what "medium" means.
        }
    }

    /** Serialised form handed to Python and to the panel. Never throws. */
    fun manifestJson(context: Context, runtimeCanHostCore: Boolean): String = try {
        manifest(context, runtimeCanHostCore).toString()
    } catch (t: Throwable) {
        // A manifest we could not build is an EMPTY manifest, not a wrong one.
        "{\"platform\":\"android\",\"capabilities\":{},\"protocol_version\":$PROTOCOL_VERSION}"
    }

    // ---------------------------------------------------------------------

    /** Omit unknowns entirely rather than writing JSON null — §5.2 treats an
     *  absent key and an explicit null identically, and omitting is smaller. */
    private fun put(target: JSONObject, key: String, value: Boolean?) {
        if (value != null) target.put(key, value)
    }

    /** `hasSystemFeature` is the only authority on this; a throw means unknown. */
    private fun feature(pm: PackageManager, name: String): Boolean? = try {
        pm.hasSystemFeature(name)
    } catch (t: Throwable) {
        null
    }

    /**
     * "Is there a screen we could draw Wavr on?" — which is precisely what
     * `recommend()` uses this for (offer the Client function, or say there is no
     * screen and this device is managed from another one).
     *
     * NOT "is the screen on": a panel with the display asleep still has a
     * display. An earlier version ORed in a `state != STATE_OFF` check that was
     * tautological and implied a liveness test it did not perform.
     */
    private fun hasDisplay(context: Context): Boolean? = try {
        val dm = context.getSystemService(Context.DISPLAY_SERVICE) as? DisplayManager
        val displays = dm?.displays
        if (displays == null) null else displays.isNotEmpty()
    } catch (t: Throwable) {
        null
    }

    /** Read from the sticky battery broadcast; EXTRA_PRESENT is the direct answer. */
    private fun hasBattery(context: Context): Boolean? = try {
        val i: Intent? = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        if (i == null) null else i.getBooleanExtra(BatteryManager.EXTRA_PRESENT, true)
    } catch (t: Throwable) {
        null
    }

    private fun ramMb(context: Context): Int? = try {
        val am = context.getSystemService(Context.ACTIVITY_SERVICE) as? ActivityManager
        if (am == null) null else {
            val info = ActivityManager.MemoryInfo()
            am.getMemoryInfo(info)
            val mb = (info.totalMem / (1024L * 1024L)).toInt()
            if (mb > 0) mb else null
        }
    } catch (t: Throwable) {
        null
    }
}
