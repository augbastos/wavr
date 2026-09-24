// The native runtime against conformance/*.json -- the answer key generated
// from the Python that owns each behaviour (scripts/gen_conformance.py). Every
// case goes through the C ABI where one exists, so the boundary other
// languages call is the thing under test, not just the C++ behind it.
#include <array>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "capabilities.h"
#include "net.h"
#include "semantics.h"
#include "wavr/wavr.h"

#ifndef WAVR_CONFORMANCE_DIR
#  error "WAVR_CONFORMANCE_DIR must point at conformance/"
#endif

namespace {

using json = nlohmann::json;
int g_failed = 0, g_passed = 0;
std::string g_dir = WAVR_CONFORMANCE_DIR;   // argv[1] overrides: a device has its own path

void check(bool ok, const std::string& what) {
  if (ok) {
    ++g_passed;
  } else {
    ++g_failed;
    std::cerr << "FAIL: " << what << "\n";
  }
}

json load(const char* name) {
  std::ifstream f(g_dir + "/" + name);
  if (!f) {
    std::cerr << "cannot read conformance/" << name << "\n";
    std::exit(2);
  }
  return json::parse(f);
}

std::vector<uint8_t> unhex(const std::string& h) {
  std::vector<uint8_t> out;
  for (size_t i = 0; i + 1 < h.size(); i += 2) {
    out.push_back(static_cast<uint8_t>(std::stoi(h.substr(i, 2), nullptr, 16)));
  }
  return out;
}

std::string hex(const uint8_t* p, size_t n) {
  static const char* k = "0123456789abcdef";
  std::string s;
  for (size_t i = 0; i < n; ++i) {
    s += k[p[i] >> 4];
    s += k[p[i] & 15];
  }
  return s;
}

void status() {
  json fx = load("status.json");
  check(fx["exit_codes"]["ok"] == 0 && fx["exit_codes"]["attention"] == 1 &&
            fx["exit_codes"]["unreachable"] == 2,
        "status exit-code constants");
  for (const auto& c : fx["cases"]) {
    const std::string name = c["name"];
    std::string runtime = c["runtime"].dump();
    std::string attention = c["attention"].dump();
    const char* att = c["attention"].is_null() ? nullptr : attention.c_str();
    check(wavr_status_exit_code(runtime.c_str(), att) == c["exit_code"].get<int>(),
          "status exit code: " + name);
    for (int ascii = 0; ascii <= 1; ++ascii) {
      std::vector<char> buf(8192);
      int n = wavr_status_render(runtime.c_str(), att, ascii, buf.data(), buf.size());
      std::string want = c[ascii ? "text_ascii" : "text_unicode"];
      bool ok = n == static_cast<int>(want.size()) && want == buf.data();
      if (!ok) std::cerr << "--- want\n" << want << "\n--- got\n" << buf.data() << "\n";
      check(ok, std::string("status render (") + (ascii ? "ascii" : "unicode") + "): " + name);
    }
  }
}

void tiers() {
  json fx = load("compute_tier.json");
  check(fx["min_core_ram_mb"] == wavr::kMinCoreRamMb, "MIN_CORE_RAM_MB");
  for (const auto& c : fx["cases"]) {
    long ram = c["ram_mb"].is_null() ? -1 : c["ram_mb"].get<long>();
    long cpus = c["cpu_count"].is_null() ? -1 : c["cpu_count"].get<long>();
    std::string plat = c["platform"];
    char out[16];
    wavr_compute_tier(ram, cpus, plat.c_str(), out, sizeof out);
    std::string label = plat + "/" + std::to_string(ram) + "/" + std::to_string(cpus);
    check(c["compute_tier"] == out, "compute tier " + label);
    auto r = ram < 0 ? std::nullopt : std::optional<long>(ram);
    check(wavr::can_be_core(plat, r, out) == c["can_be_core"].get<bool>(), "can be core " + label);
  }
}

void framing() {
  json fx = load("ld2450_framing.json");
  check(fx["max_buffer"] == wavr::Ld2450Framer::kMaxBuffer &&
            fx["keep_on_overflow"] == wavr::Ld2450Framer::kKeepOnOverflow,
        "framer bounds");
  for (const auto& c : fx["cases"]) {
    auto bytes = unhex(c["stream_hex"]);
    wavr_framer* f = wavr_framer_new();
    wavr_framer_feed(f, bytes.data(), bytes.size());
    std::vector<std::string> got;
    uint8_t frame[30];
    while (wavr_framer_next(f, frame) == 1) got.push_back(hex(frame, 30));
    std::vector<std::string> want = c["frames_hex"];
    check(got == want, "ld2450 frames: " + c["name"].get<std::string>());
    wavr::Ld2450Framer direct;
    direct.feed(bytes.data(), bytes.size());
    while (direct.next()) {
    }
    check(hex(direct.buffered().data(), direct.buffered().size()) == c["leftover_hex"],
          "ld2450 leftover: " + c["name"].get<std::string>());
    wavr_framer_free(f);
  }
}

void heartbeat() {
  json fx = load("heartbeat.json");
  const char* names[] = {"keep", "active", "disabled", "revoked"};
  for (const auto& c : fx["cases"]) {
    int status = c["http_status"].is_null() ? -1 : c["http_status"].get<int>();
    std::string body = c["body"].is_string() ? c["body"].get<std::string>() : c["body"].dump();
    int got = wavr_heartbeat_next_state(status, c["body"].is_null() ? nullptr : body.c_str());
    check(names[got] == c["next_state"].get<std::string>(),
          "heartbeat: " + c["name"].get<std::string>());
  }
}

void manifest() {
  json vocab = load("vocabulary.json");
  std::vector<char> buf(8192);
  int n = wavr_capability_manifest(buf.data(), buf.size());
  check(n > 0 && n < static_cast<int>(buf.size()), "manifest fits");
  json m = json::parse(buf.data(), nullptr, false);
  check(m.is_object(), "manifest is JSON");
  auto has = [](const json& list, const json& v) {
    for (const auto& x : list) {
      if (x == v) return true;
    }
    return false;
  };
  check(has(vocab["platforms"], m["platform"]), "manifest platform is known");
  check(has(vocab["compute_tiers"], m["compute_tier"]), "manifest tier is known");
  for (const auto& f : m["functions_supported"]) {
    check(has(vocab["device_functions"], f), "manifest function " + f.dump());
  }
  for (const auto& [k, v] : m["capabilities"].items()) {
    check(has(vocab["capability_keys"], k), "capability key " + k);
    // The honesty rule: a probed key is a real boolean; unknown is ABSENT.
    check(v.is_boolean(), "capability " + k + " is tristate by absence");
  }
  check(m["protocol_version"] == 1, "manifest protocol version");
}

void loopback() {
  json fx = load("loopback.json");
  for (const auto& c : fx["cases"]) {
    const std::string url = c["url"];
    auto u = wavr::net::parse_url(url);
    bool got = u && wavr::net::is_loopback_host(u->host);
    check(got == c["loopback"].get<bool>(), "loopback: " + url);
  }
}

void fingerprint() {
  // SHA-256 of the empty string, formatted as backend/wavr/tls.py does for an
  // empty DER (the same vector the firmware's native test uses).
  const uint8_t d[32] = {0xe3, 0xb0, 0xc4, 0x42, 0x98, 0xfc, 0x1c, 0x14, 0x9a, 0xfb, 0xf4,
                         0xc8, 0x99, 0x6f, 0xb9, 0x24, 0x27, 0xae, 0x41, 0xe4, 0x64, 0x9b,
                         0x93, 0x4c, 0xa4, 0x95, 0x99, 0x1b, 0x78, 0x52, 0xb8, 0x55};
  char out[96];
  wavr_fingerprint_format(d, out, sizeof out);
  check(std::string(out) ==
            "E3:B0:C4:42:98:FC:1C:14:9A:FB:F4:C8:99:6F:B9:24:"
            "27:AE:41:E4:64:9B:93:4C:A4:95:99:1B:78:52:B8:55",
        "fingerprint format");
}

}  // namespace

int main(int argc, char** argv) {
  if (argc > 1) g_dir = argv[1];
  check(wavr_abi_version() == WAVR_ABI_VERSION, "ABI version");
  status();
  tiers();
  framing();
  heartbeat();
  loopback();
  manifest();
  fingerprint();
  // Native-only probe helper (Python asks os.cpu_count(); this reads what it reads).
  check(wavr::cpu_list_count("0-7") == 8 && wavr::cpu_list_count("0-3,6") == 5 &&
            wavr::cpu_list_count("2\n") == 1 && wavr::cpu_list_count("") == 0 &&
            wavr::cpu_list_count("3-1") == 0 && wavr::cpu_list_count("x") == 0,
        "cpu list parsing");
  std::cout << (g_failed ? "FAILED" : "passed") << ": " << g_passed << " checks passed, "
            << g_failed << " failed\n";
  return g_failed ? 1 : 0;
}
