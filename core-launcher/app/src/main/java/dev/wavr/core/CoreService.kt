package dev.wavr.core

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.util.Log
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.LifecycleRegistry
import org.json.JSONObject
import java.io.File
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * The supervisor. Everything a Wavr Core needs from Android that Python cannot
 * ask for itself lives here: a process that outlives the screen, a notification
 * that explains itself, a lifecycle CameraX can bind to, and a thermal budget.
 *
 * ## Why a service and not the Activity
 *
 * The kiosk Activity is a *renderer*. A Core that stops sensing when the user
 * swipes away, or when the panel is not in front, is not a Core. The Python
 * runtime therefore belongs to this service, and the Activity is one of its
 * clients — it renders `https://localhost:<port>` exactly as it renders a Core
 * on another machine. That separation is also what makes "this phone is a Node
 * for someone else's Core" expressible without a second app.
 *
 * ## Foreground service type
 *
 * `specialUse` on API 34+, because that is honestly what this is: the user's own
 * server, running on the user's own device, for the user. The tempting
 * alternative, `dataSync`, is wrong twice — it describes transferring data to or
 * from a remote endpoint (Wavr's whole point is that there isn't one), and since
 * Android 15 it is time-capped at six hours a day, which would silently kill a
 * house brain every afternoon. On API 29..33, where `specialUse` does not exist,
 * `dataSync` is the only sensible stand-in and carries no cap on those releases.
 * `camera` and `connectedDevice` are OR-ed in only while those things are
 * actually in use, never asserted speculatively.
 *
 * ## What this service does NOT do
 *
 * It holds **no wake lock**. See [PowerPolicy] — the Termux Core solved Doze with
 * a permanent one and the cost was a phone that never sleeps. It also makes no
 * decisions about fusion, devices, or pairing: it hands an environment to Python
 * and reports what Python says back.
 */
class CoreService : Service(), LifecycleOwner {

