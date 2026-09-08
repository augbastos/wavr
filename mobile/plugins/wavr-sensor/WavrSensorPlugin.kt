package dev.wavr.mobile.wavrsensor

import android.Manifest
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings
import androidx.core.content.ContextCompat
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin
import com.getcapacitor.annotation.Permission
import com.getcapacitor.annotation.PermissionCallback

/**
 * WavrSensor — JS bridge for the native sensor node (CONTRACT, v1).
 *
 * THIN BY DESIGN: all sensor data, the 1 Hz loop, the pinned POST, and every secret
 * live in [SensorStreamService] / [TelemetrySampler] — natively. This class only
 * starts/stops the service, relays [StreamStatusBus] to JS as 'status' events, and
 * fires the onboarding intents (Step 6). The token, the pinned fingerprint, the
 * SSID/BSSID and the payload NEVER cross this bridge — JS sees counters and machine
 * codes only (plus `presentedFp` on PIN_MISMATCH, the same value the pairing UI shows).
 *
 * JS registration (the shim): const WavrSensor = Capacitor.registerPlugin("WavrSensor");
 * Contract:                   see definitions/wavr-sensor.d.ts (frozen; iOS-neutral).
 *
 * PERMISSION POSTURE: ACCESS_FINE_LOCATION is requested through EXACTLY ONE path —
 * requestPermissions({ wifiIdentity: true }) — the wizard's explicit Wi-Fi-identity
 * opt-in, sensor-mode-only. A viewer/admin install never triggers it. POST_NOTIFICATIONS
 * is only meaningful on Android 13+ ('na' below). Battery exemption is not a runtime
 * permission; it is surfaced as a state and opened via the system dialog.
 *
 * LOGGING: this file logs nothing.
 */
@CapacitorPlugin(
    name = "WavrSensor",
    permissions = [
        Permission(alias = "notifications", strings = [Manifest.permission.POST_NOTIFICATIONS]),
        Permission(alias = "location", strings = [Manifest.permission.ACCESS_FINE_LOCATION]),
    ]
)
class WavrSensorPlugin : Plugin() {

    private val busListener: (StreamStatusBus.Status) -> Unit = { status ->
        notifyListeners("status", statusToJs(status))
    }

    override fun load() {
        StreamStatusBus.addListener(busListener)
    }

    override fun handleOnDestroy() {
        StreamStatusBus.removeListener(busListener)
        super.handleOnDestroy()
    }

    // ---- start / stop / status ---------------------------------------------------------

    /**
     * {name} -> {running:true}. Starts the dataSync foreground service; resolution means
     * the service was DISPATCHED — STREAMING (or a terminal ERROR like NOT_PAIRED) then
     * arrives via 'status' events within the first tick. Idempotent under re-start: a
     * second start supersedes the running loop (monotonic run-id) with the new name.
     */
    @PluginMethod
    fun start(call: PluginCall) {
        val name = call.getString("name")?.trim()
        if (name.isNullOrEmpty() || name.length > SensorStreamService.MAX_NAME_LEN) {
            call.reject("name is required (max ${SensorStreamService.MAX_NAME_LEN} chars)", "INVALID_ARGS")
            return
        }
        val intent = Intent(context, SensorStreamService::class.java)
            .setAction(SensorStreamService.ACTION_START)
            .putExtra(SensorStreamService.EXTRA_NAME, name)
        try {
            ContextCompat.startForegroundService(context, intent)
        } catch (e: Exception) {
            // e.g. ForegroundServiceStartNotAllowedException from the background
            call.reject("failed to start sensor service (${e.javaClass.simpleName})", "SERVICE")
            return
        }
        val ret = JSObject()
        ret.put("running", true)
        call.resolve(ret)
    }

    /** {} -> {running:false}. Idempotent; the final IDLE status arrives as an event. */
    @PluginMethod
    fun stop(call: PluginCall) {
        if (StreamStatusBus.current.running) {
            val intent = Intent(context, SensorStreamService::class.java)
                .setAction(SensorStreamService.ACTION_STOP)
            try {
                context.startService(intent)
            } catch (_: Exception) {
                // background-start restriction et al: stopService has no such limits
                try {
                    context.stopService(Intent(context, SensorStreamService::class.java))
                } catch (_: Exception) {
                    // service already gone
                }
            }
        }
        val ret = JSObject()
        ret.put("running", false)
        call.resolve(ret)
    }

    /** -> {running, state, sent, err, lastError?, presentedFp?} (current snapshot). */
    @PluginMethod
    fun getStatus(call: PluginCall) {
        call.resolve(statusToJs(StreamStatusBus.current))
    }

    private fun statusToJs(s: StreamStatusBus.Status): JSObject {
        val ret = JSObject()
        ret.put("running", s.running)
        ret.put("state", s.state)
        ret.put("sent", s.sent)
        ret.put("err", s.err)
        s.lastError?.let { ret.put("lastError", it) }
        s.presentedFp?.let { ret.put("presentedFp", it) }
        return ret
    }

    // ---- permissions (Step 6: in-house, Capacitor's own permission machinery) -----------

