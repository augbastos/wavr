// JNI glue for dev.wavr.core.nativeui.WavrNative. Thin by rule: it calls the C
// ABI (include/wavr/wavr.h) and converts strings -- no Wavr logic lives here.
// See docs/NATIVE-CLIENT.md.
#include <jni.h>

#include <memory>
#include <string>
#include <vector>

#include "wavr/wavr.h"

namespace {

// UTF-16 -> standard UTF-8. GetStringUTFChars would give *modified* UTF-8, in
// which a character outside the BMP (an emoji in a room name inside a command's
// JSON) becomes two encoded surrogates -- not UTF-8, and the JSON would not parse.
std::string from_java(JNIEnv* env, jstring s) {
  if (!s) return {};
  const jsize n = env->GetStringLength(s);
  const jchar* u = env->GetStringChars(s, nullptr);
  if (!u) return {};
  std::string out;
  out.reserve(static_cast<size_t>(n));
  for (jsize i = 0; i < n; ++i) {
    uint32_t cp = u[i];
    if (cp >= 0xD800 && cp <= 0xDBFF && i + 1 < n && u[i + 1] >= 0xDC00 && u[i + 1] <= 0xDFFF) {
      cp = 0x10000 + ((cp - 0xD800) << 10) + (u[i + 1] - 0xDC00);
      ++i;
    } else if (cp >= 0xD800 && cp <= 0xDFFF) {
      cp = 0xFFFD;   // a lone surrogate is not a character
    }
    if (cp < 0x80) {
      out += static_cast<char>(cp);
    } else if (cp < 0x800) {
      out += static_cast<char>(0xC0 | (cp >> 6));
      out += static_cast<char>(0x80 | (cp & 0x3F));
    } else if (cp < 0x10000) {
      out += static_cast<char>(0xE0 | (cp >> 12));
      out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
      out += static_cast<char>(0x80 | (cp & 0x3F));
    } else {
      out += static_cast<char>(0xF0 | (cp >> 18));
      out += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
      out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
      out += static_cast<char>(0x80 | (cp & 0x3F));
    }
  }
  env->ReleaseStringChars(s, u);
  return out;
}

// Standard UTF-8 -> UTF-16. NewStringUTF wants *modified* UTF-8 and rejects a
// 4-byte sequence (an emoji in a room name) under CheckJNI; NewString does not.
jstring to_java(JNIEnv* env, const std::string& s) {
  std::vector<jchar> u;
  u.reserve(s.size());
  for (size_t i = 0; i < s.size();) {
    unsigned char c = static_cast<unsigned char>(s[i]);
    uint32_t cp;
    size_t n;
    if (c < 0x80) {
      cp = c, n = 1;
    } else if ((c >> 5) == 0x6 && i + 1 < s.size()) {
      cp = ((c & 0x1Fu) << 6) | (s[i + 1] & 0x3Fu), n = 2;
    } else if ((c >> 4) == 0xE && i + 2 < s.size()) {
      cp = ((c & 0x0Fu) << 12) | ((s[i + 1] & 0x3Fu) << 6) | (s[i + 2] & 0x3Fu), n = 3;
    } else if ((c >> 3) == 0x1E && i + 3 < s.size()) {
      cp = ((c & 0x07u) << 18) | ((s[i + 1] & 0x3Fu) << 12) | ((s[i + 2] & 0x3Fu) << 6) |
           (s[i + 3] & 0x3Fu),
      n = 4;
    } else {
      cp = 0xFFFD, n = 1;   // not UTF-8: the library never emits this, but never crash
    }
    if (cp >= 0x10000) {
      cp -= 0x10000;
      u.push_back(static_cast<jchar>(0xD800 + (cp >> 10)));
      u.push_back(static_cast<jchar>(0xDC00 + (cp & 0x3FF)));
    } else {
      u.push_back(static_cast<jchar>(cp));
    }
    i += n;
  }
  return env->NewString(u.data(), static_cast<jsize>(u.size()));
}

// Two-call sizing of a caller-owned buffer, as the ABI defines it.
template <typename F>
std::string read_json(F&& f) {
  int n = f(nullptr, 0);
  if (n <= 0) return {};
  std::string out(static_cast<size_t>(n) + 1, '\0');
  f(out.data(), out.size());
  out.resize(static_cast<size_t>(n));
  return out;
}

// A C++ exception (bad_alloc in a string copy) must not unwind through JNI:
// that aborts the app. Each export catches everything and answers "unknown".
template <typename F>
jstring guarded(JNIEnv* env, F&& f) noexcept {
  try {
    return f();
  } catch (...) {
    return env->NewStringUTF("");
  }
}

}  // namespace

extern "C" {

JNIEXPORT jboolean JNICALL Java_dev_wavr_core_nativeui_WavrNative_abiCompatible(
    JNIEnv*, jobject, jint major, jint minor) {
  return wavr_abi_compatible(major, minor) ? JNI_TRUE : JNI_FALSE;
}

JNIEXPORT jstring JNICALL Java_dev_wavr_core_nativeui_WavrNative_snapshotFetch(
    JNIEnv* env, jobject, jstring url, jstring credential, jstring pin, jint timeout_ms) {
  return guarded(env, [&] {
    const std::string u = from_java(env, url), c = from_java(env, credential),
                      p = from_java(env, pin);
    std::unique_ptr<wavr_snapshot, void (*)(wavr_snapshot*)> s(
        wavr_snapshot_fetch(u.c_str(), c.c_str(), p.c_str(), timeout_ms), wavr_snapshot_free);
    if (!s) return to_java(env, "");   // out of memory: the Kotlin parser reads "" as unknown
    return to_java(env, read_json([&](char* out, size_t len) {
                     return wavr_snapshot_json(s.get(), out, len);
                   }));
  });
}

JNIEXPORT jstring JNICALL Java_dev_wavr_core_nativeui_WavrNative_capabilityManifest(
    JNIEnv* env, jobject) {
  return guarded(env, [&] {
    return to_java(env, read_json([](char* out, size_t len) {
                     return wavr_capability_manifest(out, len);
                   }));
  });
}

JNIEXPORT jstring JNICALL Java_dev_wavr_core_nativeui_WavrNative_commandRun(
    JNIEnv* env, jobject, jstring url, jstring token, jstring pin, jstring name,
    jstring args_json, jint timeout_ms) {
  return guarded(env, [&] {
    const std::string u = from_java(env, url), t = from_java(env, token),
                      p = from_java(env, pin), n = from_java(env, name),
                      a = from_java(env, args_json);
    std::unique_ptr<wavr_reply, void (*)(wavr_reply*)> r(
        wavr_command_run(u.c_str(), t.c_str(), p.c_str(), n.c_str(), a.c_str(), timeout_ms),
        wavr_reply_free);
    if (!r) return to_java(env, "");
    return to_java(env, read_json([&](char* out, size_t len) {
                     return wavr_reply_json(r.get(), out, len);
                   }));
  });
}

JNIEXPORT jstring JNICALL Java_dev_wavr_core_nativeui_WavrNative_probe(
    JNIEnv* env, jobject, jstring url, jint timeout_ms) {
  return guarded(env, [&] {
    const std::string u = from_java(env, url);
    std::unique_ptr<wavr_reply, void (*)(wavr_reply*)> r(wavr_probe(u.c_str(), timeout_ms),
                                                         wavr_reply_free);
    if (!r) return to_java(env, "");
    return to_java(env, read_json([&](char* out, size_t len) {
                     return wavr_reply_json(r.get(), out, len);
                   }));
  });
}

}  // extern "C"
