// Fuzz targets for every boundary where the native runtime reads bytes it did
// not produce: a Core's HTTP answers, JSON bodies, the node state file, URLs,
// LD2450 serial bytes, and the C ABI itself. One binary per target, selected
// at compile time by WAVR_FUZZ_TARGET. Each target states the invariants it
// checks beyond "does not crash": sanitizers catch memory errors and UB; the
// asserts below catch wrong answers.
//
// Built with -fsanitize=fuzzer (clang) this is a libFuzzer target; otherwise
// fuzz/driver.cpp supplies main() and a seeded mutation loop.
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "net.h"
#include "node.h"
#include "semantics.h"
#include "wavr/wavr.h"

#ifndef WAVR_FUZZ_TARGET
#  error "define WAVR_FUZZ_TARGET"
#endif

#define WAVR_FUZZ_CHECK(cond)                                                     \
  do {                                                                            \
    if (!(cond)) {                                                                \
      std::fprintf(stderr, "invariant violated: %s (%s:%d)\n", #cond, __FILE__,   \
                   __LINE__);                                                     \
      std::abort();                                                               \
    }                                                                             \
  } while (0)

namespace {

// Split the input on NUL bytes into at most `n` parts (missing parts = absent).
std::vector<std::string> split(const uint8_t* d, size_t size, size_t n) {
  std::vector<std::string> parts(1);
  for (size_t i = 0; i < size; ++i) {
    if (d[i] == 0 && parts.size() < n) {
      parts.emplace_back();
    } else {
      parts.back().push_back(static_cast<char>(d[i]));
    }
  }
  return parts;
}

const char* part(const std::vector<std::string>& p, size_t i) {
  return i < p.size() ? p[i].c_str() : nullptr;
}

int fuzz_http_response(const uint8_t* d, size_t size) {
  wavr::net::Result r;
  std::string raw(reinterpret_cast<const char*>(d), size);
  if (wavr::net::parse_response(raw, &r)) {
    WAVR_FUZZ_CHECK(r.ok);
    WAVR_FUZZ_CHECK(r.body.size() <= raw.size());   // decoding never invents bytes
  }
  return 0;
}

int fuzz_snapshot(const uint8_t* d, size_t size) {
  auto p = split(d, size, 5);
  const int reachable = p.size() > 3 && !p[3].empty() ? (p[3][0] & 1) : 1;
  wavr_snapshot* s = wavr_snapshot_from_answers(part(p, 0), part(p, 1), part(p, 2), reachable,
                                                part(p, 4));
  WAVR_FUZZ_CHECK(s != nullptr);
  const int code = wavr_snapshot_exit_code(s);
  WAVR_FUZZ_CHECK(code >= 0 && code <= 2);
  int n = wavr_snapshot_json(s, nullptr, 0);
  WAVR_FUZZ_CHECK(n > 0);
  std::string out(static_cast<size_t>(n) + 1, '\0');
  WAVR_FUZZ_CHECK(wavr_snapshot_json(s, out.data(), out.size()) == n);
  out.resize(static_cast<size_t>(n));
  wavr_snapshot_free(s);
  // The output is always valid JSON of the fixed schema -- whatever came in.
  wavr::json j = wavr::json::parse(out, nullptr, false);
  WAVR_FUZZ_CHECK(j.is_object() && j["schema"] == 1 && j["exit_code"] == code);
  WAVR_FUZZ_CHECK(j["rooms"].is_array());
  for (const auto& room : j["rooms"]) {
    // Positions, identities and vitals can never leak through the view model.
    WAVR_FUZZ_CHECK(!room.contains("targets") && !room.contains("identities") &&
                    !room.contains("vitals"));
    WAVR_FUZZ_CHECK(room.size() == 10);
  }
  if (!reachable) WAVR_FUZZ_CHECK(j["reachable"] == false && code == 2);
  return 0;
}

int fuzz_status(const uint8_t* d, size_t size) {
  auto p = split(d, size, 2);
  const int code = wavr_status_exit_code(part(p, 0), part(p, 1));
  WAVR_FUZZ_CHECK(code == WAVR_ERR_PARSE || (code >= 0 && code <= 2));
  char buf[512];
  for (int ascii = 0; ascii <= 1; ++ascii) {
    int n = wavr_status_render(part(p, 0), part(p, 1), ascii, buf, sizeof buf);
    WAVR_FUZZ_CHECK(n == WAVR_ERR_PARSE || n >= 0);
    // Always terminated inside the buffer (a "\u0000" in the text may end it early).
    if (n >= 0) WAVR_FUZZ_CHECK(std::strlen(buf) < sizeof buf);
  }
  return 0;
}

int fuzz_framer(const uint8_t* d, size_t size) {
  wavr_framer* f = wavr_framer_new();
  WAVR_FUZZ_CHECK(f != nullptr);
  uint8_t frame[30];
  size_t i = 0;
  while (i < size) {
    size_t chunk = 1 + (d[i] % 97);   // the input also chooses how it is chunked
    if (chunk > size - i) chunk = size - i;
    WAVR_FUZZ_CHECK(wavr_framer_feed(f, d + i, chunk) == WAVR_OK);
    i += chunk;
    while (wavr_framer_next(f, frame) == 1) {
      // A frame always starts at a header. The tail is NOT checked here: the
      // canonical take_ld2450_frame leaves validity to parse_ld2450_frame (the
      // Core's job). An earlier version of this target asserted it and was wrong.
      WAVR_FUZZ_CHECK(frame[0] == 0xAA && frame[1] == 0xFF && frame[2] == 0x03 && frame[3] == 0x00);
    }
  }
  wavr_framer_free(f);
  // The direct class: the buffer stays bounded however the bytes arrive.
  wavr::Ld2450Framer direct;
  direct.feed(d, size);
  while (direct.next()) {
  }
  WAVR_FUZZ_CHECK(direct.buffered().size() <= wavr::Ld2450Framer::kMaxBuffer);
  return 0;
}

int fuzz_node_state(const uint8_t* d, size_t size) {
  wavr::NodeState s;
  if (wavr::parse_node_state(std::string(reinterpret_cast<const char*>(d), size), &s)) {
    WAVR_FUZZ_CHECK(!s.url.empty() && !s.token.empty());
  }
  return 0;
}

int fuzz_url(const uint8_t* d, size_t size) {
  std::string text(reinterpret_cast<const char*>(d), size);
  auto u = wavr::net::parse_url(text);
  if (u) {
    WAVR_FUZZ_CHECK(u->port > 0 && u->port <= 65535 && !u->host.empty());
    for (unsigned char c : u->host) WAVR_FUZZ_CHECK(c > 0x20 && c != 0x7f && c != '@');
    // Loopback is decided by parsing; a name is loopback only if it IS localhost.
    if (wavr::net::is_loopback_host(u->host)) {
      std::string h;
      for (char c : u->host) h.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(c))));
      WAVR_FUZZ_CHECK(h == "localhost" || h.find_first_not_of("0123456789.:abcdef") == std::string::npos);
    }
  }
  return 0;
}

int fuzz_heartbeat(const uint8_t* d, size_t size) {
  int status = size >= 2 ? static_cast<int>((d[0] << 8) | d[1]) - 1 : -1;   // -1 = no response
  std::string body = size > 2 ? std::string(reinterpret_cast<const char*>(d + 2), size - 2) : "";
  int next = wavr_heartbeat_next_state(status, body.c_str());
  WAVR_FUZZ_CHECK(next >= WAVR_NODE_KEEP && next <= WAVR_NODE_REVOKED);
  if (status < 0) WAVR_FUZZ_CHECK(next == WAVR_NODE_KEEP);   // a network error never revokes
  return 0;
}

}  // namespace

extern "C" int LLVMFuzzerTestOneInput(const uint8_t* d, size_t size) {
  return WAVR_FUZZ_TARGET(d, size);
}
