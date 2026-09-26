// Native (host-compiled) Unity test for fingerprint_fmt.{h,cpp} -- the pure
// SHA-256-digest-to-hex formatter extracted out of tls_pin.cpp. This is the
// one piece of this firmware's logic that must match
// backend/wavr/tls.py::format_fingerprint byte-for-byte: a drift here (wrong
// case, wrong/missing separators, wrong byte order) would silently break the
// operator's out-of-band fingerprint comparison during first enroll (see
// tls_pin.h's module comment) without ever showing up as a compile error.
#include <unity.h>
#include <string.h>
#include "fingerprint_fmt.h"

void setUp(void) {}
void tearDown(void) {}

// Digest bytes 0x00..0x1F -> exercises every hex digit and the colon
// placement (present between bytes, absent after the last one).
static void test_sequential_digest(void) {
  uint8_t digest[32];
  for (int i = 0; i < 32; i++) digest[i] = (uint8_t)i;
  char out[kFingerprintHexBufLen];
  formatFingerprintHex(digest, out);
  TEST_ASSERT_EQUAL_STRING(
      "00:01:02:03:04:05:06:07:08:09:0A:0B:0C:0D:0E:0F:"
      "10:11:12:13:14:15:16:17:18:19:1A:1B:1C:1D:1E:1F",
      out);
}

// SHA-256("") digest, ground-truthed against Python's
// backend/wavr/tls.py::format_fingerprint(b"") -- which hashes the DER it is
// given, so its output for an empty DER is exactly this digest, formatted.
// Binds this test to the actual cross-language contract, not just an
// arbitrary fixture.
static void test_matches_backend_format_fingerprint(void) {
  const uint8_t digest[32] = {
      0xe3, 0xb0, 0xc4, 0x42, 0x98, 0xfc, 0x1c, 0x14, 0x9a, 0xfb, 0xf4,
      0xc8, 0x99, 0x6f, 0xb9, 0x24, 0x27, 0xae, 0x41, 0xe4, 0x64, 0x9b,
      0x93, 0x4c, 0xa4, 0x95, 0x99, 0x1b, 0x78, 0x52, 0xb8, 0x55};
  char out[kFingerprintHexBufLen];
  formatFingerprintHex(digest, out);
  TEST_ASSERT_EQUAL_STRING(
      "E3:B0:C4:42:98:FC:1C:14:9A:FB:F4:C8:99:6F:B9:24:"
      "27:AE:41:E4:64:9B:93:4C:A4:95:99:1B:78:52:B8:55",
      out);
}

static void test_output_length_and_termination(void) {
  uint8_t digest[32] = {0};
  char out[kFingerprintHexBufLen];
  formatFingerprintHex(digest, out);
  TEST_ASSERT_EQUAL_size_t(95, strlen(out));  // 32*2 hex chars + 31 colons
}

int main(int argc, char** argv) {
  UNITY_BEGIN();
  RUN_TEST(test_sequential_digest);
  RUN_TEST(test_matches_backend_format_fingerprint);
  RUN_TEST(test_output_length_and_termination);
  return UNITY_END();
}
