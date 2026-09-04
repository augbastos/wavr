package dev.wavr.sdk

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * JVM unit tests for the Spatial SDK. No device, no emulator, no network.
 *
 *     ./gradlew :app:testDebugUnitTest
 *
 * The tests worth reading are the nullable-occupancy ones. A typed SDK's main
 * contribution here is making "nothing in this room can count" impossible to
 * confuse with "nobody is in this room", and that is a property of the type
 * rather than of anybody's discipline.
 */
class WavrClientTest {

    private val spaceContext = """
        {
          "protocol_version": 1,
          "space": {"space_id": "sp_1", "name": "My Home"},
          "rooms": [
            {"room": "kitchen", "precision": "count", "confidence": 0.8,
             "occupied": true, "occupancy": 2, "occupancy_known": true,
             "capabilities": ["presence", "count"], "anchors": [],
             "devices": [], "sensors": [],
             "limitations": ["Wavr can count people in kitchen, not place them within it."]},
            {"room": "attic", "precision": "none", "confidence": 0.0,
             "occupied": null, "occupancy": null, "occupancy_known": false,
             "capabilities": [], "anchors": [], "devices": [], "sensors": [],
             "limitations": ["No sensor covers attic."]}
          ]
        }
    """.trimIndent()

    /** A transport that answers from a table and records what was asked. */
    private fun fake(
        routes: Map<String, Any>,
        log: MutableList<WavrClient.Request> = mutableListOf(),
    ): (WavrClient.Request) -> String = { req ->
        log.add(req)
        val path = req.url.substringAfter("://").substringAfter("/").let { "/$it" }
        when (val hit = routes[path]) {
            null -> throw WavrException.NotFound("$path not found")
            is Throwable -> throw hit
            else -> hit.toString()
        }
    }

    private fun client(
        routes: Map<String, Any>,
        log: MutableList<WavrClient.Request> = mutableListOf(),
        token: String = "",
    ) = WavrClient("http://core.test:8000", token = token, transport = fake(routes, log))

    // -- Connecting and version negotiation --------------------------------

    @Test
    fun `connect learns the Space and the contract version`() {
        val w = client(mapOf("/api/experience/context" to spaceContext)).connect()
        assertEquals("My Home", w.space?.name)
        assertEquals(WavrClient.PROTOCOL_VERSION, w.protocolVersion)
        assertFalse(w.protocolAhead)
    }

    @Test
    fun `a newer Core is surfaced, not thrown on`() {
        // Throwing would break every installed application the day somebody
        // updates their Core.
        val ahead = spaceContext.replace("\"protocol_version\": 1", "\"protocol_version\": 99")
        val w = client(mapOf("/api/experience/context" to ahead)).connect()
        assertTrue(w.protocolAhead)
    }

    @Test
    fun `the token rides on every request`() {
        val log = mutableListOf<WavrClient.Request>()
        client(mapOf("/api/experience/context" to spaceContext), log, token = "abc").connect()
        assertEquals("Bearer abc", log[0].headers["Authorization"])
    }

    // -- The null that must never become a zero -----------------------------

    @Test
    fun `an uncounted room reports null occupancy`() {
        val rooms = client(mapOf("/api/experience/context" to spaceContext))
            .contexts().associateBy { it.room }
        assertNull(rooms["attic"]!!.occupancy)
        assertFalse(rooms["attic"]!!.occupancyKnown)
    }

    @Test
    fun `a real count survives as a number`() {
        val rooms = client(mapOf("/api/experience/context" to spaceContext))
            .contexts().associateBy { it.room }
        assertEquals(2, rooms["kitchen"]!!.occupancy)
        assertTrue(rooms["kitchen"]!!.occupancyKnown)
    }

    @Test
    fun `an unreported occupied flag is null rather than false`() {
        val rooms = client(mapOf("/api/experience/context" to spaceContext))
            .contexts().associateBy { it.room }
        assertNull(rooms["attic"]!!.occupied)
    }

    @Test
    fun `capabilities are a question`() {
        val kitchen = client(mapOf("/api/experience/context" to spaceContext))
            .contexts().first { it.room == "kitchen" }
        assertTrue(kitchen.can(Capability.COUNT))
        assertFalse(kitchen.can(Capability.POSITION))
    }

