#pragma once
#include <Arduino.h>
#include <WiFiClientSecure.h>
#include <mbedtls/ssl.h>
#include <mbedtls/x509_crt.h>   // mbedtls_x509_crt, used by Client::peerCert()'s
                                 // return type below -- included explicitly
                                 // rather than assumed transitive via ssl.h

// Trust-on-first-use (TOFU) pinning for Wavr's self-signed LAN TLS cert.
//
// Wavr has no public CA (backend/wavr/tls.py generates a fresh self-signed
// cert per instance), so there is nothing for a normal CA-chain check to
// validate against -- that is why this firmware used to call setInsecure()
// unconditionally on EVERY connection, which let an on-path LAN attacker
// MitM every call, including the ones carrying the bearer token. TOFU closes
// that for every call after the first:
//
//   1. First enroll ONLY: connect with no verification (setInsecure()) --
//      the node has nothing to pin yet, so this one call is inherently
//      "trust whoever answers this address" (see WavrClient::begin() in
//      wavr_client.cpp).
//   2. The instant that connection returns a genuine 200 + parseable token,
//      commit() persists the certificate THAT connection actually presented
//      (captured via capture()) into NVS ("wavr-node"/"pinPem"+"pinFp").
//   3. Every later connection (telemetry/heartbeat/reactivate) is built via
//      applyTo(), which pins to EXACTLY that certificate via setCACert() --
//      mbedtls only completes the handshake if the live cert is
//      byte-identical to the pinned one. A different cert (MitM'd, or
//      Wavr's cert legitimately rotated/regenerated) makes the handshake
//      itself fail, so the bearer token is never written to the wire --
//      postJson() in wavr_client.cpp sees this exactly like any other
//      unreachable Wavr (connect() returns false -> code <= 0), never a
//      "revoked"/kill signal, so a cert mismatch can never brick or
//      factory-reset a node by itself.
//
// Residual "first-use trust window" (be honest about this, do not oversell
// it): an attacker ALREADY on-path during that one first enroll call (i.e.
// before or during the operator's 5-minute enrollment window) can still
// intercept the enrollment code and the very first token -- TOFU cannot
// close that; nothing can without a pre-shared secret the node has before
// it ever talks to Wavr, which this product does not have (see
// NODE_PROTOCOL.md's enrollment lane). What TOFU DOES close is every call
// after that: a network position gained AFTER first enroll -- the far more
// common case, since it does not require the attacker to be on-path during
// one specific 5-minute window -- can no longer read or replay the token.
//
// If Wavr's cert ever legitimately changes (rotation, reinstall, a new
// `~/.wavr/cert.pem`), every already-pinned node's handshakes start
// failing and stay failing -- there is no automatic re-pin. Recovery is the
// same factory-reset + re-enroll path already used for a revoked node
// (physical >=3s hold, see kill_switch.h/main.cpp, or an admin
// `DELETE /api/nodes/{id}`). This is a deliberate fail-closed choice (a
// silently-accepted new cert would defeat the whole point of pinning), not
// an oversight.
//
// Implementation note (unverified -- see firmware/README.md's compile-status
// section): Arduino-ESP32's WiFiClientSecure has no public "hand me the
// certificate you just verified" getter, unlike ESP8266's BearSSL
// WiFiClientSecure (`setFingerprint()`) -- that convenience API does not
// exist on this core. What IS public, stable mbedtls API is
// `mbedtls_ssl_get_peer_cert(const mbedtls_ssl_context*)`; the only gap is
// reaching the `mbedtls_ssl_context*` itself, which WiFiClientSecure keeps
// on a PROTECTED `sslclient_context* sslclient` member (arduino-esp32's own
// `ssl_client.h`, shipped in the same WiFiClientSecure library). `Client`
// below is a thin subclass that exists ONLY to reach that one pointer --
// this is the single place in this whole firmware that depends on an
// Arduino-ESP32 core internal instead of a documented public API. If a core
// update ever renames `sslclient_context::ssl_ctx` or moves `ssl_client.h`,
// this is the first thing to fix after `pio run -e esp32dev` flags it.
namespace TlsPin {

// WiFiClientSecure subclass used for every Wavr connection in this
// firmware (see wavr_client.cpp's makeClient()) so capture() can read the
// peer certificate off it after a completed handshake.
class Client : public WiFiClientSecure {
 public:
  // The certificate presented on this client's most recent completed TLS
  // handshake, or nullptr if none (not yet connected, or the core's
  // internal layout does not match what this file assumes -- see the
  // module comment above).
  const mbedtls_x509_crt* peerCert() const;
};

// Loads any previously-committed pin from NVS into RAM. Call once, before
// the first connection attempt (WavrClient::begin() does this).
void begin();

// True once a pin (from a prior successful enroll) is held in RAM.
bool hasPin();

// Configures `client`'s TLS trust for its NEXT connect():
//   - hasPin()  -> client.setCACert(<pinned PEM>)  (pin enforced; a
//                  mismatched live cert makes the handshake fail closed)
//   - !hasPin() -> client.setInsecure()            (first-enroll-only path)
void applyTo(Client& client);

// Reads the certificate `client` presented on its most recent completed
// handshake and stages it as the pin candidate (NOT persisted -- call
// commit() only after the caller has independently confirmed this was a
// genuine, successful exchange with Wavr, e.g. a 200 enroll response with a
// parseable token). Returns false if no candidate certificate could be
// read.
bool capture(Client& client);

// Persists the last capture()'d candidate to NVS ("wavr-node"/"pinPem" +
// "pinFp"). No-op if capture() was never called or last returned false.
void commit();

// Human-readable SHA-256 fingerprint of the current pin (uppercase,
// colon-separated hex -- the same format `backend/wavr/tls.py`'s
// `format_fingerprint` uses), or "" if unpinned. Meant to be Serial-logged
// right after commit() so an operator watching `pio run -e esp32dev -t
// monitor` during first boot can compare it, out of band, against the
// fingerprint Wavr's own trusted screen shows for its serving cert -- this
// is a manual, operator-driven check, NOT an automated one (Wavr's
// node-onboarding UI does not currently render a comparable value anywhere
// in the enrollment flow itself).
String fingerprint();

}  // namespace TlsPin
