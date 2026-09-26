package dev.wavr.core.nativeui

import android.app.Application
import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Build
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** Joining a Space from this device: probe -> ask -> the person at the Core approves. */
data class JoinState(
    val url: String,
    val fingerprint: String? = null,     // what the Core presented, unverified until compared
    val compareCode: String? = null,     // shown here; typed on the Core by a person
    val requestId: String? = null,
    val waiting: Boolean = false
)

data class ManageState(
    val connection: Connection = Connection.LOOPBACK,
    val discovered: List<String> = emptyList(),
    val join: JoinState? = null,
    val watch: Boolean? = null,
    val sensing: SensingState? = null,
    val pairings: List<PendingPairing>? = null,
    val nodes: List<PendingNode>? = null,
    val doctor: List<DoctorCheck>? = null,
    val message: String? = null,
    val busy: Boolean = false
)

/**
 * The write side of the native app. Every action is one command of the native
 * command contract, run by the native runtime; this class holds UI state and
 * never decides whether an action is allowed -- the Core does, and its refusal
 * is shown as the Core phrased it.
 */
class ManageViewModel(app: Application) : AndroidViewModel(app) {
    private val store = ConnectionStore(app)
    private val mutable = MutableStateFlow(ManageState(connection = store.joined() ?: Connection.LOOPBACK))
    val state = mutable.asStateFlow()

    init {
        // Which loopback scheme answers is a network question: settle it off the main thread.
        if (store.joined() == null) viewModelScope.launch {
            val c = withContext(Dispatchers.IO) { if (WavrNative.available) store.load() else Connection.LOOPBACK }
            mutable.update { it.copy(connection = c) }
        }
    }
    private var joinJob: Job? = null
    private var discovery: NsdManager.DiscoveryListener? = null


    private suspend fun run(name: String, argsJson: String? = null, c: Connection = mutable.value.connection): Reply =
        withContext(Dispatchers.IO) {
            if (!WavrNative.available) Reply(false, null, "unreachable", "native runtime is not bundled", null)
            else Reply.parse(WavrNative.commandRun(c.url, c.token, c.pin, name, argsJson, 8000))
        }

    private fun say(reply: Reply, done: String? = null) {
        mutable.update { it.copy(message = if (reply.ok) done else reply.explain(), busy = false) }
    }

    fun refresh() = viewModelScope.launch {
        mutable.update { it.copy(busy = true, message = null) }
        val watch = run("watch.get")
        val sensing = run("sources.list")
        val onCore = mutable.value.connection.onThisDevice
        val pairings = if (onCore) run("pairings.list") else null
        val nodes = if (onCore) run("nodes.pending") else null
        mutable.update {
            it.copy(
                watch = if (watch.ok) watch.obj?.bool("on") else null,
                sensing = if (sensing.ok) parseSensing(sensing.obj) else null,
                pairings = pairings?.takeIf { r -> r.ok }?.let { r -> parsePendingPairings(r.obj) },
                nodes = nodes?.takeIf { r -> r.ok }?.let { r -> parsePendingNodes(r.obj) },
                // The first refusal says why the panel is empty (e.g. a 'user' device).
                message = listOfNotNull(watch, sensing, pairings, nodes).firstOrNull { r -> !r.ok }?.explain(),
                busy = false
            )
        }
    }

    fun setWatch(on: Boolean) = act("watch.set", args("on" to on), if (on) "Watch is on." else "Watch is off.")
    fun setSensing(on: Boolean) = act("sensing.set", args("on" to on), if (on) "Sensing is on." else "Sensing is off.")
    fun setSource(name: String, on: Boolean) = act("source.set", args("name" to name, "enabled" to on), "$name ${if (on) "on" else "off"}.")
    fun approvePairing(id: String, code: String, role: String) =
        act("pairings.approve", args("id" to id, "confirm_code" to code.filter(Char::isDigit), "role" to role), "Device let in as $role.")
    fun denyPairing(id: String) = act("pairings.deny", args("id" to id), "Request denied.")
    fun approveNode(id: String, name: String, sensorType: String, room: String) =
        act("node.approve", args("id" to id, "name" to name, "sensor_type" to sensorType, "room" to room), "Sensor let in.")
    fun denyNode(id: String) = act("node.deny", args("id" to id), "Sensor denied.")

    fun runDoctor() = viewModelScope.launch {
        mutable.update { it.copy(busy = true, message = "Checking...") }
        val r = run("doctor.run")
        mutable.update { it.copy(doctor = if (r.ok) parseDoctor(r.obj) else null,
            message = if (r.ok) null else r.explain(), busy = false) }
    }

    private fun act(name: String, argsJson: String, done: String) = viewModelScope.launch {
        mutable.update { it.copy(busy = true, message = null) }
        val r = run(name, argsJson)
        say(r, done)
        if (r.ok) refresh()
    }

    // -- joining a Space ------------------------------------------------------------

