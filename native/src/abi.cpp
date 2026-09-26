// The C ABI (include/wavr/wavr.h) over the C++ semantics. Nothing here decides
// anything: each function parses, delegates, and copies out -- inside a guard,
// because a C++ exception that reaches a C, JNI or Swift caller terminates it.
#include "wavr/wavr.h"

#include <climits>
#include <cstring>
#include <memory>
#include <new>
#include <string>

#include "capabilities.h"
#include "client_commands.h"
#include "client_view.h"
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
  return s.size() > static_cast<size_t>(INT_MAX) ? WAVR_ERR_INTERNAL
                                                  : static_cast<int>(s.size());
}

bool parse(const char* text, wavr::json* out) {
  if (!text) {
    *out = nullptr;
    return true;
  }
  *out = wavr::json::parse(text, nullptr, false);
  return !out->is_discarded();
}

// Runs `f`; any exception becomes `on_error`. The only exceptions left in
// this code are allocation failure and library invariants -- both "internal".
template <typename T, typename F>
T guarded(T on_error, F&& f) noexcept {
  try {
    return f();
  } catch (...) {
    return on_error;
  }
}

}  // namespace

struct wavr_framer {
  wavr::Ld2450Framer impl;
};

struct wavr_snapshot {
  wavr::json doc;
  std::string text;   // serialised once, at construction
  int exit_code = WAVR_ERR_INTERNAL;
};

struct wavr_reply {
  std::string text;   // serialised once, at construction
  int ok = 0;
};

namespace {

wavr_snapshot* make_snapshot(wavr::json doc) {
  auto s = std::make_unique<wavr_snapshot>();   // freed if anything below throws
  s->text = wavr::dump(doc);
  s->exit_code = doc["exit_code"].get<int>();
  s->doc = std::move(doc);
  return s.release();
}

wavr_reply* make_reply(const wavr::json& doc) {
  auto r = std::make_unique<wavr_reply>();
  r->text = wavr::dump(doc);
  r->ok = doc.value("ok", false) ? 1 : 0;
  return r.release();
}

}  // namespace

