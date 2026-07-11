#include "tls_pin.h"
#include <Preferences.h>
#include <ssl_client.h>          // arduino-esp32 internal (same lib as
                                  // WiFiClientSecure.h) -- see tls_pin.h's
                                  // module comment for why this is the one
                                  // unverified include in this firmware.
#include <mbedtls/x509_crt.h>
#include <mbedtls/base64.h>
#include <mbedtls/sha256.h>

const mbedtls_x509_crt* TlsPin::Client::peerCert() const {
  return sslclient ? mbedtls_ssl_get_peer_cert(&sslclient->ssl_ctx) : nullptr;
}

namespace {

String pinnedPem;      // committed pin, loaded by begin() / written by commit()
String pinnedFp;        // human-readable fingerprint of the same cert
String candidatePem;    // staged by capture(), not yet committed
String candidateFp;

// SHA-256 of the DER certificate bytes, formatted uppercase colon-separated
// hex -- matches backend/wavr/tls.py::format_fingerprint exactly so an
// operator can eyeball-compare the two.
String sha256Fingerprint(const unsigned char* der, size_t len) {
  unsigned char digest[32];
  // mbedtls_sha256(input, ilen, output, is224) is the current (mbedtls
  // >=3.0, i.e. current ESP-IDF/arduino-esp32) signature. Older cores
  // (ESP-IDF v4.x-era arduino-esp32 2.0.x, bundled mbedtls 2.x) name this
  // mbedtls_sha256_ret() instead -- if `pio run` fails to resolve this
  // symbol, that rename is the fix (see firmware/README.md).
  mbedtls_sha256(der, len, digest, 0);
  static const char kHex[] = "0123456789ABCDEF";
  String out;
  out.reserve(32 * 3);
  for (int i = 0; i < 32; i++) {
    if (i) out += ':';
    out += kHex[(digest[i] >> 4) & 0xF];
    out += kHex[digest[i] & 0xF];
  }
  return out;
}

// PEM-encodes DER certificate bytes so they can be handed to
// WiFiClientSecure::setCACert(), which takes a NUL-terminated PEM string
// (DER cannot be passed through it safely -- DER commonly contains embedded
// 0x00 bytes, and the Arduino wrapper measures the buffer with strlen()).
// Fixed-size stack buffer (matches this codebase's preference for static
// sizing, e.g. StaticJsonDocument, over dynamic allocation): a self-signed
// RSA-2048 leaf's DER is typically well under 1.5 KB, base64 expands that
// ~4/3 -> comfortably inside kMaxB64. mbedtls_base64_encode itself checks
// the destination size and fails (without writing OOB) rather than
// overflow if a future cert format ever needs more.
String derToPem(const unsigned char* der, size_t len) {
  static const size_t kMaxB64 = 2048;
  unsigned char b64[kMaxB64];
  size_t written = 0;
  if (mbedtls_base64_encode(b64, kMaxB64, &written, der, len) != 0) return "";

  String pem = "-----BEGIN CERTIFICATE-----\n";
  for (size_t i = 0; i < written; i += 64) {
    size_t chunk = (written - i < 64) ? (written - i) : 64;
    for (size_t j = 0; j < chunk; j++) pem += (char)b64[i + j];
    pem += '\n';
  }
  pem += "-----END CERTIFICATE-----\n";
  return pem;
}

}  // namespace

void TlsPin::begin() {
  Preferences nvs;
  nvs.begin("wavr-node", true);
  pinnedPem = nvs.getString("pinPem", "");
  pinnedFp = nvs.getString("pinFp", "");
  nvs.end();
}

bool TlsPin::hasPin() { return pinnedPem.length() > 0; }

void TlsPin::applyTo(Client& client) {
  if (hasPin()) {
    client.setCACert(pinnedPem.c_str());
  } else {
    // First-enroll-only path -- see the module comment in tls_pin.h. Every
    // OTHER caller of applyTo() is guaranteed hasPin()==true by the time it
    // runs (WavrClient::begin() always pairs a saved token with a
    // committed pin -- see its comment), so reaching this branch on a
    // data-plane call would mean that invariant broke; postJson() in
    // wavr_client.cpp additionally refuses outright to send a bearer token
    // when !hasPin(), as a second, independent guard against that.
    client.setInsecure();
  }
}

bool TlsPin::capture(Client& client) {
  const mbedtls_x509_crt* cert = client.peerCert();
  if (!cert || !cert->raw.p || cert->raw.len == 0) return false;
  candidateFp = sha256Fingerprint(cert->raw.p, cert->raw.len);
  candidatePem = derToPem(cert->raw.p, cert->raw.len);
  return candidatePem.length() > 0;
}

void TlsPin::commit() {
  if (!candidatePem.length()) return;
  pinnedPem = candidatePem;
  pinnedFp = candidateFp;
  // ESP32 NVS entries top out around 4000 bytes each; a self-signed
  // RSA-2048 leaf's PEM (~1.5-2 KB, see derToPem()'s sizing comment) fits
  // comfortably under that, so this is not expected to fail in practice --
  // flagged here rather than silently, since Preferences::putString()
  // returns a length/0 that this firmware does not currently check.
  Preferences nvs;
  nvs.begin("wavr-node", false);
  nvs.putString("pinPem", pinnedPem);
  nvs.putString("pinFp", pinnedFp);
  nvs.end();
  candidatePem = "";
  candidateFp = "";
}

String TlsPin::fingerprint() { return pinnedFp.length() ? pinnedFp : candidateFp; }
