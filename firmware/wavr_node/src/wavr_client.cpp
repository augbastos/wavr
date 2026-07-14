#include "wavr_client.h"
#include "config.h"
#include "tls_pin.h"
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <time.h>
#include <sys/time.h>
#include <string.h>

static String wavrUrl;
static String nodeToken;
static uint32_t telemetrySeq = 0;
static uint32_t seqSinceCheckpoint = 0;
static bool timeSynced = false;

// -- TLS -----------------------------------------------------------------------
// Wavr serves a self-signed LAN certificate (see backend/wavr/tls.py) -- there
// is no public CA to validate against here. Every connection this firmware
// ever makes is built through TlsPin::applyTo() (see tls_pin.h for the full
// TOFU design): the FIRST enroll connects with no verification at all
// (setInsecure() -- see enrollAndCapture() below, the ONLY caller that can
// ever hit that branch), and every connection after that pins to exactly the
// certificate captured at that first enroll (setCACert()), so an on-path
// attacker who shows up AFTER enrollment can no longer MitM the bearer
// token -- the handshake itself fails closed instead.
static TlsPin::Client makeClient() {
  TlsPin::Client c;
  TlsPin::applyTo(c);
  return c;
}

// -- local-only time sync -------------------------------------------------------
// Wavr is a LAN-only, zero-external-request product end to end -- this
// firmware never talks to pool.ntp.org or any internet NTP server. Every HTTP
// response carries a standard `Date:` header (RFC 7231) that uvicorn/
// Starlette send automatically; the first successful HTTPS response from
// Wavr is used to set the RTC via settimeofday(). NOTE: this is for on-device
// diagnostics/Serial logging only -- Wavr does NOT read a client-supplied
// timestamp from telemetry (node_event() in wavr/nodes.py always stamps its
// OWN server-side time), which is consistent with the rest of the protocol's
// anti-spoof stance: room/modality/state are all server-trusted, never
// client-declared.
static void syncTimeFromHeader(HTTPClient& http) {
  if (timeSynced) return;
  String dateHdr = http.header("Date");
  if (!dateHdr.length()) return;
  struct tm tmv = {};
  // RFC 1123 example: "Tue, 15 Nov 1994 08:12:31 GMT"
  if (strptime(dateHdr.c_str(), "%a, %d %b %Y %H:%M:%S %Z", &tmv)) {
    time_t t = mktime(&tmv);
    if (t > 0) {
      struct timeval tv = {t, 0};
      settimeofday(&tv, nullptr);
      timeSynced = true;
    }
  }
}

// Returns the RAW HTTP status code (e.g. 200, 401, 403), or a value <= 0 (an
// HTTPClient error code, see HTTPClient.h) if no response was received at
// all -- DNS/TCP/TLS failure, timeout, Wavr unreachable/restarting. This
// distinction matters to callers like sendHeartbeat(): a definitive rejection
// from the server (e.g. 403 = this node's token is dead) is NOT the same
// thing as "the network is having a bad moment", and collapsing both into a
// single bool is exactly the bug this fixes (see sendHeartbeat() below) --
// a revoked node's heartbeat used to be indistinguishable from a transient
// network blip, so it kept retrying forever instead of factory-resetting.
static int postJson(const String& path, const String& body, String& out,
                     const String& bearer = "") {
  if (WiFi.status() != WL_CONNECTED) return -1;
  if (bearer.length() && !TlsPin::hasPin()) {
    // Should be structurally unreachable -- WavrClient::begin() always
    // pairs a saved token with a committed TOFU pin (see enrollAndCapture()
    // below) -- but fail closed rather than ever send a bearer token over
    // an unpinned/setInsecure() connection if that invariant is somehow
    // violated (future code change, a partial/corrupt NVS write, ...).
    return -1;
  }
  TlsPin::Client client = makeClient();
  HTTPClient http;
  if (!http.begin(client, wavrUrl + path)) return -1;
  http.addHeader("Content-Type", "application/json");
  if (bearer.length()) http.addHeader("Authorization", "Bearer " + bearer);
  static const char* kWantHeaders[] = {"Date"};
  http.collectHeaders(kWantHeaders, 1);
  int code = http.POST(body);
  if (code > 0) {
    // Only a real HTTP response (even a rejecting one, like 403) carries a
    // body/Date header worth reading; a negative HTTPClient error code means
    // the request never got a response to parse. Note this also covers a
    // TOFU pin MISMATCH: setCACert() (via TlsPin::applyTo()) makes the TLS
    // handshake itself fail closed on a non-matching cert, so that case
    // surfaces here as an ordinary `code <= 0` -- indistinguishable from
    // any other "Wavr unreachable" condition, and handled the same safe way
    // by every caller (retry, never treat as a kill/revoke signal).
    syncTimeFromHeader(http);
    out = http.getString();
  }
  http.end();
  return code;
}