    companion object {
        private const val TAG = "WavrCore"

        const val ACTION_START = "dev.wavr.core.action.START"
        const val ACTION_STOP = "dev.wavr.core.action.STOP"

        private const val CHANNEL_ID = "wavr_core_runtime"
        private const val NOTIFICATION_ID = 0x57415652 // "WAVR"

        /** How often the supervisor re-reads the Core's own health. */
        private const val HEALTH_POLL_MS = 15_000L

        /** Live handle for the Activity bridge; null when the service is down. */
        @Volatile
        private var instance: CoreService? = null

        /**
         * Ask Android to bring the Core up. Safe to call repeatedly.
         *
         * @return true if the request was accepted. FALSE IS A REAL OUTCOME, not
         * a formality: since Android 12 an app may not start a foreground
         * service from the background, and the caller matters —
         *   * from [MainActivity] (a visible Activity): always allowed;
         *   * from [BootReceiver]: allowed, BOOT_COMPLETED is exempt;
         *   * from [CoreWatchdogJob] (a plain periodic job): **allowed only if
         *     the app holds a battery-optimisation exemption**.
         *
         * That last line is why [PowerPolicy]'s ask is not merely about speed:
         * without the exemption the crash-recovery watchdog cannot restart the
         * Core, and the operator deserves to be told that rather than discover
         * it after a silent outage.
         */
        fun start(context: Context): Boolean = try {
            val intent = Intent(context, CoreService::class.java).setAction(ACTION_START)
            ContextCompat.startForegroundService(context.applicationContext, intent)
            true
        } catch (t: Throwable) {
            // ForegroundServiceStartNotAllowedException on API 31+, and anything
            // an OEM throws in its place. Named, not swallowed.
            Log.w(TAG, "foreground start refused: ${t.javaClass.simpleName}")
            lastStartRefusal = t.javaClass.simpleName
            false
        }

        /** Why the last start request was refused, if it was. Read by the panel. */
        @Volatile
        var lastStartRefusal: String? = null
            private set

        /**
         * Restart the Core by restarting the PROCESS, which is the only restart
         * that works.
         *
         * Measured, not assumed: the embedded Core can be started exactly once
         * per interpreter. `wavr.app` builds its stores as module-level
         * singletons and closes them in its shutdown handler, so a second
         * `start()` in the same process brings uvicorn back up on top of closed
         * SQLite handles -- it logs `Cannot operate on a closed database`, the
         * sensing sources crash, and from the outside it looks healthy. Python
         * refuses that now (`wavr_android._STOPPED_ONCE`); this is the Kotlin
         * half of the same contract.
         *
         * Killing our own process is deliberate and it is the cheap option. The
         * alternatives are worse: an AlarmManager restart cannot start a
         * foreground service on Android 12+ without SCHEDULE_EXACT_ALARM (a
         * special-access permission, for something that is not an alarm), and
         * `importlib.reload` across 128 modules is not a fix but a different set
         * of bugs. This app is registered as HOME, so Android relaunches
         * MainActivity immediately and `onCreate` starts the Core again from a
         * foreground Activity, which is always allowed. On a device where Wavr
         * is not HOME the operator taps the icon -- and the UI says so rather
         * than pretending the switch took effect.
         */
        fun requestRestart(context: Context) {
            val prefs = CorePrefs(context)
            if (!prefs.isCore) return
            prefs.desiredRunning = true
            CoreWatchdogJob.schedule(context)
            android.os.Process.killProcess(android.os.Process.myPid())
        }

        /** Operator-initiated stop. Clears the intent so nothing resurrects it. */
        fun stop(context: Context) {
            CorePrefs(context).desiredRunning = false
            context.applicationContext.startService(
                Intent(context, CoreService::class.java).setAction(ACTION_STOP)
            )
        }

        /** True while the service object exists. Not "the Core is serving". */
        fun isUp(): Boolean = instance != null

        /**
         * The service's camera streamer, or null when the service is down.
         *
         * There must be exactly ONE streamer per process — two would fight over
         * the camera device and over loopback port 8081 — so the Activity asks
         * here first and only falls back to its own when there is no service.
         */
        fun streamerIfUp(): CameraMjpegStreamer? = instance?.cameraStreamer()

        /**
         * Ask the CORE what this device should become. Kotlin supplies the
         * manifest (only Android can see the hardware); `capabilities.recommend()`
         * supplies the verdict and the reasons. One recommendation engine, in
         * Python, for every platform — ADR-0010 decision 6.
         */
        fun recommendJson(context: Context): String = try {
            val runtime = PythonRuntime.resolve()
            if (!runtime.bundled()) {
                "{\"ok\":false,\"error\":\"no_runtime\"}"
            } else {
                val manifest =
                    AndroidCapabilities.manifestJson(context, runtimeCanHostCore = true)
                instance?.recommend(manifest)
                    ?: "{\"ok\":false,\"error\":\"service_down\"," +
                    "\"detail\":\"Start the Wavr Core service to get a recommendation.\"}"
            }
        } catch (t: Throwable) {
            "{\"ok\":false,\"error\":\"recommend_failed\"}"
        }

        /**
         * `{"service":..,"runtime":{..},"thermal":..,"shed":..}` — the single
         * shape the panel reads. Never throws.
         */
        fun statusJson(context: Context): String = try {
            val svc = instance
            JSONObject().apply {
                put("service", svc != null)
                put("thermal", svc?.thermalLabel() ?: "unknown")
                put("shed", svc?.shedLabel() ?: "none")
                val runtime = JSONObject(PythonRuntime.resolve().status())
                put("runtime", runtime)
                // Hoisted to the top level because it is the one flag the panel
                // must act on: a setting was changed that the running process
                // cannot adopt.
                put("restartRequired", runtime.optBoolean("restartRequired", false))
                put("port", CorePrefs(context).port)
                put("lan", CorePrefs(context).lanMode)
                lastStartRefusal?.let { put("startRefused", it) }
                // The watchdog can only resurrect a crashed Core from the
                // background when this is true (see start()).
                put("watchdogCanRestart", PowerPolicy.isExempt(context))
            }.toString()
        } catch (t: Throwable) {
            "{\"service\":false,\"thermal\":\"unknown\",\"shed\":\"none\"}"
        }
    }

