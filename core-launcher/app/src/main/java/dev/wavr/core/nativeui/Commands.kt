package dev.wavr.core.nativeui

import org.json.JSONArray
import org.json.JSONObject

/**
 * A reply to one command of the native command contract
 * (backend/wavr/client_commands.py, run by the native runtime). Kotlin never
 * builds a request or decides what an answer means: the runtime does, and this
 * only reads the reply. `error` is one stable word; `detail` is the Core's own
 * sentence, shown as-is.
 */
data class Reply(
    val ok: Boolean, val status: Int?, val error: String?, val detail: String?,
    val data: Any?
) {
    val obj: JSONObject? get() = data as? JSONObject

    /** One sentence for a person. Never claims a success the reply does not carry. */
    fun explain(): String = when {
        ok -> "Done."
        error == "forbidden" -> "Not allowed from this device" + suffix()
        error == "unauthorized" -> "This device's credential was refused" + suffix()
        error == "unreachable" -> "The Core did not answer" + suffix()
        error == "not_found" -> "The Core does not know that (any more)" + suffix()
        error == "throttled" -> "Too many attempts; wait a minute" + suffix()
        error == "locked" -> "Locked on the Core" + suffix()
        error == "invalid" || error == "conflict" || error == "refused" -> "The Core refused it" + suffix()
        error == "server" -> "The Core failed while doing it" + suffix()
        error == "bad_call" -> "The app sent a malformed request" + suffix()
        else -> "Unknown outcome" + suffix()
    }

    private fun suffix(): String = detail?.let { ": $it" } ?: "."

    companion object {
        /** An empty or unreadable reply is "unreachable", never a success. */
        fun parse(json: String): Reply {
            val root = try {
                JSONObject(json)
            } catch (_: Exception) {
                return Reply(false, null, "unreachable", "the native runtime returned nothing", null)
            }
            return Reply(root.bool("ok") == true, root.int("status"), root.string("error"),
                root.string("detail"), root.value("data"))
        }
    }
}

/** What `wavr_probe` saw: the certificate a Core presents, before any trust. */
data class Probe(val ok: Boolean, val https: Boolean?, val fingerprint: String?, val error: String?) {
    companion object {
        fun parse(json: String): Probe = try {
            JSONObject(json).let {
                Probe(it.bool("ok") == true, it.bool("https"), it.string("fingerprint"), it.string("error"))
            }
        } catch (_: Exception) {
            Probe(false, null, null, "the native runtime returned nothing")
        }
    }
}

/** Fingerprints are compared by eye: group them so a person can. */
fun groupFingerprint(fp: String?): String =
    fp?.split(":")?.chunked(8)?.joinToString("\n") { it.joinToString(":") } ?: "unknown"

data class SourceRow(val name: String, val enabled: Boolean?, val healthy: Boolean?, val state: String?)
data class SensingState(val running: Boolean?, val sources: List<SourceRow>?)

/** GET /api/system as the `sources.list` command returns it. */
fun parseSensing(data: JSONObject?): SensingState = SensingState(
    data?.bool("running"),
    data?.array("sources")?.objects { o ->
        o.string("name")?.let { SourceRow(it, o.bool("enabled"), o.bool("healthy"), o.string("state")) }
    }?.filterNotNull()
)

data class PendingPairing(val requestId: String, val name: String?, val platform: String?)

/** `pairings.list`. The Core's list carries the compare code; the UI does not
 *  show it -- the operator must type the code the joining device displays. */
fun parsePendingPairings(data: JSONObject?): List<PendingPairing>? =
    data?.array("requests")?.objects { o ->
        o.string("request_id")?.let { PendingPairing(it, o.string("requester_name"), o.string("platform")) }
    }?.filterNotNull()

data class PendingNode(val nodeId: String, val nameHint: String?, val sensorHint: String?)

fun parsePendingNodes(data: JSONObject?): List<PendingNode>? =
    data?.array("pending")?.objects { o ->
        o.string("node_id")?.let { PendingNode(it, o.string("name_hint"), o.string("sensor_hint")) }
    }?.filterNotNull()

data class DoctorCheck(val id: String?, val ok: Boolean?, val severity: String?, val detail: String?)

/** `doctor.run`. ok = null is the Core's honest "not applicable", kept as such. */
fun parseDoctor(data: JSONObject?): List<DoctorCheck>? =
    data?.array("checks")?.objects { o ->
        DoctorCheck(o.string("id"), o.bool("ok"), o.string("severity"), o.string("detail"))
    }

internal fun args(vararg pairs: Pair<String, Any?>): String = JSONObject().apply {
    pairs.forEach { (k, v) -> if (v != null) put(k, v) }
}.toString()

