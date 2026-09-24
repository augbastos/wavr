// The Node role of firmware/NODE_PROTOCOL.md, for any machine that can run a
// process: an old laptop, a Pi, a thin client, a NAS -- whatever has a sensor
// to contribute. The ESP32 firmware is the other implementation of the same
// contract; neither invents protocol, and the Core is the only parser of what
// a node sends.
#pragma once

#include <string>

namespace wavr {

struct NodeOptions {
  std::string state_path = "wavr-node.json";
  std::string sensor;            // "ld2450:COM3", "ld2450:/dev/ttyUSB0", "replay:<file>"
  int telemetry_ms = 1000;       // NODE_PROTOCOL: every ~0.5-1 s while sensing
  int heartbeat_ms = 30000;      // every ~30 s while active
  int disabled_heartbeat_ms = 60000;
  double run_seconds = 0;        // 0 = forever; >0 bounds a run (tests, benchmarks)
};

// What a node keeps between runs (the state file). Holds its bearer token.
struct NodeState {
  std::string url, node_id, token, pin, state = "active";
  long long seq = 0;
  long long press_count = 0;
};

// Parse a state file's text. False (and *s untouched) unless it names a Core
// and carries a token; every other field falls back to its default when
// missing or of the wrong type. Never throws.
bool parse_node_state(const std::string& text, NodeState* s);

// Exit codes, so a service manager can tell the cases apart.
constexpr int kNodeOk = 0;
constexpr int kNodeUsage = 64;
constexpr int kNodeFailed = 1;
constexpr int kNodeRevoked = 3;   // the Core revoked this node: state wiped, re-enrol

int node_enroll(const std::string& url, const std::string& code, const NodeOptions& opt);
int node_run(const NodeOptions& opt);
int node_reactivate(const NodeOptions& opt);
int node_status(const NodeOptions& opt);

}  // namespace wavr
