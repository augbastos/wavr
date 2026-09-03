package dev.wavr.core

import android.app.job.JobInfo
import android.app.job.JobParameters
import android.app.job.JobScheduler
import android.app.job.JobService
import android.content.ComponentName
import android.content.Context
import android.util.Log

/**
 * The backstop for the failure `START_STICKY` cannot see: the whole process
 * dying and nothing asking for it back.
 *
 * The G9 Core hit exactly this and it took a shell loop on `/sdcard` polling
 * `ss :8000` every 90 seconds to catch it. `JobScheduler` is the same idea with
 * the OS doing the polling: a persisted periodic job that checks whether the
 * Core *should* be running and is not, and starts it. Persisted means it
 * survives reboot; periodic means Android batches it with other wakeups instead
 * of holding the CPU awake for us.
 *
 * FIFTEEN MINUTES IS THE FLOOR AND WE DO NOT FIGHT IT. Android clamps periodic
 * jobs to 15 minutes and stretches them further in Doze. That is a real gap
 * during which a crashed Core stays down — and it is the honest cost of not
 * holding a wake lock. The alternatives are worse: an exact alarm needs
 * `SCHEDULE_EXACT_ALARM` (a special-access permission for a use case that is not
 * an alarm), and a permanent wake lock is the thing [PowerPolicy] explains we are
 * deliberately not doing. A Core that is back within 15 minutes of a crash,
 * without keeping the phone awake, is the right trade for a home.
 *
 * The job asks for **no network**: it inspects local state only. A watchdog that
 * waits for connectivity is a watchdog that never fires on a phone in a drawer.
 */
class CoreWatchdogJob : JobService() {

    companion object {
        private const val TAG = "WavrWatchdog"
        private const val JOB_ID = 0x57415601

        /** Android's own floor for a periodic job. Stated, not guessed. */
        private const val PERIOD_MS = 15L * 60L * 1000L

        fun schedule(context: Context) {
            try {
                val scheduler = context.getSystemService(JobScheduler::class.java) ?: return
                val job = JobInfo.Builder(
                    JOB_ID,
                    ComponentName(context.applicationContext, CoreWatchdogJob::class.java),
                )
                    .setPeriodic(PERIOD_MS)
                    // Survives reboot. Requires RECEIVE_BOOT_COMPLETED, which we
                    // already hold for BootReceiver — no extra permission.
                    .setPersisted(true)
                    .setRequiredNetworkType(JobInfo.NETWORK_TYPE_NONE)
                    .setRequiresDeviceIdle(false)
                    .setRequiresCharging(false)
                    .build()
                scheduler.schedule(job)
            } catch (t: Throwable) {
                Log.w(TAG, "could not schedule: ${t.javaClass.simpleName}")
            }
        }

        fun cancel(context: Context) {
            try {
                context.getSystemService(JobScheduler::class.java)?.cancel(JOB_ID)
            } catch (t: Throwable) {
                // nothing to cancel
            }
        }
    }

    override fun onStartJob(params: JobParameters?): Boolean {
        try {
            val prefs = CorePrefs(this)
            if (!prefs.shouldRunCore) {
                // The operator turned the Core off. Stop checking, and stop
                // costing them a wakeup every quarter hour.
                cancel(this)
                return false
            }
            if (!CoreService.isUp()) {
                Log.i(TAG, "Core should be running and is not; restarting")
                if (!CoreService.start(this)) {
                    // Android 12+ refuses a background foreground-service start
                    // unless the app is exempt from battery optimisation. This
                    // is the failure mode that looks like "the watchdog does
                    // nothing", so it is logged as the specific thing it is.
                    Log.w(
                        TAG,
                        "restart refused: a background start needs the " +
                            "battery-optimisation exemption on Android 12+",
                    )
                }
            }
        } catch (t: Throwable) {
            Log.w(TAG, "watchdog pass failed: ${t.javaClass.simpleName}")
        }
        // false: all the work happened synchronously and it was trivial.
        return false
    }

    override fun onStopJob(params: JobParameters?): Boolean = false
}
