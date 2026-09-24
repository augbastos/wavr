#include "net.h"

#include <algorithm>
#include <cctype>
#include <cstring>
#include <memory>

#include "fingerprint_fmt.h"

#if defined(_WIN32)
#  ifndef NOMINMAX
#    define NOMINMAX
#  endif
#  include <winsock2.h>
#  include <ws2tcpip.h>
using socket_t = SOCKET;
constexpr socket_t kBadSocket = INVALID_SOCKET;
#else
#  include <arpa/inet.h>
#  include <fcntl.h>
#  include <netdb.h>
#  include <netinet/in.h>
#  include <poll.h>
#  include <sys/socket.h>
#  include <sys/time.h>
#  include <unistd.h>
using socket_t = int;
constexpr socket_t kBadSocket = -1;
#endif

#if WAVR_TLS
#  include <mbedtls/ctr_drbg.h>
#  include <mbedtls/entropy.h>
#  include <mbedtls/error.h>
#  include <mbedtls/net_sockets.h>
#  include <mbedtls/sha256.h>
#  include <mbedtls/ssl.h>
#  include <mbedtls/x509_crt.h>
#  include <psa/crypto.h>
#endif

namespace wavr::net {

namespace {

constexpr size_t kMaxResponse = 1 << 20;   // a Core answer is kilobytes; refuse a flood

void close_socket(socket_t s) {
#if defined(_WIN32)
  closesocket(s);
#else
  ::close(s);
#endif
}

struct WinsockInit {
#if defined(_WIN32)
  WinsockInit() {
    WSADATA d;
    WSAStartup(MAKEWORD(2, 2), &d);
  }
#endif
};

socket_t connect_to(const Url& url, int timeout_ms, std::string* error) {
  [[maybe_unused]] static WinsockInit init;
  addrinfo hints{};
  hints.ai_family = AF_UNSPEC;
  hints.ai_socktype = SOCK_STREAM;
  addrinfo* res = nullptr;
  const std::string port = std::to_string(url.port);
  if (getaddrinfo(url.host.c_str(), port.c_str(), &hints, &res) != 0 || !res) {
    *error = "cannot resolve " + url.host;
    return kBadSocket;
  }
  socket_t s = kBadSocket;
  for (addrinfo* ai = res; ai; ai = ai->ai_next) {
    s = socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
    if (s == kBadSocket) continue;
#if defined(SO_NOSIGPIPE)
    int one = 1;
    setsockopt(s, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one);
#endif
    // Non-blocking connect bounded by timeout_ms, then back to blocking with
    // per-operation timeouts: a Core that accepts and then stalls must not
    // hang a node forever.
#if defined(_WIN32)
    u_long nb = 1;
    ioctlsocket(s, FIONBIO, &nb);
#else
    fcntl(s, F_SETFL, fcntl(s, F_GETFL, 0) | O_NONBLOCK);
#endif
    int rc = ::connect(s, ai->ai_addr, static_cast<int>(ai->ai_addrlen));
    bool connected = rc == 0;
    if (!connected) {
#if defined(_WIN32)
      fd_set wr, ex;
      FD_ZERO(&wr);
      FD_ZERO(&ex);
      FD_SET(s, &wr);
      FD_SET(s, &ex);
      timeval tv{timeout_ms / 1000, (timeout_ms % 1000) * 1000};
      connected = select(0, nullptr, &wr, &ex, &tv) > 0 && FD_ISSET(s, &wr);
#else
      pollfd p{s, POLLOUT, 0};
      if (poll(&p, 1, timeout_ms) > 0) {
        int err = 0;
        socklen_t len = sizeof err;
        getsockopt(s, SOL_SOCKET, SO_ERROR, &err, &len);
        connected = err == 0;
      }
#endif
    }
    if (connected) {
#if defined(_WIN32)
      nb = 0;
      ioctlsocket(s, FIONBIO, &nb);
      DWORD tv = static_cast<DWORD>(timeout_ms);
      setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&tv), sizeof tv);
      setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char*>(&tv), sizeof tv);
#else
      fcntl(s, F_SETFL, fcntl(s, F_GETFL, 0) & ~O_NONBLOCK);
      timeval tv{timeout_ms / 1000, (timeout_ms % 1000) * 1000};
      setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
      setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
#endif
      break;
    }
    close_socket(s);
    s = kBadSocket;
  }
  freeaddrinfo(res);
  if (s == kBadSocket) *error = "cannot connect to " + url.host + ":" + port;
  return s;
}

