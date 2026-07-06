package dev.wavr.mobile.wavrsensor

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import dev.wavr.mobile.securestorage.SecureKeyStore
import dev.wavr.mobile.wavrnet.PinMismatchException
import dev.wavr.mobile.wavrnet.PinnedClient
import java.net.URI
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong
import javax.net.ssl.SSLPeerUnverifiedException
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import kotlin.concurrent.thread

/**
 * SensorStreamService — the app's SECOND native egress: a dataSync foreground service
 * whose 1 Hz sample+POST loop streams telemetry to the paired central. It REUSES the
 * app's one proven trust anchor and adds nothing:
 *
 *  - TRANSPORT: every POST goes through [PinnedClient] — the SAME pinned-TLS OkHttp
 *    factory WavrNet uses, trusting EXACTLY the one SHA-256 fingerprint captured at
 *    pairing. No second HTTP stack, no new TrustManager, no system trust store.
 *  - SECRETS: {centralUrl, pinnedFp, token} are read from [SecureKeyStore] (the same
 *    Keystore-backed store the shim writes at pairing) ON EVERY TICK, natively. The
 *    token and fingerprint NEVER cross into JS and NEVER ride an Intent.
 *  - PEER: the ONLY network peer is the stored centralUrl + "/api/telemetry". There is
 *    no fallback URL, no cleartext mode, no legacy :9231 path.
 *
 * FAIL-CLOSED CONTRACT (the whole point):
 *  - Cert swap  => PinMismatchException from PinnedClient's TrustManager => the loop
 *    STOPS and the service exits: state ERROR / lastError "PIN_MISMATCH" (+ best-effort
 *    presentedFp for old-vs-new display). Never retried, never re-pinned, never
 *    downgraded to cleartext or trust-all.
 *  - 401/403    => the token is WIPED from SecureKeyStore (mirrors the shim's
 *    companionAuthFailed) and the service stops: ERROR / "AUTH_REVOKED". The shim's
 *    status listener drops the UI to pairing.
 *  - Unpaired (missing/corrupt url/fp/token)          => ERROR / "NOT_PAIRED", no POST.
 *  - Stored central URL not https:// (no [tls] extra) => ERROR / "NO_TLS" — an explicit
 *    error, NEVER a silent cleartext POST.
 *  - Transient failures (timeout, refused, non-auth non-2xx) increment `err` and keep
 *    streaming — only trust/auth/pairing failures are terminal.
 *
 * LIFECYCLE: started (not bound) so it survives WebView reloads, screen-off and Doze
 * (foreground importance). The loop runs on a plain worker thread INSIDE the service —
 * zero JS in the loop. A monotonic run-id guards against a stale loop double-sending
 * after a rapid Stop→Start; Stop interrupts the tick sleep so it lands within ~1 s.
 * START_REDELIVER_INTENT resumes a user-started stream if the system kills the process.
 * Android 15+ enforces a runtime budget on dataSync services; onTimeout() stops
 * cleanly with ERROR / "FGS_TIMEOUT" instead of ANRing.
 *
 * LOGGING (logcat is hostile): never the token, Bearer header, device name, SSID/BSSID,
 * payload, or full URL. The few log lines carry host:port and machine codes only.
 */
class SensorStreamService : Service() {

    private val runId = AtomicLong(0)
    @Volatile private var loopThread: Thread? = null
    private lateinit var sampler: TelemetrySampler
    private lateinit var store: SecureKeyStore
    private val mainHandler = Handler(Looper.getMainLooper())

    // Pin-preserving derived clients, memoized per normalized fingerprint (in practice
    // exactly one). newBuilder() COPIES the pinned TrustManager + hostname verifier and
    // SHARES the connection pool — same trust anchor, same pinned handshake — adding
    // only a call timeout so a wedged LAN POST cannot stall the tick for 30 s.
    private val postClients = ConcurrentHashMap<String, OkHttpClient>()

    override fun onCreate() {
        super.onCreate()
        sampler = TelemetrySampler(applicationContext)
        store = SecureKeyStore(applicationContext)
    }

