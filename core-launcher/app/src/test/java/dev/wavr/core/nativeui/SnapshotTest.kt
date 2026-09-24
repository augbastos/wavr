package dev.wavr.core.nativeui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SnapshotTest {
    private fun sample(name: String): String =
        requireNotNull(javaClass.getResourceAsStream("/$name.json"))
            .bufferedReader().use { it.readText() }

    @Test fun healthySamplePreservesValuesAndNulls() {
        val snapshot = Snapshot.parse(sample("healthy"))
        assertEquals(1, snapshot.schema)
        assertEquals(true, snapshot.reachable)
        assertNull(snapshot.error)
        assertEquals("Home", snapshot.runtime?.space)
        assertEquals(3600.0, snapshot.runtime?.uptimeS)
        assertNull(snapshot.runtime?.findings?.single()?.detail)
        assertEquals("degraded", snapshot.attention?.items?.single()?.band)
        assertTrue(snapshot.attention?.couldNotCheck?.isEmpty() == true)
        assertEquals(true, snapshot.rooms?.single()?.occupied)
        assertEquals(0.87, snapshot.rooms?.single()?.confidence)
        assertEquals("mmwave", snapshot.rooms?.single()?.sources?.single()?.modality)
        assertEquals(false, snapshot.privacy?.watch)
    }

    @Test fun unreachableSampleDoesNotInventRuntimeOrRooms() {
        val snapshot = Snapshot.parse(sample("unreachable"))
        assertEquals(false, snapshot.reachable)
        assertEquals(2, snapshot.exitCode)
        assertNull(snapshot.runtime)
        assertNull(snapshot.attention)
        assertTrue(snapshot.rooms?.isEmpty() == true)
        assertFalse(snapshot.roomsReadable ?: true)
    }

    @Test fun missingAndWrongTypesRemainUnknown() {
        val snapshot = Snapshot.parse("""{
            "schema":"1","reachable":"false","exit_code":1.5,
            "runtime":{"state":false,"uptime_s":"10","findings":"none"},
            "attention":{"total":"0","could_not_check":[true,null,"pending"],"items":[]},
            "rooms":[{"room":3,"occupied":0,"confidence":"0.9","person_count":false,
                "sources":[{"modality":true,"presence":"yes","age_s":"1"}]}],
            "rooms_readable":0,"privacy":{"watch":"false"}
        }""")
        assertNull(snapshot.schema)
        assertNull(snapshot.reachable)
        assertNull(snapshot.exitCode)
        assertNull(snapshot.runtime?.state)
        assertNull(snapshot.runtime?.uptimeS)
        assertNull(snapshot.runtime?.findings)
        assertNull(snapshot.attention?.total)
        assertEquals(listOf(null, null, "pending"), snapshot.attention?.couldNotCheck)
        assertNull(snapshot.rooms?.single()?.room)
        assertNull(snapshot.rooms?.single()?.occupied)
        assertNull(snapshot.rooms?.single()?.confidence)
        assertNull(snapshot.rooms?.single()?.personCount)
        assertNull(snapshot.rooms?.single()?.sources?.single()?.modality)
        assertNull(snapshot.rooms?.single()?.sources?.single()?.presence)
        assertNull(snapshot.roomsReadable)
        assertNull(snapshot.privacy?.watch)
    }

    @Test fun manifestKeepsUnprobedCapabilitiesUnknown() {
        val manifest = DeviceManifest.parse("""{"platform":"android","compute_tier":"medium",
            "functions_supported":["client"],"capabilities":{"camera":true,"ble":null,"wifi":"yes"}}""")
        assertEquals("android", manifest.platform)
        assertEquals("medium", manifest.tier)
        assertEquals(listOf("client"), manifest.functions)
        assertEquals(true, manifest.capabilities?.get("camera"))
        assertNull(manifest.capabilities?.get("ble"))
        assertNull(manifest.capabilities?.get("wifi"))
        assertNull(manifest.capabilities?.get("missing"))
    }
}
