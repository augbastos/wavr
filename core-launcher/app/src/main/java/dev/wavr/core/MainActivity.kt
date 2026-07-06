package dev.wavr.core

import android.annotation.SuppressLint
import android.bluetooth.BluetoothManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.graphics.Color
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import android.net.http.SslError
import android.net.wifi.WifiManager
import android.os.BatteryManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.WindowInsets
import android.view.WindowInsetsController
import android.view.WindowManager
import android.webkit.JavascriptInterface
import android.webkit.SslErrorHandler
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import androidx.activity.result.ActivityResultLauncher
import androidx.activity.result.contract.ActivityResultContracts
import androidx.biometric.BiometricManager
import androidx.biometric.BiometricPrompt
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity
import org.json.JSONObject

/**
 * Wavr Core Launcher.
 *
 * A single full-screen [WebView] that renders the local Wavr Core Panel
 * (`https://localhost:8000/?core`) as an ambient kiosk. Registered as a HOME
 * launcher so a dedicated phone boots straight into the panel.
 *
 * Design constraints (see AndroidManifest + themes.xml):
 *  - No ActionBar / title bar (framework Material NoActionBar theme).
 *  - Navigation bar hidden; TOP STATUS BAR kept visible (wifi / bt / battery).
 *  - Landscape only (device lives on its side in a stand).
 *  - Self-signed loopback TLS trusted ONLY for localhost / 127.0.0.1.
 *  - Robust boot: reloads with a short backoff until the backend answers.
 *
 * Native bridge: a minimal [WavrNativeBridge] is exposed to the panel as
 * `window.WavrNative` (system status indicators + biometric unlock). Exposing a
 * JS interface is acceptable here because the page is the trusted local Core
 * loaded over pinned loopback TLS — the surface is deliberately tiny.
 *
 * Extends [FragmentActivity] (not plain Activity) because [BiometricPrompt]
 * requires a FragmentActivity host. FragmentActivity imposes NO Theme.AppCompat
 * requirement, so the framework Material NoActionBar theme keeps working as-is.
 */
class MainActivity : FragmentActivity() {

    private companion object {
        const val CORE_URL = "https://localhost:8000/?core"
        const val RETRY_DELAY_MS = 3000L
        val BG_COLOR = Color.parseColor("#0B0F14")
        val MUTED_COLOR = Color.parseColor("#8A97A5")

        /** Fallback status JSON if everything below fails — never throw into JS. */
        const val STATUS_FALLBACK =
            "{\"wifi\":{\"connected\":false,\"level\":0}," +
                "\"bluetooth\":{\"on\":false}," +
                "\"battery\":{\"level\":0,\"charging\":false}}"

        /** Cheap cache window for getSystemStatus (panel polls ~every 5s). */
        const val STATUS_CACHE_MS = 2000L
    }

    /** Cached status payload + timestamp (getSystemStatus is polled). */
    @Volatile private var statusCache: String = STATUS_FALLBACK
    @Volatile private var statusCacheAt: Long = 0L

    private lateinit var webView: WebView
    private lateinit var placeholder: View

    private val handler = Handler(Looper.getMainLooper())
    private val retryRunnable = Runnable { loadCore() }

    /** True when the current main-frame navigation failed (so we keep retrying). */
    private var pageHadError = false

    /** On-device camera -> loopback MJPEG source. Off by default. */
    private lateinit var cameraStreamer: CameraMjpegStreamer

    /** Runtime CAMERA-permission dialog result -> forwarded to the streamer. */
    private lateinit var cameraPermLauncher: ActivityResultLauncher<String>

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Keep the panel awake indefinitely — it is a wall/stand display.
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        // Camera streamer + its permission launcher. Registering the launcher in
        // onCreate is required (must exist before the activity is STARTED). The
        // camera stays CLOSED and nothing is bound until the Core panel calls
        // WavrNative.setCamera(true).
        cameraPermLauncher = registerForActivityResult(
            ActivityResultContracts.RequestPermission()
        ) { granted -> cameraStreamer.onPermissionResult(granted) }
        cameraStreamer = CameraMjpegStreamer(this) {
            cameraPermLauncher.launch(android.Manifest.permission.CAMERA)
        }

        val root = FrameLayout(this).apply {
            setBackgroundColor(BG_COLOR)
        }

