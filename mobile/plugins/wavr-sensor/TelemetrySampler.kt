package dev.wavr.mobile.wavrsensor

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.net.wifi.WifiManager
import android.os.BatteryManager
import android.os.Handler
import android.os.HandlerThread
import androidx.core.content.ContextCompat
import org.json.JSONArray
import org.json.JSONObject

/**
 * TelemetrySampler — native sensor/battery/Wi-Fi acquisition for [SensorStreamService].
 *
 * Ports the retired Expo node's sensors.ts to the Android framework APIs, keeping the
 * wire contract IDENTICAL (backend `POST /api/telemetry` payload, frozen in the
 * unify blueprint §2/§4):
 *
 *   { device, sensors: { accel?:[x,y,z], gyro?:[x,y,z], mag?:[x,y,z],
 *     pressure?:[n], light?:[n] }, battery_pct, charging, rssi, ssid, bssid }
 *
 *  - Missing sensors are OMITTED from `sensors` (never null placeholders).
 *  - pressure/light are single-element arrays; rssi/ssid/bssid are always present
 *    (JSON null when unavailable). Rounding matches sensors.ts: accel/gyro 5 digits,
 *    mag 4, pressure 3, light 2.
 *  - NO g→m/s² conversion: unlike expo-sensors, Android's TYPE_ACCELEROMETER already
 *    reports m/s² natively — the Expo-era ACCEL_G_TO_MS2 factor is deliberately DROPPED
 *    (blueprint ground truth). Gyro rad/s, mag µT, pressure hPa, light lux all match too.
 *
 * Everything is best-effort and null-safe: a missing sensor, a denied permission, an
 * unavailable system service, or a non-finite reading NEVER breaks the stream — the
 * affected field is simply omitted / nulled.
 *
 * PRIVACY GATE (sensor-mode-only, opt-in): rssi comes from WifiManager.connectionInfo
 * and needs NO location permission. ssid/bssid are read ONLY while ACCESS_FINE_LOCATION
 * is granted — and this app requests that permission through exactly one path, the
 * plugin's requestPermissions({wifiIdentity:true}) opt-in, so "granted" implies the
 * user opted into Wi-Fi identity. Android's own sentinels ("<unknown ssid>" /
 * 02:00:00:00:00:00) are additionally nulled out.
 *
 * LOGGING: this file logs NOTHING. Payload contents (device name, ssid, bssid, sensor
 * values) never reach logcat.
 *
 * THREADING: sensor callbacks land on a dedicated HandlerThread (never the main
 * thread, never the POST loop's thread); latest values cross to the POST loop via
 * @Volatile snapshot arrays (a fresh array per event — safe publication, no tearing).
 */
class TelemetrySampler(context: Context) {

    private val appContext = context.applicationContext

    // Latest sample per sensor; null until the first event (or when absent/stopped).
    @Volatile private var accel: FloatArray? = null
    @Volatile private var gyro: FloatArray? = null
    @Volatile private var mag: FloatArray? = null
    @Volatile private var pressure: Float? = null
    @Volatile private var light: Float? = null

    private var callbackThread: HandlerThread? = null

