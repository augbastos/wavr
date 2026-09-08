package dev.wavr.mobile.wavrsensor

import java.util.concurrent.CopyOnWriteArraySet

/**
 * StreamStatusBus — process-local status channel between [SensorStreamService] (the
 * publisher) and [WavrSensorPlugin] (the subscriber that forwards to JS).
 *
 * WHY A BUS AND NOT A BOUND SERVICE: both ends live in the same process, and the
 * WebView reloads (location.reload() after pairing / role flips) recreate the plugin
 * while the service keeps streaming. A Binder connection would have to be re-bound
 * and leak-guarded across every reload; a volatile singleton gives the identical
 * started-service semantics (service outlives the WebView; plugin reads/subscribes at
 * will) with none of the ServiceConnection lifecycle churn. The service is still a
 * STARTED foreground service — only the query channel is simplified.
 *
 * CONTENT POLICY: [Status] carries counters and MACHINE CODES only — never the token,
 * the device name, an SSID/BSSID, a URL, or a raw exception message. `presentedFp` (a
 * certificate fingerprint, same value the pairing UI shows) is the single deliberate
 * exception, populated only on PIN_MISMATCH for the old-vs-new display.
 */
object StreamStatusBus {

    data class Status(
        val running: Boolean,
        /** "STREAMING" | "IDLE" | "ERROR" */
        val state: String,
        val sent: Int,
        val err: Int,
        /** Machine code (see wavr-sensor.d.ts) or null; NEVER free text with secrets. */
        val lastError: String?,
        /** Best-effort fingerprint the server is NOW presenting; PIN_MISMATCH only. */
        val presentedFp: String? = null,
    )

    val IDLE = Status(running = false, state = "IDLE", sent = 0, err = 0, lastError = null)

    @Volatile
    var current: Status = IDLE
        private set

    private val listeners = CopyOnWriteArraySet<(Status) -> Unit>()

    fun publish(status: Status) {
        current = status
        for (listener in listeners) {
            try {
                listener(status)
            } catch (_: Exception) {
                // a broken subscriber must never take down the stream loop
            }
        }
    }

    fun addListener(listener: (Status) -> Unit) {
        listeners.add(listener)
    }

    fun removeListener(listener: (Status) -> Unit) {
        listeners.remove(listener)
    }
}