    @Test
    fun `limitations reach the application verbatim`() {
        val attic = client(mapOf("/api/experience/context" to spaceContext))
            .contexts().first { it.room == "attic" }
        assertEquals(listOf("No sensor covers attic."), attic.limitations)
    }

    @Test
    fun `a device that never reported a display is not counted as having none`() {
        val ctx = RoomContext.from(
            JSONObject(
                """
                {"room": "kitchen", "devices": [
                  {"device_id": "a", "display": true},
                  {"device_id": "b", "display": null},
                  {"device_id": "c"}]}
                """.trimIndent(),
            ),
        )
        assertEquals(listOf("a"), ctx.displays().map { it.deviceId })
        assertNull(ctx.devices[1].display)
        assertNull(ctx.devices[2].display)
    }

    // -- Typed failures -----------------------------------------------------

    @Test
    fun `a refused credential is its own exception`() {
        val w = client(mapOf("/api/experience/context" to WavrException.Auth("nope")))
        try {
            w.connect()
            fail("expected an auth failure")
        } catch (e: WavrException.Auth) {
            // A settings screen, not a retry loop.
        }
    }

    @Test
    fun `an unreachable Core is a different exception from a refused one`() {
        // The distinction is the whole reason these are separate types: one is a
        // retry, the other is a settings screen, and code that catches
        // `Exception` treats them identically.
        val w = client(mapOf("/api/experience/context" to WavrException.Unreachable("down")))
        var kind = ""
        try {
            w.connect()
            fail("expected an unreachable failure")
        } catch (e: WavrException.Auth) {
            kind = "auth"
        } catch (e: WavrException.Unreachable) {
            kind = "unreachable"
        }
        assertEquals("unreachable", kind)
    }

    @Test
    fun `a non-JSON answer is a protocol error`() {
        val w = client(mapOf("/api/experience/context" to "<html>nope</html>"))
        try {
            w.connect()
            fail("expected a protocol failure")
        } catch (e: WavrException.Protocol) {
            assertTrue(e.message!!.contains("did not return JSON"))
        }
    }

    // -- The rest of the surface -------------------------------------------

    @Test
    fun `a room name is url-encoded`() {
        val log = mutableListOf<WavrClient.Request>()
        val w = client(
            mapOf("/api/experience/context/living+room" to """{"room": "living room"}"""),
            log,
        )
        w.context("living room")
        assertTrue(log[0].url.endsWith("/api/experience/context/living+room"))
    }

    @Test
    fun `anchors parse their positioned flag and frame`() {
        val w = client(
            mapOf(
                "/api/anchors" to """
                    {"anchors": [
                      {"anchor_id": "a1", "name": "Counter", "room": "kitchen",
                       "kind": "point", "positioned": true, "x": 1.2, "y": 0.4,
                       "frame": "room"},
                      {"anchor_id": "a2", "name": "Sofa", "room": "gone",
                       "kind": "logical", "positioned": false, "orphaned": true}]}
                """.trimIndent(),
            ),
        )
        val found = w.anchors()
        assertEquals(1.2, found[0].x!!, 1e-9)
        assertNull(found[1].x)
        assertTrue(found[1].orphaned)
    }

    @Test
    fun `resolving an external id returns every match`() {
        val w = client(
            mapOf(
                "/api/anchors/resolve/arkit/ABC" to
                    """{"anchors": [{"anchor_id": "a"}, {"anchor_id": "b"}]}""",
            ),
        )
        assertEquals(2, w.resolveAnchor("arkit", "ABC").size)
    }

    @Test
    fun `compatibility posts the manifest`() {
        val log = mutableListOf<WavrClient.Request>()
        val w = client(
            mapOf("/api/experience/compatibility" to """{"status": "FULLY_SUPPORTED"}"""),
            log,
        )
        val out = w.compatibility(JSONObject().put("id", "x").put("name", "X"), "kitchen")
        assertEquals("FULLY_SUPPORTED", out.optString("status"))
        assertEquals("POST", log[0].method)
        assertTrue(log[0].body!!.contains("\"manifest\""))
    }

