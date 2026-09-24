// The C ABI (include/wavr/wavr.h) over the C++ semantics. Nothing here decides
// anything: each function parses, delegates, and copies out.
#include "wavr/wavr.h"

#include <cstring>
#include <new>
#include <string>

#include "capabilities.h"
#include "fingerprint_fmt.h"
#include "semantics.h"

#ifndef WAVR_NATIVE_VERSION
#  define WAVR_NATIVE_VERSION "0.0.0"
#endif

namespace {

int copy_out(const std::string& s, char* out, size_t out_len) {
  if (out && out_len > 0) {
    size_t n = s.size() < out_len - 1 ? s.size() : out_len - 1;
    std::memcpy(out, s.data(), n);
    out[n] = '\0';
  }
  return static_cast<int>(s.size());
}

bool parse(const char* text, wavr::json* out) {
  if (!text) {
    *out = nullptr;
    return true;
  }
  *out = wavr::json::parse(text, nullptr, false);
  return !out->is_discarded();
}

}  // namespace

struct wavr_framer {
  wavr::Ld2450Framer impl;
};

extern "C" {

int wavr_abi_version(void) { return WAVR_ABI_VERSION; }

const char* wavr_version(void) { return WAVR_NATIVE_VERSION; }

int wavr_status_exit_code(const char* runtime_json, const char* attention_json) {
  wavr::json runtime, attention;
  if (!runtime_json || !parse(runtime_json, &runtime) || !runtime.is_object() ||
      !parse(attention_json, &attention)) {
    return WAVR_ERR_PARSE;
  }
  return wavr::status_exit_code(runtime, attention);
}

int wavr_status_render(const char* runtime_json, const char* attention_json, int ascii,
                       char* out, size_t out_len) {
  wavr::json runtime, attention;
  if (!runtime_json || !parse(runtime_json, &runtime) || !runtime.is_object() ||
      !parse(attention_json, &attention)) {
    return WAVR_ERR_PARSE;
  }
  return copy_out(wavr::render_status(runtime, attention, ascii != 0), out, out_len);
}

int wavr_compute_tier(long ram_mb, long cpu_count, const char* platform, char* out,
                      size_t out_len) {
  if (!platform) return WAVR_ERR_ARGUMENT;
  auto ram = ram_mb < 0 ? std::nullopt : std::optional<long>(ram_mb);
  auto cpus = cpu_count < 0 ? std::nullopt : std::optional<long>(cpu_count);
  return copy_out(wavr::compute_tier(ram, cpus, platform), out, out_len);
}

int wavr_capability_manifest(char* out, size_t out_len) {
  return copy_out(wavr::probe_manifest().dump(), out, out_len);
}

wavr_framer* wavr_framer_new(void) { return new (std::nothrow) wavr_framer(); }

void wavr_framer_free(wavr_framer* f) { delete f; }

int wavr_framer_feed(wavr_framer* f, const uint8_t* data, size_t len) {
  if (!f || (!data && len)) return WAVR_ERR_ARGUMENT;
  f->impl.feed(data, len);
  return WAVR_OK;
}

int wavr_framer_next(wavr_framer* f, uint8_t out[30]) {
  if (!f || !out) return WAVR_ERR_ARGUMENT;
  auto frame = f->impl.next();
  if (!frame) return 0;
  std::memcpy(out, frame->data(), frame->size());
  return 1;
}

int wavr_heartbeat_next_state(int http_status, const char* body) {
  auto status = http_status < 0 ? std::nullopt : std::optional<int>(http_status);
  return static_cast<int>(wavr::heartbeat_next_state(status, body ? body : ""));
}

int wavr_fingerprint_format(const uint8_t digest[32], char* out, size_t out_len) {
  if (!digest || !out || out_len < kFingerprintHexBufLen) return WAVR_ERR_ARGUMENT;
  formatFingerprintHex(digest, out);
  return static_cast<int>(std::strlen(out));
}

}  // extern "C"
