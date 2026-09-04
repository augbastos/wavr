package dev.wavr.sdk

import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/**
 * Wavr Spatial SDK — Kotlin.
 *
 * What an Android application needs from Wavr, without needing to know what a
 * modality is or which sensor answered.
 *
 * ```kotlin
 * val wavr = WavrClient("https://192.168.1.10:8443", token = "...")
 * wavr.connect()
 *
 * val kitchen = wavr.context("kitchen")
 * if (kitchen.can(Capability.COUNT)) render(kitchen.occupancy)
 * kitchen.limitations.forEach { Log.i("wavr", it) }
 * ```
 *
 * ## Why this exists next to the Core rather than as a separate library
 *
 * It lives in the app that hosts the Core, and the app uses it — see
 * `CoreService`'s notification. An SDK nothing in the repository calls is a
 * proposal, not a component, and it rots between the two releases where nobody
 * notices its API drifting away from the Core's.
 *
 * ## The three properties that make it more than a URL builder
 *
 * **`occupancy` is nullable and stays that way.** `null` means nothing in the
 * room can count. Kotlin's type system carries that all the way to the call
 * site, so an application has to acknowledge the case rather than let a `0`
 * appear. This is the single most valuable thing a typed SDK adds here.
 *
 * **Failures are a sealed hierarchy.** `Auth` and `Unreachable` need different
 * responses — one is a settings screen, the other is a retry — and code that
 * catches `Exception` treats them the same.
 *
 * **A newer Core is reported, not fatal.** [protocolAhead] is surfaced so an app
 * can decide. Throwing would break every installed app the day somebody updates
 * their Core.
 *
 * No dependencies beyond `org.json` and `HttpURLConnection`, both of which
 * Android ships. Blocking by design: callers are expected to be on a background
 * thread or inside a coroutine's IO dispatcher, and hiding that behind a
 * callback would just move the same requirement somewhere less visible.
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
class WavrClient(
    baseUrl: String,
    private val token: String = "",
    private val timeoutMs: Int = 10_000,
    /** Injectable transport, so the tests need neither a network nor a device. */
    private val transport: (Request) -> String = ::defaultTransport,
) {
    val baseUrl: String = baseUrl.trimEnd('/')

    var space: Space? = null
        private set

    var protocolVersion: Int? = null
        private set

    /**
     * True when the Core speaks a newer contract than this SDK.
     *
     * Surfaced rather than thrown on: refusing outright would break every
     * installed application the day a Core updates, and ignoring it would let a
     * field whose meaning changed pass straight through.
     */
    var protocolAhead: Boolean = false
        private set

    /** Reach the Core, learn the Space, and check the contract version. */
    fun connect(): WavrClient {
        val body = getJson("/api/experience/context")
        space = body.optJSONObject("space")?.let {
            Space(it.optString("space_id"), it.optString("name"))
        }
        protocolVersion = if (body.has("protocol_version")) {
            body.optInt("protocol_version")
        } else {
            null
        }
        protocolAhead = (protocolVersion ?: 0) > PROTOCOL_VERSION
        return this
    }

    /** Every room's context. */
    fun contexts(): List<RoomContext> {
        val rooms = getJson("/api/experience/context").optJSONArray("rooms")
            ?: JSONArray()
        return (0 until rooms.length()).map { RoomContext.from(rooms.getJSONObject(it)) }
    }

    /** Room names in this Space. */
    fun rooms(): List<String> = contexts().map { it.room }

    /** One room's context. */
    fun context(room: String): RoomContext =
        RoomContext.from(getJson("/api/experience/context/${enc(room)}"))

    /** Named places, optionally in one room. */
    fun anchors(room: String = ""): List<Anchor> {
        val path = if (room.isEmpty()) "/api/anchors" else "/api/anchors?room=${enc(room)}"
        val arr = getJson(path).optJSONArray("anchors") ?: JSONArray()
        return (0 until arr.length()).map { Anchor.from(arr.getJSONObject(it)) }
    }

    /**
     * Which Wavr anchors an external system's id refers to.
     *
     * A list. Two runtimes can bind their own id to the same place, and
     * returning the first would hide the collision behind a plausible answer.
     */
    fun resolveAnchor(providerId: String, externalId: String): List<Anchor> {
        val arr = getJson("/api/anchors/resolve/${enc(providerId)}/${enc(externalId)}")
            .optJSONArray("anchors") ?: JSONArray()
        return (0 until arr.length()).map { Anchor.from(arr.getJSONObject(it)) }
    }

    /** What each sensor can honestly observe. */
    fun coverage(): JSONObject = getJson("/api/coverage")

    /**
     * Whether the Core is working, in its own words.
     *
     * The one place any surface should ask. A client that computes health of its
     * own eventually disagrees with the tray and the dashboard, in front of
     * somebody who has no way to tell which is right.
     *
     * Throws when the Core cannot be reached — and the caller must render THAT
     * rather than the last answer it received. Keeping a stale "healthy" on
     * screen over a Core that stopped is the failure this endpoint exists to
     * make impossible.
     */
    fun runtime(): JSONObject = getJson("/api/runtime")

    /** Things waiting for a person, ranked. See `wavr/attention.py`. */
    fun attention(): JSONObject = getJson("/api/attention")

    /**
     * Whether this Space can support an experience.
     *
     * Not permission — a person grants that. This says what a room can produce.
     */
    fun compatibility(manifest: JSONObject, room: String = ""): JSONObject =
        postJson(
            "/api/experience/compatibility",
            JSONObject().put("manifest", manifest).put("room", room),
        )

    /**
     * Events since [since], newest last.
     *
     * Polling rather than a socket: an Android app is asleep most of the time,
     * and holding a WebSocket open through Doze costs battery for events nobody
     * is on screen to see. Pass the last event's `at` back in and nothing is
     * missed — the Core's tail is bounded at 200, which no real house outruns.
     */
    fun events(since: String = "", limit: Int = 50): List<SpatialEvent> {
        val path = "/api/events/recent?since=${enc(since)}&limit=$limit"
        val arr = getJson(path).optJSONArray("events") ?: JSONArray()
        return (0 until arr.length()).map { SpatialEvent.from(arr.getJSONObject(it)) }
    }

    // -- Sessions ----------------------------------------------------------

    /**
     * Open a session: a name for "this experience is running, in this room".
     *
     * Wavr will tell you when that room changes or when a screen appears in it.
     * It does NOT move your application's state anywhere — that is yours to
     * carry, and a platform that pretended otherwise would lose somebody's
     * half-finished form on the way to the television.
     */
    fun openSession(experienceId: String, room: String = ""): String =
        postJson("/api/experience/sessions", JSONObject()
            .put("experience_id", experienceId)
            .put("room", room)).optString("session_id")

    /**
     * Re-evaluate a session and get back what changed.
     *
     * The handoff primitive. It reports that a display became available;
     * whether to offer it is your decision, because it depends on what your
     * experience IS — a recipe follows somebody to a screen, a private message
     * does not.
     */
    fun observeSession(sessionId: String, room: String = ""): List<SpatialEvent> {
        val body = postJson(
            "/api/experience/sessions/${enc(sessionId)}/observe",
            JSONObject().put("room", room))
        val arr = body.optJSONArray("events") ?: JSONArray()
        return (0 until arr.length()).map { SpatialEvent.from(arr.getJSONObject(it)) }
    }

    fun closeSession(sessionId: String) {
        transport(Request("DELETE", "$baseUrl/api/experience/sessions/${enc(sessionId)}",
                          headers(), null))
    }

    // -- transport ---------------------------------------------------------

    private fun getJson(path: String): JSONObject = parse(
        transport(Request("GET", "$baseUrl$path", headers(), null)), path,
    )

    private fun postJson(path: String, body: JSONObject): JSONObject = parse(
        transport(
            Request(
                "POST", "$baseUrl$path",
                headers() + ("Content-Type" to "application/json"),
                body.toString(),
            ),
        ),
        path,
    )

    private fun headers(): Map<String, String> = buildMap {
        put("Accept", "application/json")
        // The Core's CSRF guard for same-origin browser calls. Harmless here.
        put("X-Wavr-Local", "1")
        if (token.isNotEmpty()) put("Authorization", "Bearer $token")
    }

    private fun parse(raw: String, path: String): JSONObject = try {
        JSONObject(raw)
    } catch (e: Exception) {
        throw WavrException.Protocol("$path did not return JSON", e)
    }

    private fun enc(value: String): String = URLEncoder.encode(value, "UTF-8")

    companion object {
        /** The context and event shape this SDK was written against. */
        const val PROTOCOL_VERSION = 1
    }

    /** One HTTP call, as data — which is what makes the transport injectable. */
    data class Request(
        val method: String,
        val url: String,
        val headers: Map<String, String>,
        val body: String?,
    )
}