    @Test
    fun `events carry their timestamp so a caller can resume`() {
        val w = client(
            mapOf(
                "/api/events/recent?since=&limit=50" to """
                    {"events": [{"event": "room.occupancy_changed", "room": "kitchen",
                                 "at": "2026-09-04T12:00:00+00:00", "occupied": true}]}
                """.trimIndent(),
            ),
        )
        val events = w.events()
        assertEquals("room.occupancy_changed", events[0].event)
        assertEquals("2026-09-04T12:00:00+00:00", events[0].at)
        assertTrue(events[0].raw.optBoolean("occupied"))
    }

    // -- Sessions ----------------------------------------------------------

    @Test
    fun `opening a session names the experience and the room`() {
        val log = mutableListOf<WavrClient.Request>()
        val w = client(
            mapOf("/api/experience/sessions" to """{"session_id": "ses_1"}"""),
            log,
        )
        assertEquals("ses_1", w.openSession("recipe", "kitchen"))
        assertEquals("POST", log[0].method)
        assertTrue(log[0].body!!.contains("recipe"))
        assertTrue(log[0].body!!.contains("kitchen"))
    }

    @Test
    fun `observing a session returns what changed, not a decision`() {
        // Wavr reports that a display became available. Whether to offer it
        // depends on what the experience IS, which is the application's call.
        val w = client(
            mapOf(
                "/api/experience/sessions/ses_1/observe" to """
                    {"session_id": "ses_1", "room": "living", "events": [
                      {"event": "experience.target_available", "room": "living",
                       "at": "2026-09-04T12:00:00+00:00"}]}
                """.trimIndent(),
            ),
        )
        val events = w.observeSession("ses_1", "living")
        assertEquals("experience.target_available", events[0].event)
        assertEquals("living", events[0].room)
    }

    @Test
    fun `a session id is url-encoded`() {
        val log = mutableListOf<WavrClient.Request>()
        val w = client(mapOf("/api/experience/sessions/a%2Fb/observe" to "{}"), log)
        w.observeSession("a/b")
        assertTrue(log[0].url.endsWith("/api/experience/sessions/a%2Fb/observe"))
    }

    @Test
    fun `closing a session uses DELETE`() {
        val log = mutableListOf<WavrClient.Request>()
        val w = client(mapOf("/api/experience/sessions/ses_1" to "{}"), log)
        w.closeSession("ses_1")
        assertEquals("DELETE", log[0].method)
    }

    // -- Runtime presence ---------------------------------------------------
    //
    // The same rule the tray and the web shell follow: a Core that will not
    // answer must not be renderable as a healthy one. Here that means the
    // client THROWS rather than returning an empty object a caller could mistake
    // for "nothing wrong".

    @Test
    fun `runtime returns the Core's own conclusion, not the client's`() {
        val c = client(mapOf("/api/runtime" to """
            {"state":"degraded","headline":"Wavr — degraded · My Home",
             "space":"My Home","last_state_age_s":42,
             "findings":[{"key":"sensors","state":"degraded",
                          "text":"1 of 3 sensors are not reporting."}]}
        """.trimIndent()))
        val body = c.runtime()
        assertEquals("degraded", body.getString("state"))
        assertEquals("My Home", body.getString("space"))
        // The words come from the Core. A client that writes its own sentence
        // here is a second implementation of health, and two eventually
        // disagree in front of somebody with no way to tell which is right.
        assertTrue(body.getString("headline").contains("degraded"))
    }

    @Test
    fun `an unreachable Core throws rather than returning something empty`() {
        val c = client(mapOf<String, Any>(
            "/api/runtime" to WavrException.Unreachable("connection refused")))
        try {
            c.runtime()
            fail("an unreachable Core returned normally")
        } catch (e: WavrException) {
            // Correct: the caller has to decide what to render, and the only
            // honest thing to render is "not answering".
        }
    }

    @Test
    fun `attention carries the count a badge renders`() {
        val c = client(mapOf("/api/attention" to """
            {"total":2,"blocking":1,"degraded":1,"info":0,
             "headline":"2 things need your attention",
             "items":[{"key":"pairing:r1","band":"blocking",
                       "title":"Ana's phone wants to join"}]}
        """.trimIndent()))
        val body = c.attention()
        assertEquals(2, body.getInt("total"))
        assertEquals(1, body.getInt("blocking"))
    }
}