    /** -> {notifications, location, batteryExemption} (see d.ts for the state values). */
    @PluginMethod
    override fun checkPermissions(call: PluginCall) {
        call.resolve(permissionsSnapshot())
    }

    /**
     * {wifiIdentity?:bool} -> same snapshot as checkPermissions, after prompting.
     * Requests POST_NOTIFICATIONS (Android 13+) always; ACCESS_FINE_LOCATION ONLY when
     * wifiIdentity is explicitly true — this is the single location-request path in the
     * whole app (privacy gate for ssid/bssid, sensor-mode-only).
     */
    @PluginMethod
    override fun requestPermissions(call: PluginCall) {
        val wifiIdentity = call.getBoolean("wifiIdentity") ?: false
        val aliases = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= 33) aliases.add("notifications")
        if (wifiIdentity) aliases.add("location")
        if (aliases.isEmpty()) {
            call.resolve(permissionsSnapshot())
            return
        }
        requestPermissionForAliases(aliases.toTypedArray(), call, "permissionsCallback")
    }

    @PermissionCallback
    private fun permissionsCallback(call: PluginCall) {
        call.resolve(permissionsSnapshot())
    }

    private fun permissionsSnapshot(): JSObject {
        val ret = JSObject()
        ret.put(
            "notifications",
            if (Build.VERSION.SDK_INT >= 33) getPermissionState("notifications").toString() else "na"
        )
        ret.put("location", getPermissionState("location").toString())
        ret.put("batteryExemption", if (isBatteryExempt()) "granted" else "denied")
        return ret
    }

    private fun isBatteryExempt(): Boolean = try {
        (context.getSystemService(android.content.Context.POWER_SERVICE) as? PowerManager)
            ?.isIgnoringBatteryOptimizations(context.packageName) == true
    } catch (_: Exception) {
        false
    }

    // ---- onboarding intents (Step 6: no third-party SDK; each degrades, never crashes) --

    /**
     * -> {opened, surface:'dialog'|'list'|'appInfo'|'none'}. Tries the direct
     * ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS dialog (needs the manifest
     * permission), then the optimization list, then App Info. Never rejects.
     */
    @PluginMethod
    fun openBatteryExemption(call: PluginCall) {
        val ret = JSObject()
        val direct = Intent(
            Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
            Uri.parse("package:${context.packageName}")
        )
        when {
            launch(direct) -> { ret.put("opened", true); ret.put("surface", "dialog") }
            launch(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)) -> {
                ret.put("opened", true); ret.put("surface", "list")
            }
            launchAppInfo() -> { ret.put("opened", true); ret.put("surface", "appInfo") }
            else -> { ret.put("opened", false); ret.put("surface", "none") }
        }
        call.resolve(ret)
    }

    /** -> {opened}. This app's App Info screen (battery/permissions live under it). */
    @PluginMethod
    fun openAppInfo(call: PluginCall) {
        val ret = JSObject()
        ret.put("opened", launchAppInfo())
        call.resolve(ret)
    }

    /**
     * -> {oem:'samsung'|'xiaomi'|'other', opened}. MIUI/HyperOS autostart manager via
     * its explicit component on Xiaomi-family devices; anything else (or a blocked/
     * renamed activity) falls back to App Info with opened:false so the wizard can
     * show manual copy. Never rejects.
     */
    @PluginMethod
    fun openOemAutostart(call: PluginCall) {
        val oem = detectOem()
        var opened = false
        if (oem == "xiaomi") {
            opened = launch(Intent().setClassName(MIUI_SECURITY_PACKAGE, MIUI_AUTOSTART_ACTIVITY))
        }
        if (!opened) launchAppInfo()   // best-effort landing spot on every OEM
        val ret = JSObject()
        ret.put("oem", oem)
        ret.put("opened", opened)
        call.resolve(ret)
    }

    private fun detectOem(): String {
        val hay = "${Build.MANUFACTURER ?: ""} ${Build.BRAND ?: ""}".lowercase()
        return when {
            hay.contains("samsung") -> "samsung"
            // Xiaomi's sub-brands (Redmi, Poco) share the MIUI/HyperOS autostart quirk.
            Regex("xiaomi|redmi|poco").containsMatchIn(hay) -> "xiaomi"
            else -> "other"
        }
    }

    private fun launchAppInfo(): Boolean =
        launch(
            Intent(
                Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                Uri.parse("package:${context.packageName}")
            )
        ) || launch(Intent(Settings.ACTION_SETTINGS))

    /** Fire an activity intent; false on ActivityNotFound/SecurityException/anything. */
    private fun launch(intent: Intent): Boolean = try {
        val act = activity
        if (act != null) {
            act.startActivity(intent)
        } else {
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            context.startActivity(intent)
        }
        true
    } catch (_: Exception) {
        false
    }

    companion object {
        private const val MIUI_SECURITY_PACKAGE = "com.miui.securitycenter"
        private const val MIUI_AUTOSTART_ACTIVITY =
            "com.miui.permcenter.autostart.AutoStartManagementActivity"
    }
}