        webView = WebView(this).apply {
            setBackgroundColor(BG_COLOR)
            layoutParams = FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT
            )
        }
        configureWebView(webView)

        placeholder = buildPlaceholder()

        root.addView(webView)
        root.addView(placeholder) // drawn on top while starting / retrying
        setContentView(root)

        applyImmersive()
        loadCore()
    }

    // ---------------------------------------------------------------------
    // WebView configuration
    // ---------------------------------------------------------------------

    @SuppressLint("SetJavaScriptEnabled", "JavascriptInterface")
    private fun configureWebView(view: WebView) {
        // Native bridge for the trusted local Core panel. Available at page load,
        // so `window.WavrNative` exists before the panel's inline JS runs.
        view.addJavascriptInterface(WavrNativeBridge(), "WavrNative")

        view.settings.apply {
            javaScriptEnabled = true          // the Core panel is a JS app
            domStorageEnabled = true          // localStorage / sessionStorage
            // Same-origin wss to localhost works with defaults; do NOT relax
            // mixed-content policy (everything here is https/wss).
            mediaPlaybackRequiresUserGesture = false
            loadWithOverviewMode = true
            useWideViewPort = true
        }

        view.webViewClient = object : WebViewClient() {

            override fun onPageStarted(view: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
                pageHadError = false
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                if (!pageHadError && url != null && !url.startsWith("about:")) {
                    showContent()
                }
            }

            override fun onReceivedError(
                view: WebView?,
                request: WebResourceRequest?,
                error: WebResourceError?
            ) {
                // Only a failed MAIN-FRAME navigation (e.g. backend not up yet)
                // triggers a retry. Sub-resource errors are ignored.
                if (request?.isForMainFrame == true) {
                    onMainFrameFailure()
                }
            }

            override fun onReceivedHttpError(
                view: WebView?,
                request: WebResourceRequest?,
                errorResponse: WebResourceResponse?
            ) {
                if (request?.isForMainFrame == true) {
                    onMainFrameFailure()
                }
            }

            override fun onReceivedSslError(
                view: WebView?,
                handler: SslErrorHandler,
                error: SslError?
            ) {
                // The Core serves a self-signed cert on loopback. Loopback cannot
                // be MITM'd on-device, so we PROCEED for localhost / 127.0.0.1 and
                // for NOTHING else. This is not a global trust-all.
                val host = error?.url?.let { Uri.parse(it).host }
                if (host == "localhost" || host == "127.0.0.1") {
                    handler.proceed()
                } else {
                    handler.cancel()
                }
            }
        }
    }

    // ---------------------------------------------------------------------
    // Load / retry state machine
    // ---------------------------------------------------------------------

    private fun loadCore() {
        pageHadError = false
        showPlaceholder()
        webView.loadUrl(CORE_URL)
    }

    private fun onMainFrameFailure() {
        pageHadError = true
        showPlaceholder()
        handler.removeCallbacks(retryRunnable)
        handler.postDelayed(retryRunnable, RETRY_DELAY_MS)
    }

    private fun showContent() {
        handler.removeCallbacks(retryRunnable)
        placeholder.visibility = View.GONE
    }

    private fun showPlaceholder() {
        placeholder.visibility = View.VISIBLE
    }

    // ---------------------------------------------------------------------
    // Immersive layout — hide nav bar, keep status bar
    // ---------------------------------------------------------------------

    private fun applyImmersive() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            val controller: WindowInsetsController? = window.insetsController
            // Full immersive: hide BOTH the nav bar and the status bar. The panel renders
            // its own clock, so the system status bar only duplicated time + info.
            controller?.hide(WindowInsets.Type.navigationBars() or WindowInsets.Type.statusBars())
            controller?.systemBarsBehavior =
                WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        } else {
            @Suppress("DEPRECATION")
            window.decorView.systemUiVisibility = (
                View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or
                    View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION or
                    View.SYSTEM_UI_FLAG_FULLSCREEN or
                    View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN or
                    View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                )
        }
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) applyImmersive()
    }

    // ---------------------------------------------------------------------
    // Placeholder view: spinner + "Wavr Core starting…"
    // ---------------------------------------------------------------------

    private fun buildPlaceholder(): View {
        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setBackgroundColor(BG_COLOR)
            layoutParams = FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT
            )
        }

        val spinner = ProgressBar(this).apply {
            isIndeterminate = true
        }

        val label = TextView(this).apply {
            text = getString(R.string.starting)
            setTextColor(MUTED_COLOR)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 16f)
            val topPad = (24 * resources.displayMetrics.density).toInt()
            setPadding(0, topPad, 0, 0)
        }

        column.addView(spinner)
        column.addView(label)
        return column
    }

    // ---------------------------------------------------------------------
    // Kiosk / lifecycle
    // ---------------------------------------------------------------------

    /**
     * As a HOME launcher, Back must not escape the kiosk. FragmentActivity's
     * onBackPressed is deprecated in favour of OnBackPressedDispatcher, but a
     * hard no-op override is exactly the kiosk behaviour we want.
     */
    @Suppress("DEPRECATION", "OVERRIDE_DEPRECATION")
    override fun onBackPressed() {
        // no-op
    }

    override fun onResume() {
        super.onResume()
        webView.onResume()
    }

    override fun onPause() {
        webView.onPause()
        super.onPause()
    }

    override fun onDestroy() {
        handler.removeCallbacks(retryRunnable)
        // Release the camera + close the loopback MJPEG server.
        cameraStreamer.shutdown()
        webView.destroy()
        super.onDestroy()
    }

    // =====================================================================
    // Native bridge exposed to the Core panel as window.WavrNative
    // =====================================================================

    /**
     * The ONLY object shared with the WebView. Methods here run on the WebView's
     * private "JavaBridge" worker thread, never the main thread — so anything
     * that touches UI ([requestAuth] -> BiometricPrompt / evaluateJavascript) is
     * hopped onto the UI thread explicitly. No secrets pass through this surface.
     */
    private inner class WavrNativeBridge {

        /**
         * Best-effort snapshot of wifi / bluetooth / battery for the panel's
         * status indicators. Shape:
         * `{"wifi":{"connected":Boolean,"level":Int 0..4},
         *   "bluetooth":{"on":Boolean},
         *   "battery":{"level":Int 0..100,"charging":Boolean}}`
         * Never throws into JS; returns a safe fallback on any failure.
         */
        @JavascriptInterface
        fun getSystemStatus(): String = readSystemStatus()

        /** True iff a strong biometric OR device credential is available+enrolled. */
        @JavascriptInterface
        fun hasBiometric(): Boolean = isBiometricAvailable()

        /**
         * Fire the system biometric/credential prompt. Returns immediately; the
         * outcome is delivered ONLY via a JS callback on the UI thread:
         * `window.wavrOnAuthResult(<true|false>, '<biometric|cancel|error>')`.
         */
        @JavascriptInterface
        fun requestAuth(title: String) {
            runOnUiThread {
                try {
                    startBiometricPrompt(title)
                } catch (t: Throwable) {
                    deliverAuthResult(false, "error")
                }
            }
        }

        // -----------------------------------------------------------------
        // On-device camera MJPEG source (loopback: http://127.0.0.1:8081/video)
        // -----------------------------------------------------------------

        /**
         * Turn the phone's own camera + local MJPEG stream on/off. OFF by default.
         * On first `true` this requests the CAMERA runtime permission if needed.
         * When off, the camera device is released (green light off) and no frame
         * is captured or written anywhere. Never throws into JS.
         */
        @JavascriptInterface
        fun setCamera(on: Boolean) {
            try {
                cameraStreamer.setEnabled(on)
            } catch (t: Throwable) {
                // swallow — state() reflects the real outcome
            }
        }

        /** `{"on":Boolean,"lens":"back|front","port":8081[,"reason":"..."]}`. */
        @JavascriptInterface
        fun getCameraState(): String = try {
            cameraStreamer.state()
        } catch (t: Throwable) {
            "{\"on\":false,\"lens\":\"back\",\"port\":8081,\"reason\":\"error\"}"
        }

        /** Select the lens: "back" (default) or "front". Rebinds live if on. */
        @JavascriptInterface
        fun setCameraLens(lens: String) {
            try {
                cameraStreamer.setLens(lens)
            } catch (t: Throwable) {
                // swallow — state() reflects the real lens
            }
        }
    }

    // ---------------------------------------------------------------------
    // System status
    // ---------------------------------------------------------------------

    private fun readSystemStatus(): String {
        val now = System.currentTimeMillis()
        if (now - statusCacheAt < STATUS_CACHE_MS) return statusCache
        val json = try {
            val (wifiConnected, wifiLevel) = readWifi()
            JSONObject().apply {
                put("wifi", JSONObject().put("connected", wifiConnected).put("level", wifiLevel))
                put("bluetooth", JSONObject().put("on", readBluetoothOn()))
                val (batteryLevel, charging) = readBattery()
                put("battery", JSONObject().put("level", batteryLevel).put("charging", charging))
            }.toString()
        } catch (t: Throwable) {
            STATUS_FALLBACK
        }
        statusCache = json
        statusCacheAt = now
        return json
    }

    /** @return connected-to-wifi flag + a 0..4 signal bucket (0 when not on wifi). */
    private fun readWifi(): Pair<Boolean, Int> {
        return try {
            val cm = getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
            val connected = cm?.activeNetwork
                ?.let { cm.getNetworkCapabilities(it) }
                ?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true
            if (!connected) return false to 0

            val wifi = applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
            @Suppress("DEPRECATION")
            val rssi = try { wifi?.connectionInfo?.rssi ?: -127 } catch (t: Throwable) { -127 }
            val level = when {
                wifi == null -> 0
                Build.VERSION.SDK_INT >= Build.VERSION_CODES.R -> {
                    // calculateSignalLevel(rssi) -> 0..getMaxSignalLevel(); rescale to 0..4.
                    val max = wifi.maxSignalLevel
                    val raw = wifi.calculateSignalLevel(rssi)
                    if (max <= 0) raw else (raw * 4) / max
                }
                else -> {
                    @Suppress("DEPRECATION")
                    WifiManager.calculateSignalLevel(rssi, 5)
                }
            }
            true to level.coerceIn(0, 4)
        } catch (t: Throwable) {
            false to 0
        }
    }

    /**
     * Bluetooth adapter enabled? Reading adapter state needs no runtime grant of
     * BLUETOOTH_CONNECT; a missing adapter or SecurityException degrades to false.
     */
    private fun readBluetoothOn(): Boolean {
        return try {
            val bm = getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager
            bm?.adapter?.isEnabled == true
        } catch (t: Throwable) {
            false
        }
    }

    /** @return battery percent 0..100 + charging flag, read from the sticky intent. */
    private fun readBattery(): Pair<Int, Boolean> {
        return try {
            val intent = applicationContext.registerReceiver(
                null, IntentFilter(Intent.ACTION_BATTERY_CHANGED)
            )
            val raw = intent?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
            val scale = intent?.getIntExtra(BatteryManager.EXTRA_SCALE, -1) ?: -1
            val pct = if (raw >= 0 && scale > 0) (raw * 100 / scale) else 0
            val status = intent?.getIntExtra(BatteryManager.EXTRA_STATUS, -1) ?: -1
            val charging = status == BatteryManager.BATTERY_STATUS_CHARGING ||
                status == BatteryManager.BATTERY_STATUS_FULL
            pct.coerceIn(0, 100) to charging
        } catch (t: Throwable) {
            0 to false
        }
    }

    // ---------------------------------------------------------------------
    // Biometric unlock
    // ---------------------------------------------------------------------

    private fun isBiometricAvailable(): Boolean {
        return try {
            val allowed = BiometricManager.Authenticators.BIOMETRIC_STRONG or
                BiometricManager.Authenticators.DEVICE_CREDENTIAL
            BiometricManager.from(this).canAuthenticate(allowed) ==
                BiometricManager.BIOMETRIC_SUCCESS
        } catch (t: Throwable) {
            false
        }
    }

    /** Must be called on the main thread. */
    private fun startBiometricPrompt(title: String) {
        val executor = ContextCompat.getMainExecutor(this)
        val callback = object : BiometricPrompt.AuthenticationCallback() {
            override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                deliverAuthResult(true, "biometric")
            }

            override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
                val reason = when (errorCode) {
                    BiometricPrompt.ERROR_USER_CANCELED,
                    BiometricPrompt.ERROR_NEGATIVE_BUTTON,
                    BiometricPrompt.ERROR_CANCELED -> "cancel"
                    else -> "error"
                }
                deliverAuthResult(false, reason)
            }
            // onAuthenticationFailed: one wrong attempt; the prompt stays open, so
            // we intentionally do NOT deliver a result here.
        }

        val safeTitle = title.ifBlank { getString(R.string.auth_title) }
        // DEVICE_CREDENTIAL may only be combined with BIOMETRIC_STRONG on API 30+.
        val authenticators = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            BiometricManager.Authenticators.BIOMETRIC_STRONG or
                BiometricManager.Authenticators.DEVICE_CREDENTIAL
        } else {
            BiometricManager.Authenticators.BIOMETRIC_STRONG
        }
        val info = BiometricPrompt.PromptInfo.Builder()
            .setTitle(safeTitle)
            .setAllowedAuthenticators(authenticators)
            .apply {
                // A negative button is required (and only allowed) when no device
                // credential fallback is offered.
                if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
                    setNegativeButtonText(getString(R.string.auth_cancel))
                }
            }
            .build()

        BiometricPrompt(this, executor, callback).authenticate(info)
    }

    /** Deliver the auth outcome back into the panel on the UI thread. */
    private fun deliverAuthResult(success: Boolean, reason: String) {
        runOnUiThread {
            webView.evaluateJavascript(
                "window.wavrOnAuthResult && window.wavrOnAuthResult($success, '$reason')",
                null
            )
        }
    }
}
