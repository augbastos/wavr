package dev.wavr.core

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Brings the Core back after a reboot — and only then, and only if it is
 * supposed to come back.
 *
 * THREE GATES, all of which must pass, because a house brain that resurrects
 * itself against the operator's wishes is malware with good intentions:
 *
 *  1. [CorePrefs.isCore] — this device was given the Core function at all.
 *  2. [CorePrefs.desiredRunning] — the operator has not pressed Stop.
 *  3. [CorePrefs.autostart] — they want it back automatically.
 *
 * An unconfigured install fails gate 1 and this receiver does nothing, which is
 * the whole point: installing the APK must not change what the phone does on
 * boot until someone says so.
 *
 * WHY THIS EXISTS AT ALL, given a foreground service. `START_STICKY` recovers a
 * service the system killed inside a live process; it does not survive a reboot,
 * a force-stop, or an app update. Those are exactly the three cases the Termux
 * Core needed a Magisk script, a Termux:Boot app, and a watchdog shell loop to
 * cover — here it is one receiver and one job, both first-party.
 *
 * Starting a foreground service from `BOOT_COMPLETED` is explicitly permitted:
 * the receiver is in the boot-completed exemption, and
 * `ContextCompat.startForegroundService` promotes within the allowed window
 * because [CoreService] calls `startForeground` first thing.
 */
class BootReceiver : BroadcastReceiver() {

    private companion object {
        const val TAG = "WavrBoot"
    }

    override fun onReceive(context: Context, intent: Intent?) {
        val action = intent?.action ?: return
        if (action != Intent.ACTION_BOOT_COMPLETED &&
            action != Intent.ACTION_LOCKED_BOOT_COMPLETED &&
            action != "android.intent.action.QUICKBOOT_POWERON" &&
            action != Intent.ACTION_MY_PACKAGE_REPLACED
        ) {
            return
        }
        try {
            val prefs = CorePrefs(context)
            if (!prefs.shouldRunCore || !prefs.autostart) {
                Log.i(TAG, "boot: Core not enabled on this device; doing nothing")
                return
            }
            // The watchdog is registered here rather than at first-run so that a
            // reboot re-establishes it even if the JobScheduler entry was lost.
            CoreWatchdogJob.schedule(context)
            CoreService.start(context)
            Log.i(TAG, "boot: Core requested")
        } catch (t: Throwable) {
            // A receiver that throws on BOOT_COMPLETED shows the user a crash
            // dialog before their launcher has even drawn. Never.
            Log.w(TAG, "boot handling failed: ${t.javaClass.simpleName}")
        }
    }
}