    override fun onBind(intent: Intent?): IBinder? = null   // started service; status rides StreamStatusBus

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopLoop()
                StreamStatusBus.publish(
                    StreamStatusBus.current.copy(
                        running = false, state = "IDLE", lastError = null, presentedFp = null
                    )
                )
                ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
                stopSelf()
                return START_NOT_STICKY
            }

            ACTION_START -> {
                // startForeground FIRST (must land within 5 s of startForegroundService),
                // with the declared dataSync type.
                startInForeground()
                val name = intent?.getStringExtra(EXTRA_NAME)?.trim().orEmpty()
                if (name.isEmpty() || name.length > MAX_NAME_LEN) {
                    // Plugin validates too; this only fires on a corrupted redelivery.
                    failStop(runId.incrementAndGet(), "START_FAILED")
                    return START_NOT_STICKY
                }
                startLoop(name)
                // Redeliver the start intent if the system kills us mid-stream: the
                // user pressed Start; resuming honors that. (Terminal errors and Stop
                // call stopSelf, which cancels redelivery.)
                return START_REDELIVER_INTENT
            }

            else -> {
                // Unknown/no action: nothing to do; don't linger in the foreground.
                stopSelf()
                return START_NOT_STICKY
            }
        }
    }

    override fun onDestroy() {
        stopLoop()
        // If we are being torn down NOT via our own stop/fail paths (e.g. external
        // stopService), leave an honest final status. Never overwrite a terminal ERROR.
        val cur = StreamStatusBus.current
        if (cur.running) {
            StreamStatusBus.publish(cur.copy(running = false, state = "IDLE", presentedFp = null))
        }
        super.onDestroy()
    }

    /**
     * Android 15+ (targetSdk 35+): dataSync foreground services have a system runtime
     * budget (~6 h/day). The system calls this when it expires; stopping promptly is
     * mandatory (an ANR follows otherwise). Surface it as a terminal ERROR so the node
     * screen shows WHY streaming ended instead of silently dying.
     */
    override fun onTimeout(startId: Int, fgsType: Int) {
        Log.w(TAG, "dataSync FGS budget exhausted (FGS_TIMEOUT) — stopping stream")
        failStop(runId.incrementAndGet(), "FGS_TIMEOUT")
    }

    // ---- loop management -------------------------------------------------------------

    private fun startLoop(deviceName: String) {
        val myRun = runId.incrementAndGet()   // supersedes any previous loop instantly
        loopThread?.interrupt()               // wake a superseded loop out of its sleep
        sampler.start()
        loopThread = thread(name = "WavrSensorPost") { runLoop(myRun, deviceName) }
    }

    private fun stopLoop() {
        runId.incrementAndGet()               // every live loop's guard now fails
        loopThread?.interrupt()
        loopThread = null
        sampler.stop()
    }

    /**
     * The 1 Hz sample+POST loop. Runs entirely on this worker thread — no JS, no
     * WebView, nothing Doze can throttle. Secrets are re-read from the Keystore each
     * tick so a token rotation or an unpair takes effect within one second.
     */
    private fun runLoop(myRun: Long, deviceName: String) {
        var sent = 0
        var err = 0
        var lastError: String? = null
        var ticks = 0
        StreamStatusBus.publish(StreamStatusBus.Status(true, "STREAMING", 0, 0, null))

        while (runId.get() == myRun) {
            val tickStart = SystemClock.elapsedRealtime()

            val url = store.get(K_URL)
            val fp = store.get(K_FP)
            val token = store.get(K_TOKEN)
            if (url.isNullOrBlank() || fp.isNullOrBlank() || token.isNullOrBlank()) {
                failStop(myRun, "NOT_PAIRED", sent = sent, err = err)
                return
            }
            if (!url.startsWith("https://", ignoreCase = true)) {
                // A central without the [tls] extra has nothing to pin: explicit error,
                // NEVER a silent cleartext POST.
                failStop(myRun, "NO_TLS", sent = sent, err = err)
                return
            }

            try {
                val client = try {
                    postClientFor(fp)
                } catch (_: IllegalArgumentException) {
                    // Stored fingerprint is not 64 hex chars: the pairing record is
                    // unusable. Fail closed as unpaired — never fall back unpinned.
                    failStop(myRun, "NOT_PAIRED", sent = sent, err = err)
                    return
                }
                val payload = sampler.buildPayload(deviceName)
                val request = Request.Builder()
                    .url(url.trimEnd('/') + TELEMETRY_PATH)
                    .header("Authorization", "Bearer $token")   // never logged
                    .post(payload.toString().toRequestBody(JSON_MEDIA_TYPE))
                    .build()
                client.newCall(request).execute().use { resp ->
                    when {
                        resp.isSuccessful -> {
                            sent++
                            lastError = null
                        }
                        resp.code == 401 || resp.code == 403 -> {
                            // Central revoked us: wipe the token (mirrors the shim's
                            // companionAuthFailed) and stop. The status event tells the
                            // shim to drop to pairing.
                            try {
                                store.remove(K_TOKEN)
                            } catch (e: Exception) {
                                Log.w(TAG, "token wipe failed (${e.javaClass.simpleName})")
                            }
                            Log.w(TAG, "auth revoked (HTTP ${resp.code}) by ${redactedHost(url)} — token wiped, stream stopped")
                            failStop(myRun, "AUTH_REVOKED", sent = sent, err = err)
                            return
                        }
                        else -> {
                            // Non-auth HTTP error (e.g. central not yet serving
                            // /api/telemetry): transient — count it, keep streaming.
                            err++
                            lastError = "HTTP_${resp.code}"
                        }
                    }
                }
            } catch (e: InterruptedException) {
                return   // stopLoop() — the ACTION_STOP path publishes the final status
            } catch (e: Exception) {
                if (isPinFailure(e)) {
                    // THE fail-closed moment: the server is not presenting the pinned
                    // certificate. Stop everything; best-effort read of the presented
                    // fp for the shim's old-vs-new card (probe carries no data).
                    Log.w(TAG, "TLS pin mismatch for ${redactedHost(url)} — sensor stream fail-closed")
                    failStop(myRun, "PIN_MISMATCH", presentedFp = safeProbeFp(url), sent = sent, err = err)
                    return
                }
                // Transient transport failure (timeout, refused, reset): count and
                // continue. Machine code only — exception messages can embed URLs.
                err++
                lastError = "NETWORK"
            }

            if (runId.get() != myRun) return   // superseded while POSTing: exit silently
            StreamStatusBus.publish(StreamStatusBus.Status(true, "STREAMING", sent, err, lastError))
            ticks++
            if (ticks % NOTIF_UPDATE_TICKS == 0) updateNotification(url, sent, err)

            val wait = TICK_MS - (SystemClock.elapsedRealtime() - tickStart)
            if (wait > 0) {
                try {
                    Thread.sleep(wait)   // interruptible: Stop lands within the same second
                } catch (_: InterruptedException) {
                    return   // interrupt == stop/supersede; guard above decides the rest
                }
            }
        }
    }

    /**
     * Terminal stop: publish ERROR (JS sees WHY), then leave the foreground and stop.
     * Acts only if [myRun] is still the live run — a superseded loop must neither
     * clobber its successor's status nor tear down the service its successor owns.
     */
    private fun failStop(
        myRun: Long,
        code: String,
        presentedFp: String? = null,
        sent: Int = StreamStatusBus.current.sent,
        err: Int = StreamStatusBus.current.err,
    ) {
        if (!runId.compareAndSet(myRun, myRun + 1)) return   // superseded: successor owns the service
        StreamStatusBus.publish(
            StreamStatusBus.Status(false, "ERROR", sent, err, code, presentedFp)
        )
        mainHandler.post {
            // Only stop if no NEW run started since this terminal failure.
            if (runId.get() == myRun + 1) {
                sampler.stop()
                ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
                stopSelf()
            }
        }
    }

    // ---- pinned transport helpers ------------------------------------------------------

    private fun postClientFor(pinnedFp: String): OkHttpClient {
        val normalized = PinnedClient.normalizeFp(pinnedFp)
        return postClients.computeIfAbsent(normalized) {
            PinnedClient.clientFor(pinnedFp)   // THE trust anchor — throws on bad fp
                .newBuilder()                  // copies pinned TrustManager + verifier
                .callTimeout(POST_TIMEOUT_MS, TimeUnit.MILLISECONDS)
                .followRedirects(false)
                .followSslRedirects(false)
                .build()
        }
    }

    /** Same classification as WavrNetPlugin: PIN_MISMATCH iff the pinned TrustManager
     *  (or the pinned hostname verifier) said no — never for generic TLS noise. */
    private fun isPinFailure(t: Throwable?): Boolean {
        var cur = t
        var depth = 0
        while (cur != null && depth < 8) {
            if (cur is PinMismatchException || cur is SSLPeerUnverifiedException) return true
            cur = cur.cause
            depth++
        }
        return false
    }

    /** Best-effort read of the cert the server is NOW presenting (probe: bare TLS
     *  handshake, no data, no trust established). Never throws. */
    private fun safeProbeFp(url: String): String? = try {
        PinnedClient.probeLeafFingerprint(url)
    } catch (_: Exception) {
        null
    }

    /** host:port only — safe for logs and the notification (no path/query/credentials). */
    private fun redactedHost(url: String): String = try {
        val u = URI(url)
        "${u.host}:${if (u.port == -1) 443 else u.port}"
    } catch (_: Exception) {
        "<unparseable-url>"
    }

    // ---- foreground notification -------------------------------------------------------

    private fun startInForeground() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.createNotificationChannel(
            NotificationChannel(NOTIF_CHANNEL_ID, "Wavr sensor streaming", NotificationManager.IMPORTANCE_LOW)
        )
        ServiceCompat.startForeground(
            this,
            NOTIF_ID,
            buildNotification("Streaming sensor data to your hub"),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
        )
    }

    private fun updateNotification(url: String, sent: Int, err: Int) {
        try {
            val body = buildString {
                append("Streaming to ").append(redactedHost(url))
                append(" · sent ").append(sent)
                if (err > 0) append(" · ").append(err).append(" err")
            }
            (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
                .notify(NOTIF_ID, buildNotification(body))
        } catch (_: Exception) {
            // cosmetic — never let a notification failure break the loop
        }
    }

    private fun buildNotification(body: String): Notification {
        val tap = packageManager.getLaunchIntentForPackage(packageName)?.let {
            PendingIntent.getActivity(this, 0, it, PendingIntent.FLAG_IMMUTABLE)
        }
        return NotificationCompat.Builder(this, NOTIF_CHANNEL_ID)
            .setSmallIcon(applicationInfo.icon)
            .setContentTitle("Wavr sensor")
            .setContentText(body)      // host:port + counters only — never SSID/token/name
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .setContentIntent(tap)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }

    companion object {
        private const val TAG = "WavrSensor"

        const val ACTION_START = "dev.wavr.mobile.wavrsensor.START"
        const val ACTION_STOP = "dev.wavr.mobile.wavrsensor.STOP"
        /** Device name for the payload's `device` field — NOT a secret, never logged. */
        const val EXTRA_NAME = "name"
        const val MAX_NAME_LEN = 64

        private const val TICK_MS = 1000L            // 1 Hz, matching the legacy node
        private const val POST_TIMEOUT_MS = 5000L    // legacy POST_TIMEOUT_MS: no wedged ticks
        private const val NOTIF_UPDATE_TICKS = 5     // notification refresh cadence (~5 s)
        private const val TELEMETRY_PATH = "/api/telemetry"

        private const val NOTIF_CHANNEL_ID = "wavr_sensor_stream"
        private const val NOTIF_ID = 42071

        private val JSON_MEDIA_TYPE = "application/json".toMediaType()

        // Keystore keys — MUST match the shim / wavr-secure-storage contract
        // (mobile/src/wavr-mobile-shim.js:65; wavr-secure-storage.d.ts).
        private const val K_URL = "wavr.centralUrl"
        private const val K_FP = "wavr.pinnedFp"
        private const val K_TOKEN = "wavr.token"
    }
}