extern "C" {

int wavr_abi_version(void) { return WAVR_ABI_VERSION_MAJOR; }

int wavr_abi_version_minor(void) { return WAVR_ABI_VERSION_MINOR; }

int wavr_abi_compatible(int major, int minor) {
  return major == WAVR_ABI_VERSION_MAJOR && minor >= 0 && minor <= WAVR_ABI_VERSION_MINOR;
}

const char* wavr_error_string(int code) {
  switch (code) {
    case WAVR_OK: return "ok";
    case WAVR_ERR_ARGUMENT: return "a required argument was NULL or out of range";
    case WAVR_ERR_PARSE: return "the input was not the JSON it had to be";
    case WAVR_ERR_IO: return "a file or network operation failed";
    case WAVR_ERR_INTERNAL: return "an unexpected failure inside the Wavr library";
    default: return "unknown Wavr error code";
  }
}

const char* wavr_version(void) { return WAVR_NATIVE_VERSION; }

int wavr_status_exit_code(const char* runtime_json, const char* attention_json) {
  return guarded(WAVR_ERR_INTERNAL, [&] {
    wavr::json runtime, attention;
    if (!runtime_json || !parse(runtime_json, &runtime) || !runtime.is_object() ||
        !parse(attention_json, &attention)) {
      return WAVR_ERR_PARSE;
    }
    return wavr::status_exit_code(runtime, attention);
  });
}

int wavr_status_render(const char* runtime_json, const char* attention_json, int ascii,
                       char* out, size_t out_len) {
  return guarded(WAVR_ERR_INTERNAL, [&] {
    wavr::json runtime, attention;
    if (!runtime_json || !parse(runtime_json, &runtime) || !runtime.is_object() ||
        !parse(attention_json, &attention)) {
      return WAVR_ERR_PARSE;
    }
    return copy_out(wavr::render_status(runtime, attention, ascii != 0), out, out_len);
  });
}

int wavr_compute_tier(long ram_mb, long cpu_count, const char* platform, char* out,
                      size_t out_len) {
  return guarded(WAVR_ERR_INTERNAL, [&] {
    if (!platform) return WAVR_ERR_ARGUMENT;
    auto ram = ram_mb < 0 ? std::nullopt : std::optional<long>(ram_mb);
    auto cpus = cpu_count < 0 ? std::nullopt : std::optional<long>(cpu_count);
    return copy_out(wavr::compute_tier(ram, cpus, platform), out, out_len);
  });
}

int wavr_capability_manifest(char* out, size_t out_len) {
  return guarded(WAVR_ERR_INTERNAL,
                 [&] { return copy_out(wavr::dump(wavr::probe_manifest()), out, out_len); });
}

wavr_framer* wavr_framer_new(void) { return new (std::nothrow) wavr_framer(); }

void wavr_framer_free(wavr_framer* f) { delete f; }

int wavr_framer_feed(wavr_framer* f, const uint8_t* data, size_t len) {
  return guarded(WAVR_ERR_INTERNAL, [&] {
    if (!f || (!data && len)) return WAVR_ERR_ARGUMENT;
    f->impl.feed(data, len);
    return WAVR_OK;
  });
}

int wavr_framer_next(wavr_framer* f, uint8_t out[30]) {
  return guarded(WAVR_ERR_INTERNAL, [&] {
    if (!f || !out) return WAVR_ERR_ARGUMENT;
    auto frame = f->impl.next();
    if (!frame) return 0;
    std::memcpy(out, frame->data(), frame->size());
    return 1;
  });
}

int wavr_heartbeat_next_state(int http_status, const char* body) {
  return guarded(WAVR_ERR_INTERNAL, [&] {
    auto status = http_status < 0 ? std::nullopt : std::optional<int>(http_status);
    return static_cast<int>(wavr::heartbeat_next_state(status, body ? body : ""));
  });
}

int wavr_fingerprint_format(const uint8_t digest[32], char* out, size_t out_len) {
  if (!digest || !out || out_len < kFingerprintHexBufLen) return WAVR_ERR_ARGUMENT;
  formatFingerprintHex(digest, out);
  return static_cast<int>(std::strlen(out));
}

wavr_snapshot* wavr_snapshot_fetch(const char* url, const char* token, const char* pin,
                                   int timeout_ms) {
  return guarded<wavr_snapshot*>(nullptr, [&]() -> wavr_snapshot* {
    if (!url) return nullptr;
    return make_snapshot(wavr::client_fetch(url, token ? token : "", pin ? pin : "",
                                            timeout_ms > 0 ? timeout_ms : 6000));
  });
}

wavr_snapshot* wavr_snapshot_from_answers(const char* runtime_json, const char* attention_json,
                                          const char* state_json, int reachable,
                                          const char* error) {
  return guarded<wavr_snapshot*>(nullptr, [&]() -> wavr_snapshot* {
    // Unparseable bodies are "not read" (null), exactly as a fetch treats them.
    auto body = [](const char* t) {
      wavr::json j;
      return parse(t, &j) ? j : wavr::json(nullptr);
    };
    return make_snapshot(wavr::client_snapshot(body(runtime_json), body(attention_json),
                                               body(state_json), reachable != 0,
                                               error ? error : ""));
  });
}

int wavr_snapshot_json(const wavr_snapshot* s, char* out, size_t out_len) {
  if (!s) return WAVR_ERR_ARGUMENT;
  return copy_out(s->text, out, out_len);   // no allocation: cannot throw
}

int wavr_snapshot_exit_code(const wavr_snapshot* s) {
  return s ? s->exit_code : WAVR_ERR_ARGUMENT;
}

void wavr_snapshot_free(wavr_snapshot* s) { delete s; }

int wavr_command_table(char* out, size_t out_len) {
  return guarded(WAVR_ERR_INTERNAL,
                 [&] { return copy_out(wavr::dump(wavr::command_table()), out, out_len); });
}

wavr_reply* wavr_command_run(const char* url, const char* token, const char* pin,
                             const char* name, const char* args_json, int timeout_ms) {
  return guarded<wavr_reply*>(nullptr, [&]() -> wavr_reply* {
    if (!url || !name) return nullptr;
    return make_reply(wavr::command_run(url, token ? token : "", pin ? pin : "", name,
                                        args_json ? args_json : "", timeout_ms));
  });
}

wavr_reply* wavr_probe(const char* url, int timeout_ms) {
  return guarded<wavr_reply*>(nullptr, [&]() -> wavr_reply* {
    if (!url) return nullptr;
    return make_reply(wavr::probe_core(url, timeout_ms));
  });
}

int wavr_reply_json(const wavr_reply* r, char* out, size_t out_len) {
  if (!r) return WAVR_ERR_ARGUMENT;
  return copy_out(r->text, out, out_len);
}

int wavr_reply_ok(const wavr_reply* r) { return r ? r->ok : WAVR_ERR_ARGUMENT; }

void wavr_reply_free(wavr_reply* r) { delete r; }

}  // extern "C"