    // -- Lifecycle plumbing so CameraX can bind to the SERVICE ---------------
    //
    // CameraX needs a LifecycleOwner. Today the streamer binds to the Activity,
    // which means the camera dies with the panel — fine for a kiosk, wrong for a
    // Core that must keep sensing with the screen off. So the service is its own
    // LifecycleOwner and the camera's owner becomes the thing that is actually
    // meant to stay alive.
    //
    // Driven by hand rather than by `androidx.lifecycle:lifecycle-service`'s
    // ServiceLifecycleDispatcher: that artifact is not in this project's
    // dependency set, and this project pins every version and builds offline, so
    // twelve lines here is cheaper than a new coordinate. LifecycleRegistry
    // requires main-thread dispatch, which is exactly where Service callbacks
    // already run.
    private val registry = LifecycleRegistry(this)
    override val lifecycle: Lifecycle get() = registry

    private val main = Handler(Looper.getMainLooper())
    private val worker = Executors.newSingleThreadExecutor { r ->
        Thread(r, "wavr-core-supervisor").apply { isDaemon = true }
    }

    private lateinit var prefs: CorePrefs
    private val runtime: PythonRuntime by lazy { PythonRuntime.resolve() }

    /** Guards against a second start while the first is still booting Python. */
    private val booting = AtomicBoolean(false)

    /** Last JSON the runtime returned from start(); surfaced verbatim. */
    @Volatile private var lastStartResult: String? = null

    /** Current thermal status (PowerManager.THERMAL_STATUS_*), -1 = unknown. */
    @Volatile private var thermal: Int = -1

    /** What we have shed because of heat: "none" | "framerate" | "camera". */
    @Volatile private var shed: String = "none"

    /**
     * Set the instant teardown starts. The camera's streaming callback can land
     * after onDestroy (it is posted to the main looper), and calling
     * startForeground on a dead service would throw into the catch below, which
     * calls stopSelf on an already-stopping service. Cheaper to just not.
     */
    @Volatile private var destroyed = false

    /** The on-device camera source, now owned by the service, not the Activity. */
    private var camera: CameraMjpegStreamer? = null

    private var thermalListener: PowerManager.OnThermalStatusChangedListener? = null

    /**
     * Cached answer to "has this process burned its one Core start?".
     *
     * Cached rather than read live because reading it means calling into
     * CPython, and a Chaquopy call takes the GIL. Doing that from the main
     * thread every 15 seconds puts the UI behind whatever uvicorn happens to be
     * doing -- a jank source at best and an ANR at worst. The supervisor thread
     * refreshes it; the notification only ever reads the field.
     */
    @Volatile private var cachedRestartRequired = false

    private val healthPoll = object : Runnable {
        override fun run() {
            // Python on the worker, notification on main. Never the other way.
            if (!worker.isShutdown) {
                worker.execute {
                    cachedRestartRequired = try {
                        JSONObject(runtime.status()).optBoolean("restartRequired", false)
                    } catch (t: Throwable) {
                        cachedRestartRequired
                    }
                    main.post { refreshNotification() }
                }
            }
            main.postDelayed(this, HEALTH_POLL_MS)
        }
    }

    // =====================================================================
    // Service lifecycle
    // =====================================================================

    override fun onCreate() {
        super.onCreate()
        registry.handleLifecycleEvent(Lifecycle.Event.ON_CREATE)
        instance = this
        prefs = CorePrefs(this)
        createChannel()
        attachThermalListener()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        // RESUMED is what CameraX waits for before it will open a camera. The
        // service reaches it as soon as it is started and stays there until it
        // is destroyed — there is no "paused service".
        if (registry.currentState != Lifecycle.State.RESUMED) {
            registry.handleLifecycleEvent(Lifecycle.Event.ON_START)
            registry.handleLifecycleEvent(Lifecycle.Event.ON_RESUME)
        }

        // The notification must be up within a few seconds of the start request
        // or Android kills us with a ForegroundServiceDidNotStartInTimeException.
        // So: foreground FIRST, Python second, on a worker.
        goForeground()

        when (intent?.action) {
            ACTION_STOP -> {
                shutdownCore(operatorInitiated = true)
                return START_NOT_STICKY
            }
            else -> {
                prefs.desiredRunning = true
                bootCoreAsync()
            }
        }

        main.removeCallbacks(healthPoll)
        main.postDelayed(healthPoll, HEALTH_POLL_MS)

        // START_STICKY: if Android reclaims the process under memory pressure it
        // recreates the service with a null Intent, which lands in the `else`
        // branch above and boots the Core again. That is the behaviour we want,
        // and it is why the branch must be idempotent.
        return START_STICKY
    }

