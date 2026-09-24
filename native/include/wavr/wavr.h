/*
 * wavr.h -- the stable C ABI of Wavr's native runtime.
 *
 * The one boundary every non-C++ caller uses: JNI on Android, Swift on Apple
 * platforms, P/Invoke or C++/WinRT on Windows, ctypes from Python. Deliberately
 * narrow and dull:
 *
 *   - plain C types only, no C++ across the boundary;
 *   - every output is written into a caller-owned buffer of stated size, and the
 *     return value says how many bytes were needed (so a caller can retry with a
 *     bigger buffer) -- nothing is allocated for the caller to free;
 *   - the node runtime is an opaque handle with create/step/destroy;
 *   - WAVR_ABI_VERSION changes whenever any signature or struct below changes,
 *     and callers must check wavr_abi_version() before use.
 *
 * Semantics are Wavr's, not this library's: each function below implements a
 * behaviour whose canonical definition is Python code in backend/wavr/, and
 * the JSON files in conformance/ (generated from that code) are the answer key this library
 * is tested against. Where the two ever disagree, this library is wrong.
 *
 * AGPL-3.0-or-later.
 */
#ifndef WAVR_H
#define WAVR_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#if defined(_WIN32) && defined(WAVR_BUILDING_SHARED)
#  define WAVR_API __declspec(dllexport)
#elif defined(_WIN32) && defined(WAVR_USING_SHARED)
#  define WAVR_API __declspec(dllimport)
#elif defined(__GNUC__)
#  define WAVR_API __attribute__((visibility("default")))
#else
#  define WAVR_API
#endif

#define WAVR_ABI_VERSION 1

/* Status codes returned by functions that can fail. */
#define WAVR_OK 0
#define WAVR_ERR_ARGUMENT (-1)   /* a NULL or out-of-range argument */
#define WAVR_ERR_PARSE (-2)      /* input was not the JSON it had to be */
#define WAVR_ERR_IO (-3)         /* a file or network operation failed */

WAVR_API int wavr_abi_version(void);

/* The library's own version string, e.g. "0.1.0". Static storage. */
WAVR_API const char* wavr_version(void);

/*
 * `wavr status` semantics (backend/wavr/status.py).
 *
 * runtime_json / attention_json: the bodies of GET /api/runtime and
 * GET /api/attention. attention_json may be NULL ("could not be read").
 * Returns 0 healthy, 1 needs attention, 2 not responding; WAVR_ERR_PARSE when
 * runtime_json is not a JSON object.
 */
WAVR_API int wavr_status_exit_code(const char* runtime_json, const char* attention_json);

/*
 * The human rendering of the same answer, UTF-8, into `out` (NUL-terminated
 * when it fits). ascii != 0 selects the mark set a narrow stream can print.
 * Returns the number of bytes needed EXCLUDING the NUL, or a WAVR_ERR_* code.
 */
WAVR_API int wavr_status_render(const char* runtime_json, const char* attention_json,
                                int ascii, char* out, size_t out_len);

/*
 * Hardware tier (backend/wavr/capabilities.py::_compute_tier). ram_mb / cpu_count
 * < 0 mean unknown. Writes "micro" | "low" | "medium" | "high" into out.
 * Returns bytes needed excluding NUL, or WAVR_ERR_ARGUMENT.
 */
WAVR_API int wavr_compute_tier(long ram_mb, long cpu_count, const char* platform,
                               char* out, size_t out_len);

/*
 * This host's capability manifest as JSON (docs/WAVR-PROTOCOL.md section 5):
 * probed, never guessed -- a key that could not be determined is absent
 * (unknown), never false. Returns bytes needed excluding NUL.
 */
WAVR_API int wavr_capability_manifest(char* out, size_t out_len);

/*
 * LD2450 serial framing (backend/wavr/sources/mmwave.py::take_ld2450_frame).
 * A framer holds the partial stream between calls.
 */
typedef struct wavr_framer wavr_framer;
WAVR_API wavr_framer* wavr_framer_new(void);
WAVR_API void wavr_framer_free(wavr_framer* f);
/* Append received bytes. Returns WAVR_OK. */
WAVR_API int wavr_framer_feed(wavr_framer* f, const uint8_t* data, size_t len);
/* Copy the next complete 30-byte frame into out[30]. Returns 1 when a frame was
 * produced, 0 when more bytes are needed. */
WAVR_API int wavr_framer_next(wavr_framer* f, uint8_t out[30]);

/*
 * Node heartbeat interpretation (firmware/NODE_PROTOCOL.md). http_status < 0
 * means no HTTP response at all (network error). Returns one of the
 * WAVR_NODE_* values: what the node's state must become.
 */
#define WAVR_NODE_KEEP 0
#define WAVR_NODE_ACTIVE 1
#define WAVR_NODE_DISABLED 2
#define WAVR_NODE_REVOKED 3
WAVR_API int wavr_heartbeat_next_state(int http_status, const char* body);

/* SHA-256 fingerprint text of a certificate digest, as backend/wavr/tls.py
 * formats it ("AB:CD:..."). out must hold 96 bytes. */
WAVR_API int wavr_fingerprint_format(const uint8_t digest[32], char* out, size_t out_len);

#ifdef __cplusplus
}
#endif

#endif /* WAVR_H */
