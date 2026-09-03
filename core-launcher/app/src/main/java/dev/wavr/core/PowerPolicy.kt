package dev.wavr.core

import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings
import org.json.JSONObject

/**
 * Doze, App Standby, and battery-optimisation state — read honestly, asked for
 * politely, never demanded.
 *
 * THE POSTURE, and it is a decision not a detail. The Termux Core solved this by
 * holding a `termux-wake-lock` permanently (measured held for 1 d 13 h, with Doze
 * pinned `ACTIVE` the whole time). That works and it is also the reason the phone
 * cannot sleep. This app does the opposite by default:
 *
 *  - It holds **no wake lock**. A foreground service already survives Doze for
 *    CPU purposes; what Doze defers is *network*, and a Core whose only client is
 *    on the same LAN mostly does not care.
 *  - It **asks** for a battery-optimisation exemption, on a screen that says why,
 *    and it keeps running if the answer is no — degraded and honest about it,
 *    never silently broken.
 *  - The default route is [settingsIntent], which opens Android's own list and
 *    needs no permission at all. The direct
 *    `ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` dialog is only fired when the
 *    operator taps the explicit button for it.
 *
 * [isBackgroundRestricted] is the one that actually kills Cores and is almost
 * never surfaced: if the user has set this app to "Restricted" in battery
 * settings, Android will stop the foreground service and no exemption helps.
 * We read it and say so rather than letting the Core die mysteriously overnight.
 */
object PowerPolicy {

    /** True when Android has agreed to leave this app alone in Doze. */
    fun isExempt(context: Context): Boolean = try {
        val pm = context.getSystemService(Context.POWER_SERVICE) as? PowerManager
        pm?.isIgnoringBatteryOptimizations(context.packageName) == true
    } catch (t: Throwable) {
        false
    }

    /**
     * True when the user put this app in the "Restricted" battery bucket. This
     * outranks any exemption: a restricted app cannot run a foreground service
     * reliably, so the Core will be killed. API 28+; unknown below, reported as
     * false rather than guessed.
     */
    fun isBackgroundRestricted(context: Context): Boolean = try {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.P) false
        else (context.getSystemService(Context.ACTIVITY_SERVICE) as? ActivityManager)
            ?.isBackgroundRestricted == true
    } catch (t: Throwable) {
        false
    }

    /** Right now, is the device in Doze / in battery saver? */
    fun isIdleNow(context: Context): Boolean = try {
        (context.getSystemService(Context.POWER_SERVICE) as? PowerManager)
            ?.isDeviceIdleMode == true
    } catch (t: Throwable) {
        false
    }

    fun isPowerSave(context: Context): Boolean = try {
        (context.getSystemService(Context.POWER_SERVICE) as? PowerManager)
            ?.isPowerSaveMode == true
    } catch (t: Throwable) {
        false
    }

    /**
     * The safe route: Android's own battery-optimisation list, filtered to
     * nothing. Requires NO permission and triggers no policy review; the
     * operator finds Wavr and chooses. This is the default the panel offers.
     */
    fun settingsIntent(): Intent =
        Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)

    /**
     * The direct dialog: one tap, one answer. Requires the
     * `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` permission in the manifest, which is
     * a declared-use permission on Google Play. This app is not distributed
     * there, but the rule we follow is the same one either way: it is fired ONLY
     * from an explicit operator action that has already shown [rationale], never
     * from `onCreate`, never from the service, never on a timer.
     */
    fun directRequestIntent(context: Context): Intent =
        Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
            .setData(Uri.parse("package:${context.packageName}"))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)

    /**
     * What the operator reads BEFORE anything is requested. Kept here, next to
     * the intents, so the explanation cannot drift away from the ask.
     */
    const val RATIONALE =
        "Wavr Core is the brain of your home: it keeps sensing and answering your " +
            "other devices while this screen is off. Android's battery optimiser can " +
            "pause background work to save power, which would make Wavr miss things " +
            "and make your dashboard slow to answer.\n\n" +
            "Exempting Wavr from that optimisation keeps it responsive. It does not " +
            "give Wavr any new permission, any new data, or any access to the " +
            "internet — Wavr still only talks to the devices you have paired with it.\n\n" +
            "It also does one thing that is easy to miss: from Android 12 onwards, " +
            "only an exempt app is allowed to restart its own background service. " +
            "Without the exemption, if Wavr ever crashes while you are not looking " +
            "at it, it cannot bring itself back until you open the app.\n\n" +
            "You can say no. Wavr will keep running; it may just be slower to react " +
            "while the screen is off, and it will tell you when that is happening."

    /** `{"exempt":..,"restricted":..,"idle":..,"powerSave":..,"advice":".."}` */
    fun state(context: Context): String = try {
        val exempt = isExempt(context)
        val restricted = isBackgroundRestricted(context)
        JSONObject().apply {
            put("exempt", exempt)
            put("restricted", restricted)
            put("idle", isIdleNow(context))
            put("powerSave", isPowerSave(context))
            put("advice", advice(exempt, restricted))
        }.toString()
    } catch (t: Throwable) {
        "{\"exempt\":false,\"restricted\":false,\"idle\":false,\"powerSave\":false,\"advice\":\"unknown\"}"
    }

    /**
     * One of four honest verdicts. `restricted` deliberately outranks `exempt`,
     * because it does in reality.
     */
    private fun advice(exempt: Boolean, restricted: Boolean): String = when {
        restricted -> "restricted"   // Android will kill the Core; the exemption is irrelevant.
        exempt -> "ok"
        else -> "ask"                // Works, may be deferred while the screen is off.
    }
}
