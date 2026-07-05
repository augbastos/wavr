package dev.wavr.mobile.wavrnet

import android.util.Log
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin
import java.io.IOException
import java.net.URI
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import javax.net.ssl.SSLPeerUnverifiedException
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString

/**
 * WavrNet — the app's single native network choke point (CONTRACT B, FROZEN).
 *
 * Every byte to the paired central flows through here, over [PinnedClient]'s
 * pinned TLS: exactly one SHA-256 certificate fingerprint is trusted, re-verified
 * on every handshake, hard-failing (code "PIN_MISMATCH") on any other cert. The
 * native OkHttp WebSocket is REQUIRED, not a convenience: Android's WebView never
 * fires onReceivedSslError for WebSocket handshakes, so an in-page wss:// to a
 * self-signed cert is unfixable — only this native socket can pin /ws/live.
 *
 * JS registration (the shim):  const WavrNet = Capacitor.registerPlugin("WavrNet");
 * Contract:                    see definitions/wavr-net.d.ts (frozen; iOS must match).
 *
 * LOGGING POLICY (logcat is hostile): no header, body, token, ticket, or full URL
 * is ever logged. Exception messages from the network stack can embed URLs, so
 * rejections/events carry only the exception CLASS NAME; the single log line in
 * this file carries host:port only (no path, no query).
 */
@CapacitorPlugin(name = "WavrNet")
class WavrNetPlugin : Plugin() {

    companion object {
        private const val TAG = "WavrNet"
        private const val PIN_MISMATCH_MSG =
            "server certificate does not match the pinned fingerprint"
        private val REQUIRES_BODY = setOf("POST", "PUT", "PATCH")
    }

    private val executor: ExecutorService = Executors.newCachedThreadPool()
    private val sockets = ConcurrentHashMap<String, WebSocket>()
    private val nextSocketId = AtomicLong(1)

    // ---- probe -------------------------------------------------------------------

    /**
     * {url} -> {fingerprint: "AB:CD:.."} — the SHA-256 of the leaf cert the server
     * presents, uppercase colon-separated hex (same format as the backend's
     * `cert_fingerprint()`), read WITHOUT establishing trust and WITHOUT carrying
     * data (see PinnedClient.probeLeafFingerprint). For out-of-band verification
     * at pairing time and for old-vs-new display after a PIN_MISMATCH.
     */
    @PluginMethod
    fun probe(call: PluginCall) {
        val url = call.getString("url")
        if (url.isNullOrBlank()) {
            call.reject("url is required", "INVALID_ARGS")
            return
        }
        runAsync {
            try {
                val fp = PinnedClient.probeLeafFingerprint(url)
                val ret = JSObject()
                ret.put("fingerprint", fp)
                call.resolve(ret)
            } catch (e: IllegalArgumentException) {
                call.reject("url must be a well-formed https:// or wss:// URL", "INVALID_ARGS")
            } catch (e: Exception) {
                call.reject("probe failed (${e.javaClass.simpleName})", "NETWORK")
            }
        }
    }

    // ---- request -----------------------------------------------------------------

    /**
     * {url, method?, headers?, body?, pinnedFp} -> {status, headers, body}.
     * Pinned by the caller-supplied fingerprint. On pin mismatch: reject with code
     * "PIN_MISMATCH" + data.presentedFp (best-effort re-probe) so the shim can
     * render old-vs-new. Other network errors reject with code "NETWORK".
     */
    @PluginMethod
    fun request(call: PluginCall) {
        val url = call.getString("url")
        val pinnedFp = call.getString("pinnedFp")
        if (url.isNullOrBlank() || pinnedFp.isNullOrBlank()) {
            call.reject("url and pinnedFp are required", "INVALID_ARGS")
            return
        }
        if (!url.startsWith("https://", ignoreCase = true)) {
            call.reject("request requires an https:// URL (cleartext is refused)", "INVALID_ARGS")
            return
        }
        val client = try {
            PinnedClient.clientFor(pinnedFp)
        } catch (e: IllegalArgumentException) {
            call.reject("pinnedFp must be a SHA-256 fingerprint (64 hex chars)", "INVALID_ARGS")
            return
        }
        val method = (call.getString("method") ?: "GET").uppercase()
        val bodyStr = call.getString("body")
        val builder = Request.Builder().url(url)
        var contentType = "application/json"
        call.getObject("headers")?.let { headers ->
            val keys = headers.keys()
            while (keys.hasNext()) {
                val name = keys.next()
                val value = headers.getString(name) ?: continue
                if (name.equals("Content-Type", ignoreCase = true)) contentType = value
                builder.header(name, value)   // includes Authorization — never logged
            }
        }
        val requestBody = when {
            bodyStr != null && method != "GET" && method != "HEAD" ->
                bodyStr.toRequestBody(contentType.toMediaTypeOrNull())
            method in REQUIRES_BODY ->        // OkHttp requires a body for these
                "".toRequestBody(contentType.toMediaTypeOrNull())
            else -> null
        }
        builder.method(method, requestBody)

        client.newCall(builder.build()).enqueue(object : Callback {
            override fun onFailure(c: Call, e: IOException) {
                rejectClassified(call, url, e)
            }

            override fun onResponse(c: Call, response: Response) {
                response.use { resp ->
                    val body = try {
                        resp.body?.string() ?: ""
                    } catch (e: IOException) {
                        call.reject("failed reading response body", "NETWORK")
                        return
                    }
                    val headersOut = JSObject()
                    for ((name, values) in resp.headers.toMultimap()) {
                        headersOut.put(name, values.joinToString(", "))
                    }
                    val ret = JSObject()
                    ret.put("status", resp.code)
                    ret.put("headers", headersOut)
                    ret.put("body", body)
                    call.resolve(ret)
                }
            }
        })
    }