    private val listener = object : SensorEventListener {
        override fun onSensorChanged(event: SensorEvent) {
            when (event.sensor.type) {
                Sensor.TYPE_ACCELEROMETER -> accel = event.values.clone()   // m/s² natively
                Sensor.TYPE_GYROSCOPE -> gyro = event.values.clone()        // rad/s
                Sensor.TYPE_MAGNETIC_FIELD -> mag = event.values.clone()    // µT
                Sensor.TYPE_PRESSURE -> pressure = event.values.firstOrNull() // hPa
                Sensor.TYPE_LIGHT -> light = event.values.firstOrNull()       // lux
            }
        }

        override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) { /* not used */ }
    }

    /**
     * Register for every sensor the device actually has (a missing sensor is simply
     * never registered — its field stays absent from the payload). Idempotent.
     */
    fun start() {
        if (callbackThread != null) return
        val thread = HandlerThread("WavrSensorSampling").also { it.start() }
        callbackThread = thread
        val handler = Handler(thread.looper)
        val sm = try {
            appContext.getSystemService(Context.SENSOR_SERVICE) as? SensorManager
        } catch (_: Exception) {
            null
        } ?: return   // no sensor service: stream still runs on battery/Wi-Fi alone
        for (type in SENSOR_TYPES) {
            try {
                sm.getDefaultSensor(type)?.let { sensor ->
                    sm.registerListener(listener, sensor, SAMPLING_PERIOD_US, handler)
                }
            } catch (_: Exception) {
                // best-effort: one bad sensor never blocks the others
            }
        }
    }

    /** Unregister everything and drop cached values. Idempotent. */
    fun stop() {
        try {
            (appContext.getSystemService(Context.SENSOR_SERVICE) as? SensorManager)
                ?.unregisterListener(listener)
        } catch (_: Exception) {
        }
        callbackThread?.quitSafely()
        callbackThread = null
        accel = null; gyro = null; mag = null; pressure = null; light = null
    }

    /** Assemble one full telemetry payload from the latest device state. Never throws. */
    fun buildPayload(device: String): JSONObject {
        val sensors = JSONObject()
        vec(accel, 5)?.let { sensors.put("accel", it) }
        vec(gyro, 5)?.let { sensors.put("gyro", it) }
        vec(mag, 4)?.let { sensors.put("mag", it) }
        scalar(pressure, 3)?.let { sensors.put("pressure", it) }
        scalar(light, 2)?.let { sensors.put("light", it) }

        val (batteryPct, charging) = readBattery()
        val wifi = readWifi()

        return JSONObject().apply {
            put("device", device)
            put("sensors", sensors)
            put("battery_pct", batteryPct)
            put("charging", charging)
            put("rssi", wifi.rssi ?: JSONObject.NULL)
            put("ssid", wifi.ssid ?: JSONObject.NULL)
            put("bssid", wifi.bssid ?: JSONObject.NULL)
        }
    }

    // ---- payload helpers -----------------------------------------------------------

    /** [x,y,z] rounded to [digits], or null when absent/not finite (org.json rejects NaN). */
    private fun vec(v: FloatArray?, digits: Int): JSONArray? {
        val a = v ?: return null
        if (a.size < 3 || !a[0].isFinite() || !a[1].isFinite() || !a[2].isFinite()) return null
        return JSONArray().put(round(a[0], digits)).put(round(a[1], digits)).put(round(a[2], digits))
    }

    /** Single-element [n] (the receiver contract for pressure/light), or null. */
    private fun scalar(v: Float?, digits: Int): JSONArray? {
        val n = v ?: return null
        if (!n.isFinite()) return null
        return JSONArray().put(round(n, digits))
    }

    private fun round(v: Float, digits: Int): Double {
        val f = POW10[digits]
        return Math.round(v * f) / f
    }

    // ---- battery (sticky ACTION_BATTERY_CHANGED; BatteryManager fallback) -----------

    private fun readBattery(): Pair<Int, String> = try {
        // registerReceiver(null, ...) reads the STICKY broadcast without registering
        // a receiver — no receiver lifecycle, works from a Service, Doze-safe.
        val intent = appContext.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val level = intent?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = intent?.getIntExtra(BatteryManager.EXTRA_SCALE, -1) ?: -1
        val pct = if (level >= 0 && scale > 0) (level * 100 / scale).coerceIn(0, 100)
                  else batteryPctFallback()
        val charging = when (intent?.getIntExtra(BatteryManager.EXTRA_STATUS, -1) ?: -1) {
            BatteryManager.BATTERY_STATUS_CHARGING -> "CHARGING"
            BatteryManager.BATTERY_STATUS_FULL -> "FULL"
            BatteryManager.BATTERY_STATUS_DISCHARGING,
            BatteryManager.BATTERY_STATUS_NOT_CHARGING -> "UNPLUGGED"
            else -> "UNKNOWN"
        }
        Pair(pct, charging)
    } catch (_: Exception) {
        Pair(0, "UNKNOWN")   // matches the legacy node's defensive default
    }

    private fun batteryPctFallback(): Int = try {
        (appContext.getSystemService(Context.BATTERY_SERVICE) as? BatteryManager)
            ?.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
            ?.takeIf { it in 0..100 } ?: 0
    } catch (_: Exception) {
        0
    }

    // ---- Wi-Fi (rssi permissionless; ssid/bssid gated on the location opt-in) --------

    private data class WifiRead(val rssi: Int?, val ssid: String?, val bssid: String?)

    @Suppress("DEPRECATION")   // WifiManager.connectionInfo: deprecated but the only
                               // sync API that serves a foreground service on API 29+
    private fun readWifi(): WifiRead {
        val info = try {
            (appContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager)?.connectionInfo
        } catch (_: Exception) {
            null
        } ?: return WifiRead(null, null, null)

        val rssi = try {
            info.rssi.takeIf { it != INVALID_RSSI }
        } catch (_: Exception) {
            null
        }

        // ssid/bssid ONLY behind the fine-location grant (which only the explicit
        // wifiIdentity opt-in ever requests). Without it Android returns sentinels
        // anyway; we never even read them.
        var ssid: String? = null
        var bssid: String? = null
        if (hasFineLocation()) {
            try {
                ssid = info.ssid?.removeSurrounding("\"")
                    ?.takeUnless { it.isBlank() || it == UNKNOWN_SSID }
                bssid = info.bssid?.lowercase()?.takeUnless { it == NULL_BSSID }
            } catch (_: Exception) {
                // denied mid-read / OEM quirk: fields stay null, stream continues
            }
        }
        return WifiRead(rssi, ssid, bssid)
    }

    private fun hasFineLocation(): Boolean =
        ContextCompat.checkSelfPermission(appContext, Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    companion object {
        /** 2x oversample of the 1 Hz POST tick, matching the legacy SENSOR_UPDATE_MS=500. */
        private const val SAMPLING_PERIOD_US = 500_000

        private val SENSOR_TYPES = intArrayOf(
            Sensor.TYPE_ACCELEROMETER,
            Sensor.TYPE_GYROSCOPE,
            Sensor.TYPE_MAGNETIC_FIELD,
            Sensor.TYPE_PRESSURE,
            Sensor.TYPE_LIGHT,
        )

        // Android sentinels for "Wi-Fi identity hidden" (mirrors the legacy node).
        private const val UNKNOWN_SSID = "<unknown ssid>"
        private const val NULL_BSSID = "02:00:00:00:00:00"
        /** WifiInfo.INVALID_RSSI (constant only added in API 30; minSdk is 29). */
        private const val INVALID_RSSI = -127

        private val POW10 = doubleArrayOf(1.0, 10.0, 100.0, 1e3, 1e4, 1e5, 1e6)
    }
}
