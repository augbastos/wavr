#include "fingerprint_fmt.h"

void formatFingerprintHex(const uint8_t digest[32], char out[kFingerprintHexBufLen]) {
  static const char kHex[] = "0123456789ABCDEF";
  size_t o = 0;
  for (int i = 0; i < 32; i++) {
    if (i) out[o++] = ':';
    out[o++] = kHex[(digest[i] >> 4) & 0xF];
    out[o++] = kHex[digest[i] & 0xF];
  }
  out[o] = '\0';
}