    // ---- websocket ---------------------------------------------------------------

    /**
     * {url, pinnedFp, headers?} -> {socketId} (resolves when the socket is OPEN).
     * Same pinned client as request(). Events:
     *   wavrNetMessage {socketId, data}
     *   wavrNetClose   {socketId, code, reason}
     *   wavrNetError   {socketId, code, message, presentedFp?}   code: "PIN_MISMATCH"|"NETWORK"
     * A pin failure during the WS handshake rejects this call AND emits
     * wavrNetError with code "PIN_MISMATCH".
     */
    @PluginMethod
    fun openSocket(call: PluginCall) {
        val url = call.getString("url")
        val pinnedFp = call.getString("pinnedFp")
        if (url.isNullOrBlank() || pinnedFp.isNullOrBlank()) {
            call.reject("url and pinnedFp are required", "INVALID_ARGS")
            return
        }
        if (!url.startsWith("wss://", true) && !url.startsWith("https://", true)) {
            call.reject("openSocket requires a wss:// URL (cleartext is refused)", "INVALID_ARGS")
            return
        }
        val client = try {
            PinnedClient.clientFor(pinnedFp)
        } catch (e: IllegalArgumentException) {
            call.reject("pinnedFp must be a SHA-256 fingerprint (64 hex chars)", "INVALID_ARGS")
            return
        }
        val builder = Request.Builder().url(url)   // the ticket rides in the query string
        call.getObject("headers")?.let { headers ->
            val keys = headers.keys()
            while (keys.hasNext()) {
                val name = keys.next()
                headers.getString(name)?.let { builder.header(name, it) }
            }
        }
        // Note: /ws/live's LAN-companion path (backend app.py) checks subnet + a
        // single-use ticket and does NOT enforce Origin, so no Origin header is set.
        val socketId = nextSocketId.getAndIncrement().toString()
        val settled = AtomicBoolean(false)   // resolve/reject exactly once
        val opened = AtomicBoolean(false)

        client.newWebSocket(builder.build(), object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                sockets[socketId] = webSocket
                opened.set(true)
                if (settled.compareAndSet(false, true)) {
                    val ret = JSObject()
                    ret.put("socketId", socketId)
                    call.resolve(ret)
                }
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val evt = JSObject()
                evt.put("socketId", socketId)
                evt.put("data", text)
                notifyListeners("wavrNetMessage", evt)
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                // The central sends JSON text frames (/ws/live send_json); decode any
                // binary frame as UTF-8 best-effort rather than silently dropping it.
                val evt = JSObject()
                evt.put("socketId", socketId)
                evt.put("data", bytes.utf8())
                notifyListeners("wavrNetMessage", evt)
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                try {
                    webSocket.close(code, reason.take(120))   // acknowledge remote close
                } catch (_: Exception) {
                }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                sockets.remove(socketId)
                val evt = JSObject()
                evt.put("socketId", socketId)
                evt.put("code", code)
                evt.put("reason", reason)
                notifyListeners("wavrNetClose", evt)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                sockets.remove(socketId)
                runAsync {   // off OkHttp's reader thread; the re-probe below blocks
                    val pin = isPinFailure(t)
                    if (pin) Log.w(TAG, "TLS pin mismatch for ${redactedHost(url)}")
                    else Log.w(TAG, "non-pin network failure for ${redactedHost(url)} (${t.javaClass.simpleName})")
                    val presentedFp = if (pin) safeProbeFp(url) else null
                    val codeStr = if (pin) "PIN_MISMATCH" else "NETWORK"

                    val evt = JSObject()
                    evt.put("socketId", socketId)
                    evt.put("code", codeStr)
                    // class name only — raw exception messages can embed URLs/query
                    evt.put("message", t.javaClass.simpleName)
                    presentedFp?.let { evt.put("presentedFp", it) }
                    notifyListeners("wavrNetError", evt)

                    if (settled.compareAndSet(false, true)) {
                        // handshake never completed -> the openSocket() promise fails too
                        val data = JSObject()
                        presentedFp?.let { data.put("presentedFp", it) }
                        val msg = if (pin) PIN_MISMATCH_MSG
                                  else "socket failed (${t.javaClass.simpleName})"
                        call.reject(msg, codeStr, data)
                    } else if (opened.get()) {
                        // browser-like: an errored open socket also reports a close
                        val closeEvt = JSObject()
                        closeEvt.put("socketId", socketId)
                        closeEvt.put("code", 1006)
                        closeEvt.put("reason", "")
                        notifyListeners("wavrNetClose", closeEvt)
                    }
                }
            }
        })
    }

    /** {socketId, data} — send a text frame on an open socket. */
    @PluginMethod
    fun sendSocket(call: PluginCall) {
        val socketId = call.getString("socketId")
        val data = call.getString("data")
        if (socketId.isNullOrBlank() || data == null) {
            call.reject("socketId and data are required", "INVALID_ARGS")
            return
        }
        val ws = sockets[socketId]
        if (ws == null) {
            call.reject("unknown socketId", "UNKNOWN_SOCKET")
            return
        }
        if (ws.send(data)) call.resolve()
        else call.reject("socket is closed or its outgoing buffer is full", "SEND_FAILED")
    }

    /** {socketId, code?, reason?} — close; idempotent (unknown id resolves quietly). */
    @PluginMethod
    fun closeSocket(call: PluginCall) {
        val socketId = call.getString("socketId")
        if (socketId.isNullOrBlank()) {
            call.reject("socketId is required", "INVALID_ARGS")
            return
        }
        val code = call.getInt("code") ?: 1000
        val reason = call.getString("reason")
        sockets[socketId]?.let { ws ->
            try {
                ws.close(code, reason)          // onClosed removes it from the map
            } catch (_: IllegalArgumentException) {
                ws.close(1000, null)            // invalid code/reason from JS -> normal close
            }
        }
        call.resolve()
    }

    override fun handleOnDestroy() {
        for ((_, ws) in sockets) {
            try {
                ws.close(1001, "going away")
            } catch (_: Exception) {
            }
        }
        sockets.clear()
        executor.shutdown()
        super.handleOnDestroy()
    }

    // ---- error classification ------------------------------------------------------

    /**
     * Pin failure iff the cause chain contains [PinMismatchException] (thrown by
     * the pinned TrustManager, wrapped by OkHttp in SSLHandshakeException) or
     * SSLPeerUnverifiedException (the pinned hostname verifier saying no). A
     * generic SSLHandshakeException WITHOUT those is a protocol failure, not a
     * pin decision, and is reported as NETWORK.
     */
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

    private fun rejectClassified(call: PluginCall, url: String, t: Throwable) {
        if (isPinFailure(t)) {
            Log.w(TAG, "TLS pin mismatch for ${redactedHost(url)}")
            runAsync {
                val data = JSObject()
                safeProbeFp(url)?.let { data.put("presentedFp", it) }
                call.reject(PIN_MISMATCH_MSG, "PIN_MISMATCH", data)
            }
        } else {
            Log.w(TAG, "non-pin network failure for ${redactedHost(url)} (${t.javaClass.simpleName})")
            // class name only — IOException messages can embed the URL (query incl. ticket)
            call.reject("network error (${t.javaClass.simpleName})", "NETWORK")
        }
    }

    /** Best-effort re-probe of the presented cert for old-vs-new display; never throws. */
    private fun safeProbeFp(url: String): String? = try {
        PinnedClient.probeLeafFingerprint(url)
    } catch (_: Exception) {
        null
    }

    /** host:port only — safe to log (no path, no query, no credentials). */
    private fun redactedHost(url: String): String = try {
        val u = URI(url)
        "${u.host}:${if (u.port == -1) 443 else u.port}"
    } catch (_: Exception) {
        "<unparseable-url>"
    }

    private fun runAsync(block: () -> Unit) {
        try {
            executor.execute(block)
        } catch (_: RejectedExecutionException) {
            // plugin is being destroyed; drop silently
        }
    }
}