/** Failures an application can branch on without parsing a message. */
sealed class WavrException(message: String, cause: Throwable? = null) :
    Exception(message, cause) {

    /** The Core did not answer. Retrying may work. */
    class Unreachable(message: String, cause: Throwable? = null) :
        WavrException(message, cause)

    /** The credential was refused. Retrying will not help. */
    class Auth(message: String) : WavrException(message)

    /** No such room, anchor or route. */
    class NotFound(message: String) : WavrException(message)

    /** The Core answered with something this SDK cannot read. */
    class Protocol(message: String, cause: Throwable? = null) :
        WavrException(message, cause)

    /** The Core failed. Usually worth retrying. */
    class Server(message: String, val status: Int) : WavrException(message)
}

data class Space(val spaceId: String, val name: String)

/** What an application may ask a room for. */
enum class Capability(val wire: String) {
    PRESENCE("presence"),
    COUNT("count"),
    POSITION("position"),
    ANCHORS("anchors"),
    DISPLAY("display"),
    AUDIO("audio"),
}

/** One room, as an application sees it. */
data class RoomContext(
    val room: String,
    val precision: String,
    val confidence: Double,
    /** `null` when nothing here reported. */
    val occupied: Boolean?,
    /**
     * `null` when nothing in this room can count.
     *
     * Never 0 for "unknown" — the nullability is the point, and it is what stops
     * an application rendering a confident "0 people" about a room nobody is
     * counting.
     */
    val occupancy: Int?,
    val occupancyKnown: Boolean,
    val capabilities: List<String>,
    val anchors: List<Anchor>,
    val devices: List<ContextDevice>,
    /** Plain sentences naming what this room cannot answer right now. */
    val limitations: List<String>,
) {
    fun can(capability: Capability): Boolean = capability.wire in capabilities

    fun anchor(name: String): Anchor? = anchors.firstOrNull { it.name == name }

    /**
     * Devices here that SAID they have a screen.
     *
     * One that never reported is not counted: "did not say" and "has no screen"
     * send an application to different places.
     */
    fun displays(): List<ContextDevice> = devices.filter { it.display == true }

    companion object {
        fun from(o: JSONObject): RoomContext = RoomContext(
            room = o.optString("room"),
            precision = o.optString("precision", "none"),
            confidence = o.optDouble("confidence", 0.0),
            occupied = if (o.isNull("occupied")) null else o.optBoolean("occupied"),
            occupancy = if (o.isNull("occupancy")) null else o.optInt("occupancy"),
            occupancyKnown = o.optBoolean("occupancy_known", false),
            capabilities = o.optJSONArray("capabilities").strings(),
            anchors = o.optJSONArray("anchors").objects().map { Anchor.from(it) },
            devices = o.optJSONArray("devices").objects().map { ContextDevice.from(it) },
            limitations = o.optJSONArray("limitations").strings(),
        )
    }
}

