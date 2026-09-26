#pragma once
#include <stddef.h>
#include <stdint.h>

// Pure, hardware-independent formatting of a 32-byte SHA-256 digest as
// uppercase colon-separated hex ("AB:CD:...:EF") -- extracted out of
// tls_pin.cpp so it can be exercised by the native Unity test in
// test/test_fingerprint_fmt/ without pulling in Arduino/mbedtls/NVS. This
// is the exact format backend/wavr/tls.py::format_fingerprint produces, so
// an operator can eyeball-compare a node's Serial-printed fingerprint
// against Wavr's own serving-cert fingerprint (see tls_pin.h's module
// comment) -- a formatting drift here (wrong case, missing/extra
// separators, wrong byte order) would silently break that comparison.
//
// Buffer size: 32 bytes -> 2 hex chars each + ':' between (not after the
// last) = 32*2 + 31 = 95 chars + NUL.
inline constexpr size_t kFingerprintHexBufLen = 96;

void formatFingerprintHex(const uint8_t digest[32], char out[kFingerprintHexBufLen]);
