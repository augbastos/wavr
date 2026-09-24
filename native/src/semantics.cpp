#include "semantics.h"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <cstring>

namespace wavr {

namespace {

// UTF-8 spelled out, so the source encoding can never change what is printed.
constexpr const char* kDash = "\xE2\x80\x94";     // U+2014, the "no Space" placeholder
constexpr const char* kBullet = "\xE2\x80\xA2";   // U+2022, the mark of an unknown state

struct Mark {
  const char* state;
  const char* unicode;
  const char* ascii;
};

constexpr Mark kMarks[] = {
    {"healthy", "\xE2\x97\x8F", "+"},   // U+25CF
    {"starting", "\xE2\x97\x8B", "-"},  // U+25CB
    {"updating", "\xE2\x97\x8B", "-"},
    {"paused", "\xE2\x97\x8B", "-"},
    {"degraded", "!", "!"},
    {"attention", "!", "!"},
    {"unavailable", "\xC3\x97", "x"},   // U+00D7
};

const char* mark_for(const std::string& state, bool ascii) {
  for (const auto& m : kMarks) {
    if (state == m.state) return ascii ? m.ascii : m.unicode;
  }
  return kBullet;   // status.py: marks.get(state, "•"), in BOTH mark sets
}

// Python's floor division and modulo, which differ from C++'s for negatives.
long long floordiv(long long a, long long b) {
  long long q = a / b;
  if ((a % b != 0) && ((a < 0) != (b < 0))) --q;
  return q;
}
long long floormod(long long a, long long b) { return a - floordiv(a, b) * b; }

// str(value) as Python would print it inside an f-string.
std::string py_str(const json& v) {
  if (v.is_string()) return v.get<std::string>();
  if (v.is_null()) return "None";
  if (v.is_boolean()) return v.get<bool>() ? "True" : "False";
  if (v.is_number_integer()) return std::to_string(v.get<long long>());
  if (v.is_number_unsigned()) return std::to_string(v.get<unsigned long long>());
  if (v.is_number_float()) {
    double d = v.get<double>();
    char buf[64];
    auto res = std::to_chars(buf, buf + sizeof buf, d);
    std::string s(buf, res.ptr);
    if (s.find_first_of(".eEn") == std::string::npos) s += ".0";   // 5.0, not 5
    return s;
  }
  return v.dump();
}

// str.capitalize(): first character upper, the rest lower (ASCII letters).
std::string capitalize(std::string s) {
  for (size_t i = 0; i < s.size(); ++i) {
    unsigned char c = static_cast<unsigned char>(s[i]);
    if (c < 0x80) s[i] = static_cast<char>(i == 0 ? std::toupper(c) : std::tolower(c));
  }
  return s;
}

const json& get_or_null(const json& obj, const char* key) {
  static const json kNull = nullptr;
  if (!obj.is_object()) return kNull;
  auto it = obj.find(key);
  return it == obj.end() ? kNull : *it;
}

bool in(const std::string& s, std::initializer_list<const char*> set) {
  for (const char* x : set) {
    if (s == x) return true;
  }
  return false;
}

}  // namespace

bool truthy(const json& v) {
  if (v.is_null()) return false;
  if (v.is_boolean()) return v.get<bool>();
  if (v.is_number_integer()) return v.get<long long>() != 0;
  if (v.is_number_unsigned()) return v.get<unsigned long long>() != 0;
  if (v.is_number_float()) return v.get<double>() != 0.0;
  if (v.is_string()) return !v.get_ref<const std::string&>().empty();
  return !v.empty();   // array / object
}

std::string human_duration(const json& seconds) {
  if (seconds.is_null()) return "never";
  long long s = 0;
  if (seconds.is_number_float()) {
    s = static_cast<long long>(std::trunc(seconds.get<double>()));   // int(4.9) == 4
  } else if (seconds.is_number()) {
    s = seconds.get<long long>();
  } else if (seconds.is_boolean()) {
    s = seconds.get<bool>() ? 1 : 0;
  }
  if (s < 60) return std::to_string(s) + "s";
  if (s < 3600) return std::to_string(floordiv(s, 60)) + "m";
  if (s < 86400) {
    return std::to_string(floordiv(s, 3600)) + "h " +
           std::to_string(floordiv(floormod(s, 3600), 60)) + "m";
  }
  return std::to_string(floordiv(s, 86400)) + "d " +
         std::to_string(floordiv(floormod(s, 86400), 3600)) + "h";
}

std::string render_status(const json& runtime, const json& attention, bool ascii) {
  std::vector<std::pair<std::string, std::string>> rows;

  const json& state_v = get_or_null(runtime, "state");
  std::string state = runtime.is_object() && runtime.contains("state")
                          ? py_str(state_v)
                          : std::string("unknown");

  const json& space = get_or_null(runtime, "space");
  rows.emplace_back("Space", truthy(space) ? py_str(space) : std::string(kDash));
  rows.emplace_back("Core", std::string(mark_for(state, ascii)) + " " + capitalize(state));
  const json& role = get_or_null(runtime, "role");
  if (truthy(role)) rows.emplace_back("Role", py_str(role));
  rows.emplace_back("Uptime", human_duration(get_or_null(runtime, "uptime_s")));
  const json& age = get_or_null(runtime, "last_state_age_s");
  rows.emplace_back("Last reading",
                    age.is_null() ? std::string("none yet") : human_duration(age) + " ago");

  const json& findings = get_or_null(runtime, "findings");
  if (findings.is_array()) {
    for (const auto& f : findings) {
      const json& key = get_or_null(f, "key");
      if (key.is_string() &&
          in(key.get<std::string>(), {"sensors", "sources", "nodes", "egress", "storage"})) {
        const json& text = get_or_null(f, "text");
        rows.emplace_back(capitalize(key.get<std::string>()),
                          f.contains("text") ? py_str(text) : std::string());
      }
    }
  }
  if (truthy(attention)) {
    const json& headline = get_or_null(attention, "headline");
    rows.emplace_back("Attention",
                      attention.contains("headline") ? py_str(headline) : std::string());
  } else {
    rows.emplace_back("Attention", "could not check");
  }

  size_t width = 0;
  for (const auto& [k, v] : rows) width = std::max(width, k.size());   // ASCII keys
  std::string out = "Wavr";
  for (const auto& [k, v] : rows) {
    out += "\n  " + k + std::string(width - k.size(), ' ') + "  " + v;
  }
  if (findings.is_array()) {
    for (const auto& f : findings) {
      const json& key = get_or_null(f, "key");
      const json& fstate = get_or_null(f, "state");
      if (key.is_string() && key.get<std::string>() == "state" && fstate.is_string() &&
          in(fstate.get<std::string>(), {"degraded", "attention", "unavailable"})) {
        out += "\n\n  " + (f.contains("text") ? py_str(get_or_null(f, "text")) : std::string());
      }
    }
  }
  return out;
}

int status_exit_code(const json& runtime, const json& attention) {
  const json& state = get_or_null(runtime, "state");
  std::string s = state.is_string() ? state.get<std::string>() : std::string();
  if (s == "unavailable") return kExitUnreachable;
  if (s == "degraded" || s == "attention") return kExitAttention;
  if (attention.is_object() && truthy(get_or_null(attention, "total"))) return kExitAttention;
  // An inbox that could not be read is not an empty inbox (status.py).
  if (attention.is_null() || truthy(get_or_null(attention, "could_not_check"))) {
    return kExitAttention;
  }
  return kExitOk;
}

std::string compute_tier(std::optional<long> ram_mb, std::optional<long> cpus,
                         const std::string& platform) {
  if (platform == "esp32") return "micro";
  if (!ram_mb) return "low";
  if (*ram_mb >= 7000 && cpus.value_or(0) >= 4) return "high";
  if (*ram_mb >= 1800) return "medium";
  if (*ram_mb >= kMinCoreRamMb) return "low";
  return "micro";
}

bool can_be_core(const std::string& platform, std::optional<long> ram_mb,
                 const std::string& tier) {
  if (platform == "esp32" || tier == "micro") return false;
  if (platform == "ios") return false;
  if (ram_mb && *ram_mb < kMinCoreRamMb) return false;
  return true;
}

void Ld2450Framer::feed(const uint8_t* data, size_t len) {
  buf_.insert(buf_.end(), data, data + len);
}

std::optional<std::array<uint8_t, Ld2450Framer::kFrameLen>> Ld2450Framer::next() {
  static constexpr uint8_t kHeader[4] = {0xAA, 0xFF, 0x03, 0x00};
  auto it = std::search(buf_.begin(), buf_.end(), std::begin(kHeader), std::end(kHeader));
  if (it != buf_.end()) {
    size_t i = static_cast<size_t>(it - buf_.begin());
    if (buf_.size() >= i + kFrameLen) {
      std::array<uint8_t, kFrameLen> frame{};
      std::copy_n(buf_.begin() + static_cast<std::ptrdiff_t>(i), kFrameLen, frame.begin());
      buf_.erase(buf_.begin(), buf_.begin() + static_cast<std::ptrdiff_t>(i + kFrameLen));
      return frame;
    }
  }
  if (buf_.size() > kMaxBuffer) {
    buf_.erase(buf_.begin(), buf_.end() - static_cast<std::ptrdiff_t>(kKeepOnOverflow));
  }
  return std::nullopt;
}

NodeNext heartbeat_next_state(std::optional<int> http_status, const std::string& body) {
  if (!http_status) return NodeNext::Keep;                 // network blip: never reset
  if (*http_status == 401 || *http_status == 403) return NodeNext::Revoked;
  if (*http_status != 200) return NodeNext::Keep;
  json parsed = json::parse(body, nullptr, /*allow_exceptions=*/false);
  if (!parsed.is_object()) return NodeNext::Keep;
  const json& cmd = get_or_null(parsed, "command");
  if (!cmd.is_string()) return NodeNext::Keep;
  const std::string& c = cmd.get_ref<const std::string&>();
  if (c == "ok" || c == "run") return NodeNext::Active;    // `run`: legacy synonym
  if (c == "sleep") return NodeNext::Disabled;
  if (c == "revoked") return NodeNext::Revoked;
  return NodeNext::Keep;
}

}  // namespace wavr