// A peer that closes mid-write must be an error return, not SIGPIPE killing
// the node: MSG_NOSIGNAL on Linux, SO_NOSIGPIPE (set in connect_to) on Apple.
#if defined(MSG_NOSIGNAL)
constexpr int kSendFlags = MSG_NOSIGNAL;
#else
constexpr int kSendFlags = 0;
#endif

int raw_send(socket_t s, const unsigned char* buf, size_t len) {
  return static_cast<int>(
      ::send(s, reinterpret_cast<const char*>(buf), static_cast<int>(len), kSendFlags));
}
int raw_recv(socket_t s, unsigned char* buf, size_t len) {
  return static_cast<int>(::recv(s, reinterpret_cast<char*>(buf), static_cast<int>(len), 0));
}

// A byte channel: the socket itself, or TLS over it.
struct Channel {
  virtual ~Channel() = default;
  virtual bool write_all(const std::string& data) = 0;
  virtual int read(unsigned char* buf, size_t len) = 0;   // <=0: closed or error
};

struct PlainChannel : Channel {
  socket_t s;
  explicit PlainChannel(socket_t sock) : s(sock) {}
  bool write_all(const std::string& data) override {
    size_t off = 0;
    while (off < data.size()) {
      int n = raw_send(s, reinterpret_cast<const unsigned char*>(data.data()) + off,
                       data.size() - off);
      if (n <= 0) return false;
      off += static_cast<size_t>(n);
    }
    return true;
  }
  int read(unsigned char* buf, size_t len) override { return raw_recv(s, buf, len); }
};

#if WAVR_TLS
int bio_send(void* ctx, const unsigned char* buf, size_t len) {
  int n = raw_send(*static_cast<socket_t*>(ctx), buf, len);
  return n < 0 ? MBEDTLS_ERR_NET_SEND_FAILED : n;
}
int bio_recv(void* ctx, unsigned char* buf, size_t len) {
  int n = raw_recv(*static_cast<socket_t*>(ctx), buf, len);
  return n < 0 ? MBEDTLS_ERR_NET_RECV_FAILED : n;
}

struct TlsChannel : Channel {
  socket_t s;
  mbedtls_entropy_context entropy;
  mbedtls_ctr_drbg_context drbg;
  mbedtls_ssl_config conf;
  mbedtls_ssl_context ssl;

  explicit TlsChannel(socket_t sock) : s(sock) {
    mbedtls_entropy_init(&entropy);
    mbedtls_ctr_drbg_init(&drbg);
    mbedtls_ssl_config_init(&conf);
    mbedtls_ssl_init(&ssl);
  }
  ~TlsChannel() override {
    mbedtls_ssl_close_notify(&ssl);
    mbedtls_ssl_free(&ssl);
    mbedtls_ssl_config_free(&conf);
    mbedtls_ctr_drbg_free(&drbg);
    mbedtls_entropy_free(&entropy);
  }

  // Handshake WITHOUT chain verification -- a Wavr Core has no public CA -- and
  // hand back the SHA-256 fingerprint of the certificate actually presented, so
  // the caller decides whether that is acceptable before sending anything.
  bool handshake(const std::string& host, std::string* fingerprint, std::string* error) {
    if (psa_crypto_init() != PSA_SUCCESS) {
      *error = "TLS: crypto init failed";
      return false;
    }
    static const char pers[] = "wavr-native";
    if (mbedtls_ctr_drbg_seed(&drbg, mbedtls_entropy_func, &entropy,
                              reinterpret_cast<const unsigned char*>(pers), sizeof pers) != 0 ||
        mbedtls_ssl_config_defaults(&conf, MBEDTLS_SSL_IS_CLIENT, MBEDTLS_SSL_TRANSPORT_STREAM,
                                    MBEDTLS_SSL_PRESET_DEFAULT) != 0) {
      *error = "TLS: setup failed";
      return false;
    }
    mbedtls_ssl_conf_authmode(&conf, MBEDTLS_SSL_VERIFY_NONE);
    mbedtls_ssl_conf_rng(&conf, mbedtls_ctr_drbg_random, &drbg);
    if (mbedtls_ssl_setup(&ssl, &conf) != 0 || mbedtls_ssl_set_hostname(&ssl, host.c_str()) != 0) {
      *error = "TLS: setup failed";
      return false;
    }
    mbedtls_ssl_set_bio(&ssl, &s, bio_send, bio_recv, nullptr);
    int rc;
    while ((rc = mbedtls_ssl_handshake(&ssl)) != 0) {
      if (rc != MBEDTLS_ERR_SSL_WANT_READ && rc != MBEDTLS_ERR_SSL_WANT_WRITE) {
        char msg[128];
        mbedtls_strerror(rc, msg, sizeof msg);
        *error = std::string("TLS handshake failed: ") + msg;
        return false;
      }
    }
    const mbedtls_x509_crt* peer = mbedtls_ssl_get_peer_cert(&ssl);
    if (!peer) {
      *error = "TLS: the server presented no certificate";
      return false;
    }
    unsigned char digest[32];
    mbedtls_sha256(peer->raw.p, peer->raw.len, digest, 0);
    char hex[kFingerprintHexBufLen];
    formatFingerprintHex(digest, hex);
    *fingerprint = hex;
    return true;
  }

