package dev.wavr.core

import android.annotation.SuppressLint
import android.app.Activity
import android.graphics.Color
import android.net.Uri
import android.net.http.SslError
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
 */
class MainActivity : Activity() {

    private companion object {
        const val CORE_URL = "https://localhost:8000/?core"
        const val RETRY_DELAY_MS = 3000L
        val BG_COLOR = Color.parseColor("#0B0F14")
        val MUTED_COLOR = Color.parseColor("#8A97A5")
    }

    private lateinit var webView: WebView
    private lateinit var placeholder: View

    private val handler = Handler(Looper.getMainLooper())
    private val retryRunnable = Runnable { loadCore() }

    /** True when the current main-frame navigation failed (so we keep retrying). */
    private var pageHadError = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Keep the panel awake indefinitely — it is a wall/stand display.
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

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

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWebView(view: WebView) {
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

    /** As a HOME launcher, Back must not escape the kiosk. */
    @Suppress("DEPRECATION")
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
        webView.destroy()
        super.onDestroy()
    }
}