// -- first enroll: the ONE connection this firmware ever makes with no TLS
// verification at all (see tls_pin.h's module comment for the full TOFU
// design and its honestly-disclosed "first-use trust window"). Structured
// separately from postJson() (rather than reusing it) because this is also
// the only call that needs to read the peer certificate back off the
// connection it just used, to stage it as the TOFU pin candidate.
static int enrollAndCapture(const String& body, String& out) {
  if (WiFi.status() != WL_CONNECTED) return -1;
  TlsPin::Client client = makeClient();   // TlsPin::hasPin() is false here
                                           // pre-enrollment -> setInsecure()
  HTTPClient http;
  if (!http.begin(client, wavrUrl + "/api/nodes/enroll")) return -1;
  http.addHeader("Content-Type", "application/json");
  static const char* kWantHeaders[] = {"Date"};
  http.collectHeaders(kWantHeaders, 1);
  int code = http.POST(body);
  if (code > 0) {
    syncTimeFromHeader(http);
    out = http.getString();
    // Stage the cert THIS exchange actually presented -- staged only, not
    // yet trusted. WavrClient::begin() commits it iff `out` turns out to
    // hold a genuine, parseable token (see there).
    TlsPin::capture(client);
  }
  http.end();
  return code;
}

// -- seq checkpointing (survive a reboot without going "stale") -----------------
// wavr/nodes.py's record_seq() only accepts a STRICTLY INCREASING seq per
// node, remembered server-side across the node's whole lifetime. A plain
// in-RAM counter restarts at 0 on every reboot (power loss, OTA, a crash) and
// would then be PERMANENTLY rejected as stale (409) against the server's
// remembered high-water mark -- telemetry would silently stop forever.
// Persisting an occasional checkpoint and jumping ahead of it on boot keeps
// seq monotonic ACROSS reboots without writing NVS on every send (flash wear).
static const uint32_t kSeqCheckpointEvery = 20;   // persist every N telemetry sends
static const uint32_t kSeqBootMargin = 1000;      // jump this far past the last
                                                   // checkpoint on boot, covering
                                                   // sends that happened after the
                                                   // last persisted checkpoint but
                                                   // before an unclean reset

static void saveSeqCheckpoint() {
  Preferences nvs;
  nvs.begin("wavr-node", false);
  nvs.putUInt("seqhi", telemetrySeq);
  nvs.end();
}

static void loadSeqCheckpoint() {
  Preferences nvs;
  nvs.begin("wavr-node", true);
  uint32_t checkpoint = nvs.getUInt("seqhi", 0);
  nvs.end();
  telemetrySeq = checkpoint + kSeqBootMargin;
  saveSeqCheckpoint();   // publish the jumped value now, so a SECOND reboot
                         // right after this one still jumps forward
}