  bool write_all(const std::string& data) override {
    size_t off = 0;
    while (off < data.size()) {
      int n = mbedtls_ssl_write(&ssl, reinterpret_cast<const unsigned char*>(data.data()) + off,
                                data.size() - off);
      if (n == MBEDTLS_ERR_SSL_WANT_READ || n == MBEDTLS_ERR_SSL_WANT_WRITE) continue;
      if (n <= 0) return false;
      off += static_cast<size_t>(n);
    }
    return true;
  }

  int read(unsigned char* buf, size_t len) override {
    for (;;) {
      int n = mbedtls_ssl_read(&ssl, buf, len);
      if (n == MBEDTLS_ERR_SSL_WANT_READ || n == MBEDTLS_ERR_SSL_WANT_WRITE) continue;
#  ifdef MBEDTLS_ERR_SSL_RECEIVED_NEW_SESSION_TICKET
      // TLS 1.3 servers send tickets after the handshake; not data.
      if (n == MBEDTLS_ERR_SSL_RECEIVED_NEW_SESSION_TICKET) continue;
#  endif
      if (n == MBEDTLS_ERR_SSL_PEER_CLOSE_NOTIFY) return 0;
      return n;
    }
  }
};
#endif

std::string lower(std::string s) {
  for (auto& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  return s;
}

}  // namespace

bool parse_response(const std::string& raw, Result* out) {
  size_t head_end = raw.find("\r\n\r\n");
  if (head_end == std::string::npos || raw.compare(0, 5, "HTTP/") != 0) return false;
  size_t sp = raw.find(' ');
  if (sp == std::string::npos || sp + 4 > head_end) return false;
  out->status = std::atoi(raw.c_str() + sp + 1);
  std::string headers = lower(raw.substr(0, head_end));
  std::string body = raw.substr(head_end + 4);
  if (headers.find("\r\ntransfer-encoding: chunked") != std::string::npos) {
    std::string decoded;
    size_t pos = 0;
    for (;;) {
      size_t eol = body.find("\r\n", pos);
      if (eol == std::string::npos) return false;
      unsigned long n = std::strtoul(body.c_str() + pos, nullptr, 16);
      pos = eol + 2;
      if (n == 0) break;
      if (n > body.size() - pos) return false;   // no pos + n: a huge n would wrap
      decoded.append(body, pos, n);
      pos += n + 2;
    }
    body = std::move(decoded);
  } else {
    size_t cl = headers.find("\r\ncontent-length:");
    if (cl != std::string::npos) {
      unsigned long n = std::strtoul(headers.c_str() + cl + 17, nullptr, 10);
      if (n > body.size()) return false;   // the connection ended mid-answer
      body.resize(n);
    }
  }
  out->body = std::move(body);
  out->ok = true;
  return true;
}

std::optional<Url> parse_url(const std::string& text) {
  Url u;
  std::string rest;
  if (text.rfind("https://", 0) == 0) {
    u.https = true;
    rest = text.substr(8);
  } else if (text.rfind("http://", 0) == 0) {
    rest = text.substr(7);
  } else {
    return std::nullopt;
  }
  rest = rest.substr(0, rest.find('/'));
  if (rest.empty()) return std::nullopt;
  size_t colon = rest.rfind(':');
  if (rest.front() == '[') {                      // [::1]:8000
    size_t close = rest.find(']');
    if (close == std::string::npos) return std::nullopt;
    u.host = rest.substr(1, close - 1);
    colon = rest.find(':', close);
  } else {
    u.host = rest.substr(0, colon);
  }
  u.port = colon == std::string::npos ? (u.https ? 443 : 80)
                                      : std::atoi(rest.c_str() + colon + 1);
  if (u.host.empty() || u.port <= 0 || u.port > 65535) return std::nullopt;
  // A host is a name or an address: no control bytes (an embedded NUL was
  // accepted and then silently cut by every C API after it), no spaces, and no
  // userinfo -- "127.0.0.1@elsewhere" is not a URL this client should read.
  for (unsigned char c : u.host) {
    if (c <= 0x20 || c == 0x7f || c == '@' || c == '\\' || c == '?' || c == '#') return std::nullopt;
  }
  return u;
}

