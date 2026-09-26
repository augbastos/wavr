package dev.wavr.core.nativeui

import java.io.File
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The Kotlin reader of command replies against conformance/client_commands.json
 * (generated from backend/wavr/client_commands.py): every reply the runtime can
 * produce reads back with the same verdict, and nothing unreadable reads as ok.
 */
class ReplyConformanceTest {
    private fun fixture(): JSONObject {
        val f = listOf("../../conformance/client_commands.json", "../conformance/client_commands.json")
            .map(::File).first { it.exists() }
        return JSONObject(f.readText())
    }

    private fun JSONObject.orNull(key: String): Any? = opt(key).takeUnless { it == JSONObject.NULL }

    @Test
    fun everyCanonicalReplyReadsBackWithTheSameVerdict() {
        val results = fixture().getJSONArray("results")
        for (i in 0 until results.length()) {
            val case = results.getJSONObject(i)
            val want = case.getJSONObject("result")
            val got = Reply.parse(want.toString())
            val name = case.getString("name")
            assertEquals(name, want.getBoolean("ok"), got.ok)
            assertEquals(name, want.orNull("status"), got.status)
            assertEquals(name, want.orNull("error"), got.error)
            assertEquals(name, want.orNull("detail"), got.detail)
            if (!got.ok) assertFalse(name, got.explain() == "Done.")
        }
        val tf = fixture().getJSONObject("transport_failure").getJSONObject("result")
        val got = Reply.parse(tf.toString())
        assertFalse(got.ok)
        assertEquals("unreachable", got.error)
        assertTrue(got.explain().contains("did not answer"))
    }

    @Test
    fun nothingUnreadableIsASuccess() {
        for (text in listOf("", "null", "{", "[]", "{\"ok\": \"true\"}", "{\"ok\": 1}")) {
            assertFalse(text, Reply.parse(text).ok)
        }
        assertNull(Probe.parse("").fingerprint)
        assertFalse(Probe.parse("garbage").ok)
    }

    @Test
    fun pendingListsKeepTheirHintsAsHints() {
        val nodes = parsePendingNodes(JSONObject(
            """{"pending":[{"node_id":"n1","name_hint":"radar","sensor_hint":"ld2450"},{"name_hint":"no id"}]}"""))
        assertEquals(listOf(PendingNode("n1", "radar", "ld2450")), nodes)
        val pairs = parsePendingPairings(JSONObject(
            """{"requests":[{"request_id":"r1","requester_name":"Pixel","platform":"android","compare_code":"123456"}]}"""))
        assertEquals(listOf(PendingPairing("r1", "Pixel", "android")), pairs)
        assertNull(parsePendingPairings(null))
        val sensing = parseSensing(JSONObject(
            """{"running":true,"sources":[{"name":"network","enabled":false,"healthy":false,"state":"stopped"},{"enabled":true}]}"""))
        assertEquals(true, sensing.running)
        assertEquals(listOf(SourceRow("network", false, false, "stopped")), sensing.sources)
        assertNull(parseSensing(null).sources)
    }
}
