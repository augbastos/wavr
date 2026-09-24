package dev.wavr.core.nativeui

import org.json.JSONArray
import org.json.JSONObject

data class Snapshot(
    val schema: Int?, val reachable: Boolean?, val error: String?, val exitCode: Int?,
    val runtime: RuntimeStatus?, val attention: Attention?, val rooms: List<Room>?,
    val roomsReadable: Boolean?, val privacy: Privacy?
) {
    companion object {
        fun parse(json: String): Snapshot = JSONObject(json).let { root ->
            Snapshot(root.int("schema"), root.bool("reachable"), root.string("error"),
                root.int("exit_code"), root.obj("runtime")?.let(::parseRuntime),
                root.obj("attention")?.let(::parseAttention),
                root.array("rooms")?.objects(::parseRoom), root.bool("rooms_readable"),
                root.obj("privacy")?.let { Privacy(it.bool("watch")) })
        }
    }
}

data class RuntimeStatus(
    val state: String?, val headline: String?, val space: String?, val role: String?,
    val uptimeS: Double?, val lastStateAgeS: Double?, val findings: List<Finding>?
)
data class Finding(val key: String?, val state: String?, val text: String?, val detail: String?)
data class Attention(
    val total: Int?, val blocking: Int?, val degraded: Int?, val info: Int?,
    val headline: String?, val couldNotCheck: List<String?>?, val items: List<AttentionItem>?
)
data class AttentionItem(
    val key: String?, val band: String?, val title: String?, val detail: String?,
    val where: String?, val action: String?, val since: String?, val count: Int?
)
data class Room(
    val room: String?, val occupied: Boolean?, val confidence: Double?,
    val personCount: Int?, val precisionLevel: String?, val explanation: String?,
    val ts: String?, val watch: Boolean?, val unrecognized: Boolean?,
    val sources: List<Source>?
)
data class Source(
    val modality: String?, val sensorId: String?, val presence: Boolean?,
    val confidence: Double?, val ageS: Double?, val health: String?, val count: Int?
)
data class Privacy(val watch: Boolean?)

data class DeviceManifest(
    val platform: String?, val tier: String?, val functions: List<String?>?,
    val capabilities: Map<String, Boolean?>?
) {
    companion object {
        fun parse(json: String): DeviceManifest = JSONObject(json).let { root ->
            val caps = root.obj("capabilities")?.let { obj ->
                obj.keys().asSequence().associateWith { key -> obj.bool(key) }
            }
            DeviceManifest(root.string("platform"), root.string("compute_tier"),
                root.array("functions_supported")?.strings(), caps)
        }
    }
}

private fun JSONObject.value(key: String): Any? = if (has(key)) opt(key).takeUnless { it == JSONObject.NULL } else null
private fun JSONObject.string(key: String): String? = value(key) as? String
private fun JSONObject.bool(key: String): Boolean? = value(key) as? Boolean
private fun JSONObject.int(key: String): Int? = when (val number = value(key)) {
    is Int -> number
    is Long -> number.takeIf { it >= Int.MIN_VALUE.toLong() && it <= Int.MAX_VALUE.toLong() }?.toInt()
    else -> null
}
private fun JSONObject.double(key: String): Double? = (value(key) as? Number)?.toDouble()?.takeIf { it.isFinite() }
private fun JSONObject.obj(key: String): JSONObject? = value(key) as? JSONObject
private fun JSONObject.array(key: String): JSONArray? = value(key) as? JSONArray
private fun JSONArray.strings(): List<String?> = (0 until length()).map { opt(it) as? String }
private fun <T> JSONArray.objects(parse: (JSONObject) -> T): List<T> =
    (0 until length()).mapNotNull { (opt(it) as? JSONObject)?.let(parse) }

private fun parseRuntime(o: JSONObject) = RuntimeStatus(
    o.string("state"), o.string("headline"), o.string("space"), o.string("role"),
    o.double("uptime_s"), o.double("last_state_age_s"),
    o.array("findings")?.objects { Finding(it.string("key"), it.string("state"), it.string("text"), it.string("detail")) }
)
private fun parseAttention(o: JSONObject) = Attention(
    o.int("total"), o.int("blocking"), o.int("degraded"), o.int("info"),
    o.string("headline"), o.array("could_not_check")?.strings(),
    o.array("items")?.objects {
        AttentionItem(it.string("key"), it.string("band"), it.string("title"),
            it.string("detail"), it.string("where"), it.string("action"),
            it.string("since"), it.int("count"))
    }
)
private fun parseRoom(o: JSONObject) = Room(
    o.string("room"), o.bool("occupied"), o.double("confidence"),
    o.int("person_count"), o.string("precision_level"), o.string("explanation"),
    o.string("ts"), o.bool("watch"), o.bool("unrecognized"),
    o.array("sources")?.objects {
        Source(it.string("modality"), it.string("sensor_id"), it.bool("presence"),
            it.double("confidence"), it.double("age_s"), it.string("health"), it.int("count"))
    }
)
