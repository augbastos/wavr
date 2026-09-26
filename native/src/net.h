// The one HTTP client of the native runtime: HTTP/1.1 over a socket, with TLS
// from mbedTLS when WAVR_TLS is built in.
//
// Certificate rules, the same as the Python CLI and the ESP32 node firmware:
//   * loopback:        a Core's self-signed certificate is the EXPECTED answer,
//                      so `Unverified` is allowed there and ONLY there;
//   * anything else:   there is no public CA for a Wavr Core, so the only
//                      acceptable trust is a pin (the TOFU fingerprint captured
//                      at enrolment). The pin is checked after the handshake and
//                      BEFORE a single byte of the request -- and therefore the
//                      bearer token -- is written.
#pragma once

#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace wavr::net {

struct Url {
  bool https = false;
  std::string host;
  int port = 0;
};

std::optional<Url> parse_url(const std::string& text);
bool is_loopback_host(const std::string& host);

enum class Tls {
  Unverified,   // loopback only; refused for any other host
  Capture,      // TOFU enrolment: no verification, report the fingerprint
  Pin,          // must match `pin` exactly or nothing is sent
};

struct Request {
  std::string method = "GET";
  std::string path = "/";
  std::vector<std::pair<std::string, std::string>> headers;
  std::string body;
};

struct Result {
  bool ok = false;                  // an HTTP response was received
  int status = 0;
  std::string body;
  std::string error;                // why there is no response
  std::string peer_fingerprint;     // "AB:CD:..." when TLS was used
};

// Parse one raw HTTP/1.x response (status line, headers, Content-Length or
// chunked body). False for anything malformed or truncated. Exposed for tests.
bool parse_response(const std::string& raw, Result* out);

Result send(const Url& url, const Request& req, Tls tls = Tls::Pin,
            const std::string& pin = "", int timeout_ms = 6000);

}  // namespace wavr::net
