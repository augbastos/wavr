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
 *   - stateful things are opaque handles with an explicit create/free pair;
 *     a handle is used by one thread at a time; every other function is
 *     reentrant and may be called from any thread;
 *   - no C++ exception crosses this boundary: an unexpected failure inside is
 *     WAVR_ERR_INTERNAL (or NULL from a constructor), never a terminated caller;
 *   - versioned as MAJOR.MINOR. MINOR grows when functions are ADDED; MAJOR
 *     changes when any existing signature, struct or meaning changes. A caller
 *     checks wavr_abi_compatible(WAVR_ABI_VERSION_MAJOR, WAVR_ABI_VERSION_MINOR)
 *     once, before anything else.
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

#define WAVR_ABI_VERSION_MAJOR 1
#define WAVR_ABI_VERSION_MINOR 2
/* Kept for callers built against 1.0, which compared this to wavr_abi_version(). */
#define WAVR_ABI_VERSION WAVR_ABI_VERSION_MAJOR

/* Status codes returned by functions that can fail. */
#define WAVR_OK 0
#define WAVR_ERR_ARGUMENT (-1)   /* a NULL or out-of-range argument */
#define WAVR_ERR_PARSE (-2)      /* input was not the JSON it had to be */
#define WAVR_ERR_IO (-3)         /* a file or network operation failed */
#define WAVR_ERR_INTERNAL (-4)   /* an unexpected failure inside the library (e.g. memory) */

/* The library's ABI major version (1.0 callers: compare with WAVR_ABI_VERSION). */
WAVR_API int wavr_abi_version(void);
WAVR_API int wavr_abi_version_minor(void);
/* 1 when this library serves a caller built against major.minor: same MAJOR,
 * and this library's MINOR >= the caller's. 0 otherwise -- do not proceed. */
WAVR_API int wavr_abi_compatible(int major, int minor);
/* A short English sentence for a WAVR_ERR_* code. Static storage. */
WAVR_API const char* wavr_error_string(int code);

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

/*
 * Client snapshot (ABI 1.1): everything a native screen shows, in one JSON
 * document -- backend/wavr/client_view.py, schema documented in
 * docs/NATIVE-CLIENT.md. It projects the Core's answers; it never decides.
 *
 * wavr_snapshot_fetch asks the Core at `url` (token / pin may be NULL or "").
 * An unpinned request is allowed to a loopback Core only. It never returns
 * NULL for a Core that did not answer: that is a snapshot with reachable=false
 * and exit_code 2. NULL means only that memory ran out, or `url` was NULL.
 * Blocking; timeout_ms applies to each of its three requests (<= 0: 6000).
 *
 * wavr_snapshot_from_answers builds one from bodies the caller already has
 * (NULL = not read). reachable = 0 means the Core did not answer.
 */
typedef struct wavr_snapshot wavr_snapshot;
WAVR_API wavr_snapshot* wavr_snapshot_fetch(const char* url, const char* token,
                                            const char* pin, int timeout_ms);
WAVR_API wavr_snapshot* wavr_snapshot_from_answers(const char* runtime_json,
                                                   const char* attention_json,
                                                   const char* state_json, int reachable,
                                                   const char* error);
/* The snapshot as JSON. Returns bytes needed excluding the NUL (call with
 * out = NULL, out_len = 0 to size a buffer), or WAVR_ERR_ARGUMENT. */
WAVR_API int wavr_snapshot_json(const wavr_snapshot* s, char* out, size_t out_len);
/* The `wavr status` exit code of this snapshot: 0, 1 or 2. */
WAVR_API int wavr_snapshot_exit_code(const wavr_snapshot* s);
WAVR_API void wavr_snapshot_free(wavr_snapshot* s);

/*
 * Commands and replies (ABI 1.2).
 *
 * A command asks the Core to act: pair this device, approve a waiting device,
 * switch Watch or sensing, turn a source off, change a setting. Each command
 * is one row of backend/wavr/client_commands.py -- a name, the existing Core
 * route it maps to, its arguments -- and conformance/client_commands.json holds
 * this library to it. The Core alone decides who may do what; this library
 * checks only the SHAPE of the arguments. wavr_command_table writes the table.
 *
 * wavr_command_run sends one command and returns a reply. A malformed call or a
 * Core that did not answer is a reply with ok = false, never NULL; NULL means
 * only that memory ran out, or `url` / `name` was NULL. The reply's JSON is
 *   {ok, status, error, detail, data}
 * where `error` is one stable word -- bad_call, unreachable, unauthorized,
 * forbidden, not_found, conflict, invalid, locked, throttled, refused, server --
 * `detail` is the Core's own sentence (or, for bad_call, which rule the call
 * broke) and `data` is the Core's answer. args_json NULL or "" means no
 * arguments. Onboarding commands never send the token. Blocking.
 *
 * wavr_probe connects WITHOUT verifying the certificate and returns a reply
 *   {ok, https, fingerprint, status, error}
 * with the fingerprint the Core presented, for a person to compare with the
 * one the Core shows before pairing (trust on first use). Blocking.
 */
typedef struct wavr_reply wavr_reply;
WAVR_API int wavr_command_table(char* out, size_t out_len);
WAVR_API wavr_reply* wavr_command_run(const char* url, const char* token, const char* pin,
                                      const char* name, const char* args_json,
                                      int timeout_ms);
WAVR_API wavr_reply* wavr_probe(const char* url, int timeout_ms);
/* The reply as JSON: bytes needed excluding the NUL, or WAVR_ERR_ARGUMENT. */
WAVR_API int wavr_reply_json(const wavr_reply* r, char* out, size_t out_len);
/* 1 when the command succeeded (or the probe saw a certificate), else 0. */
WAVR_API int wavr_reply_ok(const wavr_reply* r);
WAVR_API void wavr_reply_free(wavr_reply* r);

#ifdef __cplusplus
}
#endif

#endif /* WAVR_H */
