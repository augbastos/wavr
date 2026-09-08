package dev.wavr.mobile.wavrnet

import android.content.Context
import android.net.NetworkCapabilities
import android.provider.Settings
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
// Fixed, app-specific salt: the hub stores a digest, and the same raw id under
// a different app would hash to something else. Not a secret -- it is in the
// APK -- it just stops the value from being a bare, portable ANDROID_ID.
private const val CHAVE_SAL = "wavr-device-key-v1:"
// The constant a well-known buggy manufacturer's firmware returned for every
// device. Treating it as an id would make unrelated phones look like the same
// one, and one of them would revoke the other's credential.
private const val ANDROID_ID_QUEBRADO = "9774d56d682e549c"
private const val PREFS_CHAVE = "wavr_net"
private const val PREF_CHAVE_INSTALACAO = "install_key"

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
        // A browse outliving the screen keeps the resolver querying and keeps
        // system-side request slots held. The deadline would end it eventually;
        // "eventually" is not a teardown.
        buscaViva.getAndSet(null)?.let { fim -> try { fim() } catch (_: Throwable) { } }
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

    /**
     * `{services: [{name, host, port, txt}], via, note}` — find Cores THROUGH
     * THE SYSTEM, on the Wi-Fi, with a VPN running.
     *
     * ## Why the app cannot do this itself
     *
     * `capacitor-zeroconf` runs JmDNS inside this process: it opens its own
     * multicast socket and sends the query to 224.0.0.251. On a phone with a
     * VPN that is the end of it. Measured on a handset with a commercial VPN: the tunnel
     * carries `224.0.0.0/3` — every multicast address — and Android binds the
     * app to that VPN by UID, with `bypassable=false`, so binding the socket to
     * the Wi-Fi address does not help either. The query leaves through the
     * tunnel and never reaches the living room.
     *
     * Confirmed rather than reasoned: a `_wavrtest._tcp` service was published
     * from the laptop and this phone, asked through its own ZeroConf plugin,
     * saw neither it nor the Core, while the laptop saw both instantly.
     *
     * ## Why the system can
     *
     * `NsdManager` does not do the querying here. It asks the platform's own
     * mDNS resolver, which lives outside this app's network scope — the same
     * resolver that keeps advertising this phone's `_adb-tls-connect._tcp` on
     * the Wi-Fi while the app can see nothing at all. Since API 33 the query
     * can also name the `Network` to run on, so the Wi-Fi is asked explicitly
     * instead of whatever the app's default route happens to be.
     *
     * A VPN is a reasonable thing to leave on at home, and "turn it off" is not
     * an answer to "find the computer in the next room".
     */
    /**
     * How to end the browse that is currently running, if one is.
     *
     * Two things need this. The plugin being destroyed must not leave a browse
     * running against a screen that is gone; and starting a browse must end the
     * previous one, because the discovery screen can be entered and left
     * repeatedly and each entry used to stack another concurrent browse with
     * its own deadline and its own registered callbacks.
     */
    private val buscaViva = java.util.concurrent.atomic.AtomicReference<(() -> Unit)?>(null)

    @PluginMethod
    fun discoverCores(call: PluginCall) {
        // One browse at a time. Ending the previous one is not a courtesy:
        // each browse holds system-side request slots (see `registrados`).
        buscaViva.getAndSet(null)?.let { fim -> try { fim() } catch (_: Throwable) { } }
        val tipo = call.getString("type") ?: "_wavr._tcp."
        val espera = (call.getInt("timeoutMs") ?: 8000).coerceIn(1000, 30000)
        val nsd = context.getSystemService(Context.NSD_SERVICE) as? android.net.nsd.NsdManager
        if (nsd == null) {
            call.reject("NsdManager unavailable", "NO_NSD")
            return
        }

        // The Wi-Fi network, explicitly — NOT the app's default, which is the
        // VPN. Null is fine: the platform then uses its own default, which is
        // still outside this app's VPN scope.
        var wifi: android.net.Network? = null
        try {
            val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE)
                as android.net.ConnectivityManager
            for (n in cm.allNetworks) {
                val caps = cm.getNetworkCapabilities(n) ?: continue
                if (caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) &&
                    !caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)
                ) { wifi = n; break }
            }
        } catch (e: Exception) {
            Log.w(TAG, "could not pick the Wi-Fi network: ${e.message}")
        }

        val achados = java.util.concurrent.ConcurrentHashMap<String, JSObject>()
        val pendentes = java.util.concurrent.atomic.AtomicInteger(0)
        val respondido = AtomicBoolean(false)
        // Run the platform's callbacks inline instead of on an executor we own.
        //
        // An executor handed to NsdManager belongs to NsdManager until IT says
        // it is finished, and it says so by using that executor -- so shutting
        // it down when we stop the browse is a race we lose: the reply lands on
        // a terminated pool, AbortPolicy throws inside the system's own Handler
        // with nothing catching it, and the process dies. That is not a theory;
        // it killed the app on the first user's phone at 21:49 on 2026-09-07.
        //
        // Inline is what the platform does for the older overload anyway
        // (`discoverServices(type, protocol, listener)` is implemented as
        // `Runnable::run`), and everything we do in a callback is a map write
        // or a resolve request, so there is nothing worth a thread here.
        val direto = java.util.concurrent.Executor { it.run() }
        // Every service-info callback we register, so the round can hand them
        // all back. One that never produces an update would otherwise stay
        // registered until the process dies, and a client is capped at 200
        // outstanding requests -- past that, every browse fails to start.
        val registrados = java.util.Collections.synchronizedList(
            mutableListOf<android.net.nsd.NsdManager.ServiceInfoCallback>())

        fun guardar(info: android.net.nsd.NsdServiceInfo) {
            val porta = info.port
            if (porta <= 0) return
            val enderecos = try {
                if (android.os.Build.VERSION.SDK_INT >= 34) info.hostAddresses
                else listOfNotNull(@Suppress("DEPRECATION") info.host)
            } catch (_: Throwable) { emptyList() }
            val ipv4 = enderecos.firstOrNull { it is java.net.Inet4Address }?.hostAddress
                ?: enderecos.firstOrNull()?.hostAddress ?: return
            val txt = JSObject()
            try {
                for ((k, v) in info.attributes) {
                    txt.put(k, if (v == null) "" else String(v, Charsets.UTF_8))
                }
            } catch (_: Throwable) { }
            val o = JSObject()
            o.put("name", info.serviceName ?: "")
            o.put("host", ipv4)
            o.put("port", porta)
            o.put("txt", txt)
            achados[(info.serviceName ?: "") + "@" + ipv4 + ":" + porta] = o
        }

        // Set once the listener exists; `acabou()` needs it to end the round.
        val fechar = java.util.concurrent.atomic.AtomicReference<(String?) -> Unit>({ })

        /**
         * One resolution finished, for any reason. When nothing is still being
         * resolved and we have something to report, answer now.
         *
         * A browse has no natural end -- there is no moment when everything
         * that exists has replied -- so the deadline stays as the backstop. But
         * waiting the full deadline when the answer is already in hand is what
         * put the result 6.0s into a 7s window on the screen above, where a
         * hub that HAD been found got thrown away as "nothing answered".
         */
        fun acabou() {
            if (pendentes.decrementAndGet() <= 0 && achados.isNotEmpty()) {
                fechar.get().invoke(null)
            }
        }

        val ouvinte = object : android.net.nsd.NsdManager.DiscoveryListener {
            override fun onStartDiscoveryFailed(t: String?, code: Int) {
                terminar(call, achados, "nsd", "discovery could not start (code $code)",
                         respondido, nsd, this, registrados)
            }
            override fun onStopDiscoveryFailed(t: String?, code: Int) { }
            override fun onDiscoveryStarted(t: String?) { }
            override fun onDiscoveryStopped(t: String?) { }
            override fun onServiceLost(info: android.net.nsd.NsdServiceInfo?) { }

            override fun onServiceFound(info: android.net.nsd.NsdServiceInfo?) {
                if (info == null) return
                pendentes.incrementAndGet()
                // Resolution is what turns a name into an address and a port.
                // `registerServiceInfoCallback` is the current API;
                // `resolveService` is deprecated from 34 and refuses to run
                // concurrently, which is exactly what a browse produces.
                try {
                    if (android.os.Build.VERSION.SDK_INT >= 34) {
                        val cb = object : android.net.nsd.NsdManager.ServiceInfoCallback {
                            override fun onServiceInfoCallbackRegistrationFailed(e: Int) {
                                registrados.remove(this)
                                acabou()
                            }
                            override fun onServiceUpdated(u: android.net.nsd.NsdServiceInfo) {
                                guardar(u)
                                registrados.remove(this)
                                try { nsd.unregisterServiceInfoCallback(this) } catch (_: Throwable) { }
                                acabou()
                            }
                            override fun onServiceLost() { }
                            override fun onServiceInfoCallbackUnregistered() { }
                        }
                        registrados.add(cb)
                        nsd.registerServiceInfoCallback(info, direto, cb)
                    } else {
                        @Suppress("DEPRECATION")
                        nsd.resolveService(info, object : android.net.nsd.NsdManager.ResolveListener {
                            override fun onResolveFailed(i: android.net.nsd.NsdServiceInfo?, c: Int) {
                                acabou()
                            }
                            override fun onServiceResolved(r: android.net.nsd.NsdServiceInfo?) {
                                if (r != null) guardar(r)
                                acabou()
                            }
                        })
                    }
                } catch (e: Exception) {
                    acabou()
                    Log.w(TAG, "resolve failed: ${e.message}")
                }
            }
        }

        val comoChegou = if (android.os.Build.VERSION.SDK_INT >= 33 && wifi != null)
            "nsd/wifi" else "nsd/default"
        fechar.set({ nota: String? ->
            terminar(call, achados, comoChegou, nota, respondido, nsd, ouvinte, registrados)
        })

        try {
            if (android.os.Build.VERSION.SDK_INT >= 33 && wifi != null) {
                nsd.discoverServices(tipo, android.net.nsd.NsdManager.PROTOCOL_DNS_SD,
                                     wifi, direto, ouvinte)
            } else {
                @Suppress("DEPRECATION")
                nsd.discoverServices(tipo, android.net.nsd.NsdManager.PROTOCOL_DNS_SD, ouvinte)
            }
        } catch (e: Exception) {
            terminar(call, achados, "nsd", "discovery threw: ${e.message}",
                     respondido, nsd, ouvinte, registrados)
            return
        }

        // Ending this browse, from anywhere: the deadline below, the plugin
        // being destroyed, or the next browse starting.
        buscaViva.set({ fechar.get().invoke("stopped") })

        // The deadline is the backstop, not the plan: `acabou()` answers as
        // soon as everything found has resolved.
        android.os.Handler(android.os.Looper.getMainLooper()).postDelayed({
            fechar.get().invoke(null)
        }, espera.toLong())
    }

    private fun terminar(
        call: PluginCall,
        achados: Map<String, JSObject>,
        via: String,
        nota: String?,
        respondido: AtomicBoolean,
        nsd: android.net.nsd.NsdManager,
        ouvinte: android.net.nsd.NsdManager.DiscoveryListener,
        registrados: MutableList<android.net.nsd.NsdManager.ServiceInfoCallback>,
    ) {
        if (!respondido.compareAndSet(false, true)) return
        buscaViva.set(null)
        try { nsd.stopServiceDiscovery(ouvinte) } catch (_: Throwable) { }
        // Hand back every outstanding service-info request. Nothing shuts an
        // executor down here: the one the platform was given runs inline and
        // has no lifecycle, which is the whole point (see `direto`).
        val pendura = synchronized(registrados) { registrados.toList() }
        registrados.clear()
        for (cb in pendura) {
            try { nsd.unregisterServiceInfoCallback(cb) } catch (_: Throwable) { }
        }
        val res = JSObject()
        val arr = com.getcapacitor.JSArray()
        for (v in achados.values) arr.put(v)
        res.put("services", arr)
        res.put("via", via)
        if (nota != null) res.put("note", nota)
        call.resolve(res)
    }

    /**
     * `{known, vpnActive, wifiActive}` — what the discovery screen needs to
     * explain itself.
     *
     * Wavr finds a Core by browsing mDNS, which is a multicast query to
     * 224.0.0.251. A VPN on this phone routes that into the tunnel and it never
     * reaches the Wi-Fi. Measured on a handset running a commercial VPN, which installs
     * `224.0.0.0/3` on `tun0` — every multicast address there is — while
     * deliberately leaving `192.168.0.0/16` outside the tunnel.
     *
     * That combination is the hardest possible one for a person to diagnose,
     * because the Core IS reachable: typing its address works, pairing works,
     * the dashboard works. Only the FINDING is broken. The screen meanwhile
     * said "make sure your hub is on and this phone is on the same network" —
     * two things that were already true — and he spent an evening on it.
     *
     * This does not claim the VPN is the cause: an app cannot read the VPN's
     * routing table. It reports that one is up, which is enough for the screen
     * to name it as the likeliest reason and offer the two ways through,
     * instead of sending somebody to re-check what is already correct.
     */
    @PluginMethod
    fun networkFacts(call: PluginCall) {
        val res = JSObject()
        var vpn = false
        var wifi = false
        try {
            val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE)
                as android.net.ConnectivityManager
            // Every network, not only the active one: a VPN and the Wi-Fi it
            // rides on are two entries, and the active one is the VPN.
            for (n in cm.allNetworks) {
                val caps = cm.getNetworkCapabilities(n) ?: continue
                if (caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) vpn = true
                if (caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) &&
                    !caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)
                ) wifi = true
            }
            res.put("known", true)
        } catch (e: Exception) {
            // Never a reason to fail the screen that asked. "Unknown" is a real
            // answer, and the caller stays quiet rather than guessing.
            Log.w("WavrNet", "networkFacts unavailable: ${e.message}")
            res.put("known", false)
        }
        res.put("vpnActive", vpn)
        res.put("wifiActive", wifi)
        call.resolve(res)
    }

    /**
     * A stable, opaque id for THIS phone, so the hub can recognise it instead
     * of collecting duplicates.
     *
     * The first user paired his phone four times over one afternoon and his hub
     * finished the day holding four live credentials to his home, three of them
     * forgotten. Every one of them still worked. The hub had no way to tell
     * that the fourth pairing was the same phone as the first, because nothing
     * in a WebView survives a reinstall.
     *
     * `ANDROID_ID` does, and at the right grain: it is scoped to this app's
     * signing key on this device for this user, it survives reinstalling the
     * app, and a factory reset clears it. It is never sent raw -- it is hashed
     * with a fixed app string first, so what reaches the hub is a digest it
     * cannot reverse and can only compare to another digest. It never leaves
     * the local network either way.
     *
     * The fallback matters: a handful of devices report nothing, and one buggy
     * family famously reports the same constant for everyone. Rather than send
     * a value that would make two different phones look like one -- which would
     * revoke a stranger's credential -- we fall back to a random id generated
     * once and kept for this install. That still de-duplicates every re-pair
     * that does not involve a reinstall, and de-duplicates nothing it is not
     * sure about.
     */
    @PluginMethod
    fun deviceKey(call: PluginCall) {
        val res = JSObject()
        try {
            res.put("key", chaveEstavel())
            res.put("known", true)
        } catch (e: Exception) {
            // No key is a fine answer: pairing works exactly as it did before,
            // it just cannot retire the older credentials. Failing the call
            // would break pairing outright, which is far worse than a duplicate.
            Log.w("WavrNet", "deviceKey unavailable: ${e.message}")
            res.put("known", false)
        }
        call.resolve(res)
    }

    /** SHA-256(salt + raw) as lowercase hex. */
    private fun digerir(cru: String): String {
        val md = java.security.MessageDigest.getInstance("SHA-256")
        val bytes = md.digest((CHAVE_SAL + cru).toByteArray(Charsets.UTF_8))
        val sb = StringBuilder(bytes.size * 2)
        for (b in bytes) sb.append(String.format("%02x", b))
        return sb.toString()
    }

    private fun chaveEstavel(): String {
        val bruto = try {
            Settings.Secure.getString(context.contentResolver, Settings.Secure.ANDROID_ID)
        } catch (e: Exception) {
            null
        }
        if (!bruto.isNullOrBlank() && bruto != ANDROID_ID_QUEBRADO) return digerir(bruto)
        // Per-install fallback, generated once and kept.
        val prefs = context.getSharedPreferences(PREFS_CHAVE, Context.MODE_PRIVATE)
        var guardado = prefs.getString(PREF_CHAVE_INSTALACAO, null)
        if (guardado.isNullOrBlank()) {
            guardado = java.util.UUID.randomUUID().toString()
            prefs.edit().putString(PREF_CHAVE_INSTALACAO, guardado).apply()
        }
        return digerir(guardado)
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
