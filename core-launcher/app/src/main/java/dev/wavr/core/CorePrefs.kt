package dev.wavr.core

import android.content.Context
import android.content.SharedPreferences

/**
 * Everything the operator has decided about THIS device, in app-private storage.
 *
 * Deliberately a plain [SharedPreferences] file (`MODE_PRIVATE`, no backup — the
 * manifest sets `allowBackup="false"`). Nothing secret lives here: device
 * functions, a port number, and three booleans. Credentials belong to the Core's
 * own SQLite in `filesDir`, never to this file.
 *
 * THE DEFAULT IS "UNCONFIGURED", AND THAT IS LOAD-BEARING. An app that has never
 * been through onboarding must behave exactly as the kiosk did before the Core
 * runtime existed: render `https://localhost:8000/?core`, start no service, and
 * boot no interpreter. The the field device in the field runs its Core in a Termux
 * proot on that same port — if this app decided on its own to become a Core it
 * would fight the running one for the socket. Becoming a Core is an explicit act.
 */
class CorePrefs(context: Context) {

    private val prefs: SharedPreferences =
        context.applicationContext.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    companion object {
        /** Shared with MainActivity's existing screen-policy prefs. */
        const val FILE = "wavr_core"

        private const val KEY_CONFIGURED = "roles_configured"
        private const val KEY_FUNCTIONS = "device_functions"
        private const val KEY_AUTOSTART = "core_autostart"
        private const val KEY_LAN = "core_lan_mode"
        private const val KEY_PORT = "core_port"
        private const val KEY_MAINS = "operator_says_mains"
        private const val KEY_DESIRED = "core_desired_running"

        /** ADR-0009 axis 2 — a device may hold any combination of these. */
        const val FN_CORE = "core"
        const val FN_NODE = "node"
        const val FN_CLIENT = "client"
        val VALID_FUNCTIONS = setOf(FN_CORE, FN_NODE, FN_CLIENT)

        const val DEFAULT_PORT = 8000

        /**
         * What an unconfigured install is. Client-only: exactly today's kiosk.
         * NOT a recommendation — [AndroidCapabilities] plus the Core's own
         * `capabilities.recommend()` produce that, and only after the operator
         * asks for it.
         */
        val UNCONFIGURED_FUNCTIONS = setOf(FN_CLIENT)
    }

    /** False until onboarding completes. While false, nothing new happens. */
    val configured: Boolean get() = prefs.getBoolean(KEY_CONFIGURED, false)

    /** The operator's chosen combination. Client-only while unconfigured. */
    val functions: Set<String>
        get() {
            if (!configured) return UNCONFIGURED_FUNCTIONS
            val raw = prefs.getStringSet(KEY_FUNCTIONS, null) ?: return UNCONFIGURED_FUNCTIONS
            val clean = raw.filterTo(mutableSetOf()) { it in VALID_FUNCTIONS }
            return if (clean.isEmpty()) UNCONFIGURED_FUNCTIONS else clean
        }

    /** True when this device is supposed to be running the authoritative runtime. */
    val isCore: Boolean get() = FN_CORE in functions

    /** True when this device contributes sensing to someone else's Core. */
    val isNode: Boolean get() = FN_NODE in functions

    val isClient: Boolean get() = FN_CLIENT in functions

    /**
     * Persist the operator's choice. Unknown function names are dropped rather
     * than rejected — the same posture `CapabilityManifest.from_dict` takes to a
     * manifest off the wire. An empty result leaves the device unconfigured.
     */
    fun setFunctions(wanted: Collection<String>) {
        val clean = wanted.filterTo(mutableSetOf()) { it in VALID_FUNCTIONS }
        if (clean.isEmpty()) {
            prefs.edit().putBoolean(KEY_CONFIGURED, false).remove(KEY_FUNCTIONS).apply()
            return
        }
        prefs.edit()
            .putStringSet(KEY_FUNCTIONS, clean)
            .putBoolean(KEY_CONFIGURED, true)
            .apply()
    }

    /** Undo onboarding entirely — back to the plain kiosk. */
    fun clearRoles() {
        prefs.edit().putBoolean(KEY_CONFIGURED, false).remove(KEY_FUNCTIONS).apply()
    }

    /**
     * Start the Core on boot. Defaults ON once the device is a Core (a house
     * brain that needs a human to press play after every power cut is not a
     * brain), but it is still only reachable through the Core role, which is
     * itself opt-in.
     */
    var autostart: Boolean
        get() = prefs.getBoolean(KEY_AUTOSTART, true)
        set(v) = prefs.edit().putBoolean(KEY_AUTOSTART, v).apply()

    /**
     * ADR-0006's opt-in, as a switch. OFF means `WAVR_BIND=127.0.0.1` and no
     * `WAVR_MULTIDEVICE` — ADR-0002's default, byte-identical. ON means the Core
     * binds the LAN over self-signed TLS and companions must present a token.
     */
    var lanMode: Boolean
        get() = prefs.getBoolean(KEY_LAN, false)
        set(v) = prefs.edit().putBoolean(KEY_LAN, v).apply()

    /** The port the Core listens on and the kiosk renders. */
    var port: Int
        get() = prefs.getInt(KEY_PORT, DEFAULT_PORT).coerceIn(1024, 65535)
        set(v) = prefs.edit().putInt(KEY_PORT, v.coerceIn(1024, 65535)).apply()

    /**
     * The operator has told us this device lives on a charger permanently (a wall
     * panel, a docked phone). Android cannot know this — "plugged in right now"
     * is not "always on mains" — so [AndroidCapabilities] reports
     * `permanent_power` as UNKNOWN unless the operator has said otherwise here.
     * That is the honesty rule of WAVR-PROTOCOL §5.1 applied to a fact only a
     * human holds.
     */
    var declaredMainsPowered: Boolean
        get() = prefs.getBoolean(KEY_MAINS, false)
        set(v) = prefs.edit().putBoolean(KEY_MAINS, v).apply()

    /** True when the operator has explicitly answered the mains question. */
    val mainsAnswered: Boolean get() = prefs.contains(KEY_MAINS)

    /**
     * The operator's INTENT, which is not the same as whether the process is
     * alive. Tapping "Stop" in the notification must mean stopped — not
     * "stopped until the 15-minute watchdog job or the next reboot brings it
     * back". [BootReceiver] and [CoreWatchdogJob] both start the Core only when
     * this is true, so a deliberate stop survives everything short of the
     * operator turning it on again.
     */
    var desiredRunning: Boolean
        get() = prefs.getBoolean(KEY_DESIRED, false)
        set(v) = prefs.edit().putBoolean(KEY_DESIRED, v).apply()

    /** Should the supervisor be running the Core right now? */
    val shouldRunCore: Boolean get() = isCore && desiredRunning
}
