#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>

// All HTTPS calls to the Wavr instance. firmware/NODE_PROTOCOL.md is the
// single source of truth for this wire contract -- keep this file
// byte-compatible with it. Owns the node's bearer token (NVS-persisted) and
// the monotonic telemetry seq counter; nothing else in this firmware talks
// to Wavr directly.
namespace WavrClient {

enum class Command { kRun, kSleep, kRevoked, kUnreachable };

// Loads the stored URL/token; if there is no token yet, redeems the one-time
// code the portal saved (POST /api/nodes/enroll). Returns false if
// enrollment could not complete (bad/expired code, Wavr unreachable, ...) --
// caller should back off and retry, NOT wipe provisioning.
bool begin();

// POST /api/nodes/telemetry with whatever the active SensorDriver filled
// `doc` with (this stamps "seq" itself). Fire-and-forget: a single failed
// POST is not fatal, the next tick tries again.
void sendTelemetry(JsonDocument& doc);

// POST /api/nodes/heartbeat. Returns the kill-switch command Wavr wants this
// node to obey right now:
//   kRun      -- body {"command":"run"} (or "ok") -- sense normally.
//   kSleep    -- body {"command":"sleep"} -- remote-OFF reached the hardware:
//                stop sensing and go dark (the caller also slows its own
//                heartbeat cadence to WAVR_HEARTBEAT_DISABLED_MS).
//   kRevoked  -- body {"command":"revoked"} OR an HTTP 401/403 on this call.
//                A revoked node's token hash is cleared server-side
//                (NodeStore.revoke(), anti-resurrection), so in practice it
//                can NEVER authenticate again and this route always comes
//                back 401/403 for it -- there is no friendlier in-body
//                "revoked" to expect. Either signal means the same thing:
//                factory-reset now.
//   kUnreachable -- no definitive response at all (DNS/TCP/TLS failure,
//                timeout, Wavr mid-restart) or an unparseable/unknown 200
//                body. Callers MUST treat this as "keep doing what you were
//                doing" -- never as an implicit sleep/revoke. This is the
//                fail-gracefully path: a transient network blip must never
//                brick or factory-reset a node.
Command sendHeartbeat();

// POST /api/nodes/reactivate {press_count}. Call this on every physical
// kill-switch short-press; the server ignores a non-increasing press_count.
void sendReactivate(uint32_t pressCount);

}  // namespace WavrClient