// status.is_loopback, checked against conformance/loopback.json. An address,
// parsed, or the name "localhost" -- never a prefix test: a DNS name such as
// "127.0.0.1.example.com" starts like an address and resolves anywhere.
bool is_loopback_host(const std::string& host) {
  const std::string h = lower(host);
  if (h == "localhost") return true;
  in_addr v4{};
  if (inet_pton(AF_INET, h.c_str(), &v4) == 1) return (ntohl(v4.s_addr) >> 24) == 127;
  in6_addr v6{};
  if (inet_pton(AF_INET6, h.c_str(), &v6) == 1) {
    const unsigned char* b = reinterpret_cast<const unsigned char*>(&v6);
    static const unsigned char mapped[12] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xff};
    if (std::memcmp(b, mapped, 12) == 0) return b[12] == 127;   // ::ffff:127.x.y.z
    return IN6_IS_ADDR_LOOPBACK(&v6);
  }
  return false;
}

Result send(const Url& url, const Request& req, Tls tls, const std::string& pin,
            int timeout_ms) {
  Result r;
  // Plain HTTP has no certificate at all: the request, and any token on it,
  // would cross the network in clear text. Only to this machine.
  if (!url.https && !is_loopback_host(url.host)) {
    r.error = "refusing plain HTTP to " + url.host +
              ": a Core across the network is reached over HTTPS";
    return r;
  }
  if (url.https && tls == Tls::Unverified && !is_loopback_host(url.host)) {
    r.error = "refusing an unverified TLS connection to " + url.host +
              ": only a loopback Core may skip certificate checks";
    return r;
  }
  if (url.https && tls == Tls::Pin && pin.empty()) {
    r.error = "no certificate pin for " + url.host + "; enrol first";
    return r;
  }
#if !WAVR_TLS
  if (url.https) {
    r.error = "this build has no TLS support";
    return r;
  }
#endif
  socket_t s = connect_to(url, timeout_ms, &r.error);
  if (s == kBadSocket) return r;

  std::unique_ptr<Channel> ch;
#if WAVR_TLS
  if (url.https) {
    auto t = std::make_unique<TlsChannel>(s);
    if (!t->handshake(url.host, &r.peer_fingerprint, &r.error)) {
      t.reset();
      close_socket(s);
      return r;
    }
    if (tls == Tls::Pin && r.peer_fingerprint != pin) {
      // The whole point: nothing -- not the path, not the token -- is written.
      r.error = "the Core presented a certificate that does not match this node's pin";
      t.reset();
      close_socket(s);
      return r;
    }
    ch = std::move(t);
  }
#endif
  if (!ch) ch = std::make_unique<PlainChannel>(s);

  std::string msg = req.method + " " + req.path + " HTTP/1.1\r\nHost: " + url.host + ":" +
                    std::to_string(url.port) + "\r\nConnection: close\r\nUser-Agent: wavr-native\r\n";
  for (const auto& [k, v] : req.headers) msg += k + ": " + v + "\r\n";
  if (!req.body.empty() || req.method == "POST" || req.method == "PUT") {
    msg += "Content-Length: " + std::to_string(req.body.size()) + "\r\n";
  }
  msg += "\r\n" + req.body;

  std::string raw;
  if (ch->write_all(msg)) {
    unsigned char buf[8192];
    int n;
    while ((n = ch->read(buf, sizeof buf)) > 0) {
      raw.append(reinterpret_cast<const char*>(buf), static_cast<size_t>(n));
      if (raw.size() > kMaxResponse) break;
    }
  }
  ch.reset();
  close_socket(s);
  if (raw.size() > kMaxResponse) {
    r.error = "response too large";
  } else if (!parse_response(raw, &r)) {
    r.error = raw.empty() ? "no response" : "malformed HTTP response";
  }
  return r;
}

}  // namespace wavr::net