    fun check(url: String) = viewModelScope.launch {
        val clean = url.trim().trimEnd('/')
        mutable.update { it.copy(join = JoinState(clean), busy = true, message = null) }
        val p = withContext(Dispatchers.IO) { Probe.parse(WavrNative.probe(clean, 6000)) }
        mutable.update {
            it.copy(join = JoinState(clean, fingerprint = p.fingerprint), busy = false,
                message = if (p.ok) null else "No certificate from $clean: ${p.error ?: "unknown"}")
        }
    }

    fun askToJoin() {
        val j = mutable.value.join ?: return
        val fp = j.fingerprint ?: return
        joinJob?.cancel()
        joinJob = viewModelScope.launch {
            // Every request from here on is pinned to the certificate the person compared.
            val pinned = Connection(j.url, fp, null)
            val r = run("pair.request", args("requester_name" to Build.MODEL, "platform" to "android",
                "reported_fp" to fp), pinned)
            val data = r.obj
            if (!r.ok || data == null) return@launch say(r)
            if (data.string("cert_fingerprint") != fp) {
                mutable.update { it.copy(join = null, message = "The Core's certificate changed during joining. Stopped.") }
                return@launch
            }
            val rid = data.string("request_id") ?: return@launch say(r)
            mutable.update { it.copy(join = j.copy(compareCode = data.string("compare_code"),
                requestId = rid, waiting = true)) }
            repeat(90) {
                delay(2_000)
                val s = run("pair.status", args("request_id" to rid), pinned)
                val st = s.obj
                when (st?.string("status")) {
                    "approved" -> {
                        val token = st.string("token")
                        if (token.isNullOrEmpty()) return@launch say(s)
                        // A genuine Core issues URL-safe tokens. Anything with a control
                        // character would be written into every future request header:
                        // never store it (the native runtime refuses it too).
                        if (token.any { it < ' ' || it == '\u007f' }) {
                            mutable.update { it.copy(join = null, message = "The Core returned a malformed credential. Not saved.") }
                            return@launch
                        }
                        val c = Connection(j.url, fp, token)
                        store.save(c)
                        mutable.update { it.copy(connection = c, join = null, message = "Joined.") }
                        refresh()
                        return@launch
                    }
                    "denied" -> { mutable.update { it.copy(join = null, message = "The Core denied this device.") }; return@launch }
                    "expired" -> { mutable.update { it.copy(join = null, message = "The request expired. Ask again.") }; return@launch }
                    "pending" -> Unit
                    else -> if (!s.ok) { say(s); mutable.update { it.copy(join = null) }; return@launch }
                }
            }
            mutable.update { it.copy(join = null, message = "Nobody approved it in time. Ask again.") }
        }
    }

    fun cancelJoin() {
        joinJob?.cancel()
        mutable.update { it.copy(join = null) }
    }

    fun forget() {
        store.forget()
        mutable.update { ManageState(connection = Connection.LOOPBACK, message = "This device forgot the Core.") }
        viewModelScope.launch {
            val c = withContext(Dispatchers.IO) { store.load() }
            mutable.update { it.copy(connection = c) }
        }
    }

    /** Cores advertising `_wavr._tcp` on this network (the Core launcher does). */
    fun discover(context: Context) {
        if (discovery != null) return
        val nsd = context.getSystemService(Context.NSD_SERVICE) as? NsdManager ?: return
        val listener = object : NsdManager.DiscoveryListener {
            override fun onServiceFound(info: NsdServiceInfo) {
                @Suppress("DEPRECATION")
                nsd.resolveService(info, object : NsdManager.ResolveListener {
                    override fun onResolveFailed(i: NsdServiceInfo, code: Int) = Unit
                    override fun onServiceResolved(i: NsdServiceInfo) {
                        @Suppress("DEPRECATION")
                        val host = i.host?.hostAddress ?: return
                        if (host.contains(':')) return   // the Core admits IPv4 peers only
                        val url = "https://$host:${i.port}"
                        mutable.update { s -> if (url in s.discovered) s else s.copy(discovered = s.discovered + url) }
                    }
                })
            }
            override fun onServiceLost(info: NsdServiceInfo) = Unit
            override fun onDiscoveryStarted(type: String) = Unit
            override fun onDiscoveryStopped(type: String) = Unit
            override fun onStartDiscoveryFailed(type: String, code: Int) = Unit
            override fun onStopDiscoveryFailed(type: String, code: Int) = Unit
        }
        discovery = listener
        try {
            nsd.discoverServices("_wavr._tcp", NsdManager.PROTOCOL_DNS_SD, listener)
        } catch (_: Exception) {
            discovery = null
        }
    }

    fun stopDiscovery(context: Context) {
        val listener = discovery ?: return
        discovery = null
        try {
            (context.getSystemService(Context.NSD_SERVICE) as? NsdManager)?.stopServiceDiscovery(listener)
        } catch (_: Exception) {
        }
    }
}