data class ContextDevice(
    val deviceId: String,
    val name: String,
    val functions: List<String>,
    /** Tristate. `null` means the device never said — which is NOT "no screen". */
    val display: Boolean?,
    val audio: Boolean?,
    val uwb: Boolean?,
) {
    companion object {
        fun from(o: JSONObject) = ContextDevice(
            deviceId = o.optString("device_id"),
            name = o.optString("name"),
            functions = o.optJSONArray("functions").strings(),
            display = o.tristate("display"),
            audio = o.tristate("audio"),
            uwb = o.tristate("uwb"),
        )
    }
}

data class Anchor(
    val anchorId: String,
    val name: String,
    val room: String,
    val kind: String,
    val positioned: Boolean,
    val x: Double?,
    val y: Double?,
    /** True when the room this anchor names no longer exists. */
    val orphaned: Boolean,
) {
    companion object {
        fun from(o: JSONObject) = Anchor(
            anchorId = o.optString("anchor_id"),
            name = o.optString("name"),
            room = o.optString("room"),
            kind = o.optString("kind", "logical"),
            positioned = o.optBoolean("positioned", false),
            x = if (o.has("x") && !o.isNull("x")) o.optDouble("x") else null,
            y = if (o.has("y") && !o.isNull("y")) o.optDouble("y") else null,
            orphaned = o.optBoolean("orphaned", false),
        )
    }
}

data class SpatialEvent(
    val event: String,
    val room: String,
    val at: String,
    val raw: JSONObject,
) {
    companion object {
        fun from(o: JSONObject) = SpatialEvent(
            event = o.optString("event"),
            room = o.optString("room"),
            at = o.optString("at"),
            raw = o,
        )
    }
}

private fun JSONArray?.strings(): List<String> =
    if (this == null) emptyList() else (0 until length()).map { optString(it) }

private fun JSONArray?.objects(): List<JSONObject> =
    if (this == null) emptyList()
    else (0 until length()).mapNotNull { optJSONObject(it) }

/** Absent or JSON null both mean "did not say", which is not `false`. */
private fun JSONObject.tristate(key: String): Boolean? =
    if (!has(key) || isNull(key)) null else optBoolean(key)

/**
 * The real transport.
 *
 * Separate from the client so a test drives canned bodies with no network and no
 * device — and so the error mapping lives in one place rather than at each call
 * site, where one of them would eventually forget that 403 is not 500.
 */
fun defaultTransport(req: WavrClient.Request): String {
    val conn = try {
        (URL(req.url).openConnection() as HttpURLConnection)
    } catch (e: Exception) {
        throw WavrException.Unreachable("cannot reach Wavr at ${req.url}", e)
    }
    return try {
        conn.requestMethod = req.method
        conn.connectTimeout = 10_000
        conn.readTimeout = 10_000
        conn.instanceFollowRedirects = false // a credentialed request follows nothing
        req.headers.forEach { (k, v) -> conn.setRequestProperty(k, v) }
        if (req.body != null) {
            conn.doOutput = true
            conn.outputStream.use { it.write(req.body.toByteArray()) }
        }
        when (val code = conn.responseCode) {
            401, 403 -> throw WavrException.Auth("not authorised for ${req.url}")
            404 -> throw WavrException.NotFound("${req.url} not found")
            in 200..299 -> conn.inputStream.bufferedReader().use { it.readText() }
            else -> throw WavrException.Server("Wavr returned $code", code)
        }
    } catch (e: WavrException) {
        throw e
    } catch (e: IOException) {
        throw WavrException.Unreachable("cannot reach Wavr at ${req.url}", e)
    } finally {
        conn.disconnect()
    }
}
