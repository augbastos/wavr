package dev.wavr.core.nativeui

import java.io.File
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * The Kotlin parser against the canonical answer key, conformance/client_view.json
 * (generated from backend/wavr/client_view.py): every expected snapshot must read
 * back without losing a value or inventing one.
 */
class SnapshotConformanceTest {
    private fun fixture(): JSONObject {
        // Unit tests run from core-launcher/app.
        val f = listOf("../../conformance/client_view.json", "../conformance/client_view.json")
            .map(::File).first { it.exists() }
        return JSONObject(f.readText())
    }

    private fun JSONObject.orNull(key: String): Any? = opt(key).takeUnless { it == JSONObject.NULL }

    @Test
    fun everyCanonicalSnapshotReadsBackExactly() {
        val cases = fixture().getJSONArray("cases")
        for (i in 0 until cases.length()) {
            val case = cases.getJSONObject(i)
            val name = case.getString("name")
            val want = case.getJSONObject("snapshot")
            val got = Snapshot.parse(want.toString())
            assertEquals(name, want.getInt("exit_code"), got.exitCode)
            assertEquals(name, want.getBoolean("reachable"), got.reachable)
            assertEquals(name, want.getBoolean("rooms_readable"), got.roomsReadable)
            if (want.orNull("runtime") == null) assertNull(name, got.runtime)
            else assertEquals(name, want.getJSONObject("runtime").orNull("state"), got.runtime?.state)
            if (want.orNull("attention") == null) assertNull(name, got.attention)
            else assertEquals(name, want.getJSONObject("attention").getInt("total"), got.attention?.total)
            val rooms = want.getJSONArray("rooms")
            assertEquals(name, rooms.length(), got.rooms?.size)
            for (r in 0 until rooms.length()) {
                val wr = rooms.getJSONObject(r)
                val gr = assertNotNullAndGet(got.rooms?.get(r), name)
                assertEquals(name, wr.orNull("occupied"), gr.occupied)
                assertEquals(name, (wr.orNull("confidence") as? Number)?.toDouble(), gr.confidence)
                assertEquals(name, wr.orNull("person_count"), gr.personCount)
                assertEquals(name, wr.orNull("precision_level"), gr.precisionLevel)
                assertEquals(name, wr.getJSONArray("sources").length(), gr.sources?.size)
            }
        }
    }

    private fun <T> assertNotNullAndGet(v: T?, msg: String): T {
        assertNotNull(msg, v)
        return v!!
    }
}
