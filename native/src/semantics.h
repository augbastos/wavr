// Wavr semantics implemented natively. Every function here has a canonical
// definition in backend/wavr/ (named beside it) and an answer key in
// conformance/*.json generated from that definition. When they disagree, this
// file is wrong.
#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace wavr {

using json = nlohmann::json;

// A field of JSON that came off the network or the disk: missing, or of any
// other type, gives `fallback`. json::value() throws on a type mismatch, and a
// corrupted file or a misbehaving peer must not end the process.
inline std::string str_field(const json& j, const char* key, const std::string& fallback = "") {
  if (!j.is_object()) return fallback;
  auto it = j.find(key);
  return it != j.end() && it->is_string() ? it->get<std::string>() : fallback;
}
inline long long int_field(const json& j, const char* key, long long fallback = 0) {
  if (!j.is_object()) return fallback;
  auto it = j.find(key);
  return it != j.end() && it->is_number_integer() ? it->get<long long>() : fallback;
}

// -- backend/wavr/status.py ----------------------------------------------------

constexpr int kExitOk = 0;
constexpr int kExitAttention = 1;
constexpr int kExitUnreachable = 2;

// `_human(seconds)`: "never", "4s", "12m", "3h 5m", "2d 4h".
std::string human_duration(const json& seconds);

// `render(runtime, attention, marks)`; `attention` null = could not be read.
std::string render_status(const json& runtime, const json& attention, bool ascii);

// `exit_code(runtime, attention)`.
int status_exit_code(const json& runtime, const json& attention);

// -- backend/wavr/capabilities.py -------------------------------------------------

constexpr long kMinCoreRamMb = 450;

// `_compute_tier(ram_mb, cpus, plat)`; nullopt = unknown.
std::string compute_tier(std::optional<long> ram_mb, std::optional<long> cpus,
                         const std::string& platform);

// `_can_be_core(plat, ram_mb, tier)`.
bool can_be_core(const std::string& platform, std::optional<long> ram_mb,
                 const std::string& tier);

// -- backend/wavr/sources/mmwave.py ------------------------------------------------

// `take_ld2450_frame`, as a stateful framer over a byte stream.
class Ld2450Framer {
 public:
  static constexpr size_t kFrameLen = 30;
  static constexpr size_t kMaxBuffer = 4096;
  static constexpr size_t kKeepOnOverflow = 64;

  void feed(const uint8_t* data, size_t len);
  // The next complete frame, or nullopt when more bytes are needed. Applies
  // the overflow trim exactly when the Python function would.
  std::optional<std::array<uint8_t, kFrameLen>> next();
  const std::vector<uint8_t>& buffered() const { return buf_; }

 private:
  std::vector<uint8_t> buf_;
};

// -- firmware/NODE_PROTOCOL.md (heartbeat) ------------------------------------------

enum class NodeNext { Keep = 0, Active = 1, Disabled = 2, Revoked = 3 };

// http_status nullopt = no HTTP response at all.
NodeNext heartbeat_next_state(std::optional<int> http_status, const std::string& body);

// -- Python truthiness, used wherever the canonical code says `if x:` / `x or y` --

bool truthy(const json& v);

}  // namespace wavr
