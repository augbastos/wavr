#include "wifi_provision.h"
#include "config.h"
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Preferences.h>

static WebServer portal(80);
static DNSServer dns;
static const byte kDnsPort = 53;

static const char* kPortalHtml =
    "<!DOCTYPE html><html><head>"
    "<meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>Wavr node setup</title></head><body>"
    "<h2>Wavr node setup</h2>"
    "<form method=POST action=/save>"
    "Home Wi-Fi SSID:<br><input name=ssid required><br>"
    "Wi-Fi password:<br><input name=pass type=password><br>"
    "Wavr URL (https://ip:port):<br><input name=url value='https://' required><br>"
    "Enrollment code (Wavr &rarr; Nodes &rarr; Add a node):<br>"
    "<input name=code required><br><br>"
    "<button type=submit>Connect</button></form>"
    "<p>Credentials are stored on this device only, and are sent solely to "
    "the Wavr URL you enter above.</p>"
    "</body></html>";

static void handleRoot() { portal.send(200, "text/html", kPortalHtml); }

// Any unrecognised host -- including the OS's own captive-portal probes,
// e.g. /generate_204 (Android) or /hotspot-detect.html (iOS/macOS) -- also
// gets the form, so phones surface the "Sign in to network" prompt
// automatically instead of the operator having to browse to 192.168.4.1.
static void handleNotFound() { handleRoot(); }

static void handleSave() {
  Preferences nvs;
  nvs.begin("wavr-node", false);
  nvs.putString("ssid", portal.arg("ssid"));
  nvs.putString("pass", portal.arg("pass"));
  nvs.putString("url", portal.arg("url"));
  nvs.putString("code", portal.arg("code"));   // one-time; cleared once enroll succeeds
  nvs.putBool("provisioned", true);
  nvs.end();
  portal.send(200, "text/html", "<h3>Saved. Rebooting...</h3>");
  delay(800);
  ESP.restart();
}

bool WifiProvision::isProvisioned() {
  Preferences nvs;
  nvs.begin("wavr-node", true);
  bool p = nvs.getBool("provisioned", false);
  nvs.end();
  return p;
}

void WifiProvision::runPortal() {
  uint8_t mac[6];
  WiFi.macAddress(mac);
  char apName[24];
  snprintf(apName, sizeof(apName), "wavr-node-%02x%02x", mac[4], mac[5]);

  WiFi.mode(WIFI_AP);
  // Open AP by design: the SECRET here is the one-time, 5-minute-TTL
  // enrollment code the operator types into the form, not a Wi-Fi password
  // (matches NODE_PROTOCOL.md's enrollment lane). The AP is only ever up
  // during initial physical setup, which already requires the operator to
  // be standing at the device.
  WiFi.softAP(apName);
  dns.start(kDnsPort, "*", WiFi.softAPIP());   // captive: every hostname resolves to us

  portal.on("/", handleRoot);
  portal.on("/save", HTTP_POST, handleSave);
  portal.onNotFound(handleNotFound);
  portal.begin();

  for (;;) {   // never returns; /save reboots the node
    dns.processNextRequest();
    portal.handleClient();
    delay(2);
  }
}

bool WifiProvision::joinStoredWifi() {
  Preferences nvs;
  nvs.begin("wavr-node", true);
  String ssid = nvs.getString("ssid", "");
  String pass = nvs.getString("pass", "");
  nvs.end();

  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid.c_str(), pass.c_str());
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED &&
         millis() - start < WAVR_WIFI_CONNECT_TIMEOUT_MS) {
    delay(250);
  }
  return WiFi.status() == WL_CONNECTED;
}

// -- reconnect watchdog -------------------------------------------------------
static uint32_t disconnectedSinceMs = 0;
static uint32_t nextRetryMs = 0;
static uint32_t backoffMs = 1000;
static const uint32_t kMaxBackoffMs = 60000;               // cap retries at 1/min
static const uint32_t kGiveUpRestartMs = 10UL * 60UL * 1000UL;  // 10 min -> reboot

void WifiProvision::maintainConnection() {
  if (WiFi.status() == WL_CONNECTED) {
    disconnectedSinceMs = 0;
    backoffMs = 1000;
    return;
  }
  uint32_t now = millis();
  if (disconnectedSinceMs == 0) disconnectedSinceMs = now;

  // Last resort: some Wi-Fi driver states only clear on a reboot. This is
  // fully automatic (no physical intervention) -- setup() will re-run
  // joinStoredWifi() and, if it still fails, retry-with-restart again, so a
  // node quietly keeps trying through a long router outage instead of
  // needing anyone to touch it.
  if (now - disconnectedSinceMs > kGiveUpRestartMs) {
    ESP.restart();
  }

  if (now >= nextRetryMs) {
    WiFi.reconnect();
    backoffMs = (backoffMs * 2 > kMaxBackoffMs) ? kMaxBackoffMs : backoffMs * 2;
    nextRetryMs = now + backoffMs;
  }
}