// -- public API ------------------------------------------------------------------
bool WavrClient::begin() {
  TlsPin::begin();   // load any previously-committed TOFU pin into RAM
                      // FIRST, before anything below can use makeClient()

  Preferences nvs;
  nvs.begin("wavr-node", true);
  wavrUrl = nvs.getString("url", "");
  nodeToken = nvs.getString("token", "");
  String code = nvs.getString("code", "");
  nvs.end();

  loadSeqCheckpoint();

  if (nodeToken.length()) return true;   // already enrolled from a prior run
                                          // -- TlsPin::begin() above already
                                          // loaded this token's pin too
  if (!code.length() || !wavrUrl.length()) return false;

  StaticJsonDocument<128> req;
  req["code"] = code;
  req["cert_fingerprint"] = "";   // optional per NODE_PROTOCOL.md; this is the
                                   // NODE's own cert fingerprint field the
                                   // server records but does not yet enforce
                                   // -- unrelated to the TOFU pin below, which
                                   // is this node's trust in WAVR's cert and
                                   // is entirely client-side (see tls_pin.h)
  String body;
  serializeJson(req, body);
  String resp;
  if (enrollAndCapture(body, resp) != 200) return false;

  StaticJsonDocument<256> doc;
  if (deserializeJson(doc, resp)) return false;
  const char* token = doc["token"];
  if (!token || !strlen(token)) return false;
  nodeToken = token;

  // Only NOW -- a genuine 200 with a parseable token -- do we trust the
  // certificate enrollAndCapture() staged. Every connection after this one
  // pins to exactly it (see TlsPin::applyTo()).
  TlsPin::commit();
  Serial.print("[wavr] TOFU pin captured, fingerprint: ");
  Serial.println(TlsPin::fingerprint());
  Serial.println("[wavr] Out-of-band check: compare this against Wavr's own "
                  "serving-cert fingerprint (loopback dashboard) before "
                  "trusting this node's data.");

  Preferences save;
  save.begin("wavr-node", false);
  save.putString("token", nodeToken);
  save.remove("code");   // one-time code: consumed, never reused
  save.end();
  return true;
}

void WavrClient::sendTelemetry(JsonDocument& doc) {
  doc["seq"] = ++telemetrySeq;
  if (++seqSinceCheckpoint >= kSeqCheckpointEvery) {
    seqSinceCheckpoint = 0;
    saveSeqCheckpoint();
  }
  String body;
  serializeJson(doc, body);
  String resp;
  postJson("/api/nodes/telemetry", body, resp, nodeToken);   // fire-and-forget
}

WavrClient::Command WavrClient::sendHeartbeat() {
  String resp;
  int code = postJson("/api/nodes/heartbeat", "{}", resp, nodeToken);

  // THE FIX: a definitive 401/403 on this authenticated route means Wavr no
  // longer accepts this node's own token. NodeStore.revoke() clears the
  // token hash server-side (belt-and-suspenders anti-resurrection -- see
  // wavr/nodes.py), so a revoked node can never authenticate again and this
  // call always comes back 401/403 for it; there is no friendlier in-body
  // "revoked" this node will ever see. Previously this branch was
  // unreachable: postJson() collapsed 403 into the same "false" as a dead
  // network, so a revoked node's heartbeat looked identical to a Wi-Fi
  // hiccup and it just kept retrying forever instead of factory-resetting.
  if (code == 401 || code == 403) return Command::kRevoked;

  // Anything else that isn't a clean 200 is a TRANSIENT condition (no
  // response at all -- code <= 0, e.g. DNS/TCP/TLS failure, timeout, Wavr
  // mid-restart), never a kill signal. Fail gracefully: keep the current
  // sensing state and retry next heartbeat.
  if (code != 200) return Command::kUnreachable;

  StaticJsonDocument<128> doc;
  if (deserializeJson(doc, resp)) return Command::kUnreachable;
  const char* cmd = doc["command"];
  if (!cmd) return Command::kUnreachable;
  if (!strcmp(cmd, "run") || !strcmp(cmd, "ok")) return Command::kRun;
  if (!strcmp(cmd, "sleep")) return Command::kSleep;
  if (!strcmp(cmd, "revoked")) return Command::kRevoked;
  // An unknown/unexpected command string on an otherwise-valid 200 is
  // ambiguous, not a kill signal -- do NOT guess "dead" here (that used to
  // turn a body-parsing edge case into a spurious factory-reset). Retry.
  return Command::kUnreachable;
}

void WavrClient::sendReactivate(uint32_t pressCount) {
  StaticJsonDocument<32> req;
  req["press_count"] = pressCount;
  String body;
  serializeJson(req, body);
  String resp;
  postJson("/api/nodes/reactivate", body, resp, nodeToken);
}