    override fun onDestroy() {
        destroyed = true
        main.removeCallbacks(healthPoll)
        detachThermalListener()
        camera?.shutdown()
        camera = null
        // Deliberately NOT calling runtime.stop() here on a system-initiated
        // destroy: if Android is reclaiming us, the process is going anyway and
        // uvicorn goes with it. Calling into Python during teardown is how you
        // get a hang instead of a clean death.
        instance = null
        worker.shutdownNow()
        // Unbinds anything still observing (CameraX included) before the object
        // goes away, in the order LifecycleRegistry requires.
        registry.handleLifecycleEvent(Lifecycle.Event.ON_PAUSE)
        registry.handleLifecycleEvent(Lifecycle.Event.ON_STOP)
        registry.handleLifecycleEvent(Lifecycle.Event.ON_DESTROY)
        super.onDestroy()
    }

    // =====================================================================
    // Core boot / shutdown
    // =====================================================================

    private fun bootCoreAsync() {
        if (!booting.compareAndSet(false, true)) return
        worker.execute {
            try {
                val env = buildEnvironment()
                val result = runtime.start(this, env)
                lastStartResult = result
                cachedRestartRequired = result.contains("\"restartRequired\":true")
                Log.i(TAG, "Core start returned ok=${result.contains("\"ok\":true")}")
            } catch (t: Throwable) {
                Log.w(TAG, "Core boot failed: ${t.javaClass.name}")
                lastStartResult = "{\"ok\":false,\"error\":\"boot_failed\"}"
            } finally {
                booting.set(false)
                main.post { refreshNotification() }
            }
        }
    }

    private fun shutdownCore(operatorInitiated: Boolean) {
        if (operatorInitiated) prefs.desiredRunning = false
        worker.execute {
            try {
                runtime.stop()
                // Stopping is exactly the event that makes a restart necessary;
                // record it here rather than waiting for the next poll.
                cachedRestartRequired = true
            } catch (t: Throwable) {
                Log.w(TAG, "Core stop failed: ${t.javaClass.name}")
            }
            main.post {
                ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
                stopSelf()
            }
        }
    }

    /**
     * The environment the Core runs under. Every path is inside
     * [Context.getFilesDir] — app-private, `0700`, not on external storage, not
     * world-readable, and removed by uninstall. The proot Core kept `wavr.db` on
     * the shared SD card, where any app with storage permission could read it;
     * this is strictly tighter and needs no permission at all.
     */
    private fun buildEnvironment(): Map<String, String> {
        val files = filesDir
        val tls = File(files, "tls").apply { mkdirs() }
        val frontend = AssetStaging.ensureFrontend(this)

        val env = linkedMapOf(
            // -- storage, all app-private -------------------------------
            "WAVR_DB" to File(files, "wavr.db").absolutePath,
            "WAVR_HOUSE_MAP" to File(files, "house.json").absolutePath,
            // HOME steers anything that resolves `~` (tls.py's `~/.wavr/`
            // fallback, dotenv, tempfile heuristics) into app-private storage
            // instead of `/` or `/data`, where it would silently fail.
            "HOME" to files.absolutePath,
            "TMPDIR" to cacheDir.absolutePath,

            // -- the dashboard ------------------------------------------
            // app.py's `_find_frontend()` honours this; without it the Core
            // serves the API and 404s on GET / inside an APK.
            "WAVR_FRONTEND" to frontend.absolutePath,

            // -- listener -----------------------------------------------
            "WAVR_PORT" to prefs.port.toString(),
        )

        if (prefs.lanMode) {
            // ADR-0006's opt-in, and nothing more than it: subnet + per-device
            // token + self-signed TLS. Enabled only by an explicit switch.
            env["WAVR_MULTIDEVICE"] = "1"
            env["WAVR_BIND"] = "0.0.0.0"
            env["WAVR_TLS_CERT"] = File(tls, "wavr-cert.pem").absolutePath
            env["WAVR_TLS_KEY"] = File(tls, "wavr-key.pem").absolutePath
        } else {
            // ADR-0002 §1, unchanged: loopback only. Setting the values
            // explicitly rather than relying on defaults means a leftover
            // process environment can never quietly widen the bind.
            env["WAVR_MULTIDEVICE"] = ""
            env["WAVR_BIND"] = "127.0.0.1"
        }

        return env
    }

    // =====================================================================
    // Thermal governor
    // =====================================================================

    /**
     * A mid-range phone in a stand has no airflow, and the Core is a permanent
     * workload. Rather than wait for Android to throttle the whole CPU — which
     * makes the *API* slow, the one thing that must stay responsive — we shed
     * sensing load first and say that we did.
     *
     * The order matters and is deliberate: frame rate before the camera, camera
     * before anything else, and the HTTP/WebSocket surface never. A Core that
     * answers "I am too hot to look right now" is useful; a Core that stops
     * answering is not.
     */
    private fun attachThermalListener() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return
        try {
            val pm = getSystemService(Context.POWER_SERVICE) as? PowerManager ?: return
            thermal = pm.currentThermalStatus
            val listener = PowerManager.OnThermalStatusChangedListener { status ->
                thermal = status
                applyThermalPolicy(status)
                refreshNotification()
            }
            pm.addThermalStatusListener(mainExecutor, listener)
            thermalListener = listener
            applyThermalPolicy(thermal)
        } catch (t: Throwable) {
            Log.w(TAG, "thermal listener unavailable: ${t.javaClass.simpleName}")
        }
    }

    private fun detachThermalListener() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return
        try {
            val pm = getSystemService(Context.POWER_SERVICE) as? PowerManager
            thermalListener?.let { pm?.removeThermalStatusListener(it) }
        } catch (t: Throwable) {
            // tearing down
        } finally {
            thermalListener = null
        }
    }

    private fun applyThermalPolicy(status: Int) {
        val cam = camera
        // Report only what was ACTUALLY shed. With no camera bound there is
        // nothing to give up, and a notification reading "Wavr has switched its
        // camera off because the phone is hot" when no camera was ever on is a
        // small lie that teaches the operator to distrust the big ones.
        val wasStreaming = cam?.isStreaming() == true
        shed = when {
            status >= PowerManager.THERMAL_STATUS_SEVERE -> {
                // Release the camera device entirely (green light off) — the
                // single biggest sustained draw this app can create.
                if (wasStreaming) {
                    cam?.setEnabled(false)
                    "camera"
                } else {
                    "none"
                }
            }
            status >= PowerManager.THERMAL_STATUS_MODERATE -> {
                cam?.setThermalDerate(true)
                if (wasStreaming) "framerate" else "none"
            }
            else -> {
                cam?.setThermalDerate(false)
                "none"
            }
        }
    }

    fun thermalLabel(): String = when (thermal) {
        PowerManager.THERMAL_STATUS_NONE -> "none"
        PowerManager.THERMAL_STATUS_LIGHT -> "light"
        PowerManager.THERMAL_STATUS_MODERATE -> "moderate"
        PowerManager.THERMAL_STATUS_SEVERE -> "severe"
        PowerManager.THERMAL_STATUS_CRITICAL -> "critical"
        PowerManager.THERMAL_STATUS_EMERGENCY -> "emergency"
        PowerManager.THERMAL_STATUS_SHUTDOWN -> "shutdown"
        else -> "unknown"
    }

    fun shedLabel(): String = shed

    /**
     * True once this process's interpreter has served and stopped. Reads the
     * cache, never CPython -- see [cachedRestartRequired].
     */
    private fun runtimeSaysRestartRequired(): Boolean = cachedRestartRequired

    // =====================================================================
    // Camera, owned by the service
    // =====================================================================

    /**
     * The service's camera source. Bound to the SERVICE lifecycle, so it keeps
     * running with the screen off — and, exactly as before, it is **off until
     * asked** (ADR-0002 §2) and cannot request its own permission, because a
     * service has no UI. If the grant is missing the streamer reports
     * `permission_denied` and the panel does the asking.
     */
    /**
     * Hand the Android manifest to the Core and return its recommendation
     * verbatim. Runs on the caller's thread and must therefore not be called
     * from the UI thread while the interpreter is still booting — the bridge
     * only calls it once [PythonRuntime.bundled] and the service are both up.
     */
    private fun recommend(manifestJson: String): String = try {
        val py = PythonRuntime.resolve()
        if (!py.bundled()) "{\"ok\":false,\"error\":\"no_runtime\"}"
        else PythonRuntime.Chaquopy.recommend(manifestJson)
    } catch (t: Throwable) {
        "{\"ok\":false,\"error\":\"recommend_failed\"}"
    }

    fun cameraStreamer(): CameraMjpegStreamer {
        camera?.let { return it }
        val created = CameraMjpegStreamer(
            context = this,
            lifecycleOwner = this,
            requestCameraPermission = null, // a Service cannot show a dialog
            // Android 14+ refuses camera access from a foreground service whose
            // declared running type does not include `camera`. We assert types
            // by what is actually in use, so the moment that changes the
            // notification has to be re-posted with the new mask — otherwise the
            // first frame after the camera opens throws SecurityException.
            onStreamingChanged = { main.post { goForeground() } },
        )
        camera = created
        return created
    }

    // =====================================================================
    // Notification
    // =====================================================================

    private fun createChannel() {
        val nm = getSystemService(NotificationManager::class.java) ?: return
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.core_channel_name),
            // LOW: permanently visible, never makes a sound. A house brain that
            // pings you every time it restarts is a house brain you mute, and a
            // muted channel is one the operator stops reading.
            NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description = getString(R.string.core_channel_desc)
            setShowBadge(false)
        }
        nm.createNotificationChannel(channel)
    }

    private fun goForeground() {
        if (destroyed) return
        try {
            ServiceCompat.startForeground(
                this,
                NOTIFICATION_ID,
                buildNotification(),
                foregroundServiceType(),
            )
        } catch (t: Throwable) {
            Log.w(TAG, "startForeground refused: ${t.javaClass.simpleName}")
            stopSelf()
        }
    }

    private fun refreshNotification() {
        try {
            getSystemService(NotificationManager::class.java)
                ?.notify(NOTIFICATION_ID, buildNotification())
        } catch (t: Throwable) {
            // A notification we cannot draw is not worth killing the Core over.
        }
    }

    /**
     * Only the types actually in use, computed per call. Declaring `camera` while
     * the camera is closed is the kind of over-claim that gets an app pulled, and
     * it is also just untrue.
     */
    private fun foregroundServiceType(): Int {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return 0
        var mask =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
            } else {
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
            }
        if (camera?.isStreaming() == true) {
            mask = mask or ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA
        }
        return mask
    }

    /**
     * The notification is the app's honesty surface. A permanently-running
     * background process on someone's phone owes them three things and this
     * carries all of them: what is running, why it is running, and how to stop it
     * in one tap.
     */
    private fun buildNotification(): Notification {
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP),
            PendingIntent.FLAG_IMMUTABLE,
        )
        val stop = PendingIntent.getService(
            this,
            1,
            Intent(this, CoreService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE,
        )

        val where = if (prefs.lanMode) {
            getString(R.string.core_scope_lan, prefs.port)
        } else {
            getString(R.string.core_scope_loopback, prefs.port)
        }
        val heat = when (shed) {
            "camera" -> getString(R.string.core_shed_camera)
            "framerate" -> getString(R.string.core_shed_framerate)
            else -> ""
        }
        // "The service is up" and "the Core is serving" are different facts, and
        // conflating them in the one place the operator looks would be the exact
        // dishonesty this notification exists to avoid.
        val pendingRestart = runtimeSaysRestartRequired()
        val body = getString(R.string.core_notification_body, where) +
            (if (heat.isNotEmpty()) "\n\n$heat" else "") +
            (if (pendingRestart) "\n\n" + getString(R.string.core_restart_pending) else "")

        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle(
                if (pendingRestart) getString(R.string.core_notification_title_restart)
                else getString(R.string.core_notification_title)
            )
            .setContentText(if (pendingRestart) getString(R.string.core_restart_pending) else where)
            .setStyle(Notification.BigTextStyle().bigText(body))
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentIntent(open)
            .setOngoing(true)
            .setShowWhen(false)
            .setCategory(Notification.CATEGORY_SERVICE)
            .addAction(
                Notification.Action.Builder(
                    null as android.graphics.drawable.Icon?,
                    getString(R.string.core_notification_stop),
                    stop,
                ).build()
            )
            .build()
    }
}
