// Wavr sensor node - main firmware entry point.
//
// See firmware/NODE_PROTOCOL.md for the wire contract this implements and
// firmware/README.md for the flash + wiring walkthrough. This file only
// wires the pieces together -- Wi-Fi provisioning, the Wavr HTTPS client,
// the kill-switch, the status LED, OTA, and the sensor driver each live in
// their own module and are independently testable/replaceable. The two
// seams a new deployment usually touches are include/config.h (pins/timing)
// and src/sensors/ (which driver is compiled in).

#include <Arduino.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <WiFi.h>
#include "config.h"
#include "wifi_provision.h"
#include "wavr_client.h"
#include "kill_switch.h"
#include "status_led.h"
#include "ota_update.h"

#if WAVR_SENSOR_DRIVER == WAVR_SENSOR_LD2450
  #include "sensors/ld2450_driver.h"
  static Ld2450Driver sensor(Serial2, WAVR_LD2450_RX_PIN, WAVR_LD2450_TX_PIN,
                              WAVR_LD2450_BAUD);
#elif WAVR_SENSOR_DRIVER == WAVR_SENSOR_PIR
  #include "sensors/pir_driver.h"
  static PirDriver sensor(WAVR_PIR_PIN);
#else
  #error "Unknown WAVR_SENSOR_DRIVER -- add a driver in src/sensors/ or fix config.h"
#endif

static uint32_t pressCount = 0;
static bool sensing = true;   // false while the kill-switch heartbeat says "sleep"

// -- press_count persistence (B3: survive a reboot without needing N+1 presses) --
// wavr/nodes.py's NodeStore.reactivate() only accepts a press_count STRICTLY
// ABOVE the server's remembered high-water mark for this node_id (see
// firmware/NODE_PROTOCOL.md's reactivate section). A plain in-RAM counter
// restarts at 0 on every reboot (power loss, OTA, a crash) while the server
// keeps whatever it last saw -- the physical re-enable button would then
// silently need N+1 presses to produce a value the server still accepts,
// with no error shown anywhere. Checkpointing to NVS on every press (not a
// windowed/batched checkpoint like telemetrySeq in wavr_client.cpp -- a
// physical button press is a rare, human-paced event, so there is no flash-
// wear reason to defer it) keeps this monotonic across reboots exactly like
// the docs claim.
static void loadPressCount() {
  Preferences nvs;
  nvs.begin("wavr-node", true);
  pressCount = nvs.getUInt("pressCnt", 0);
  nvs.end();
}

static void savePressCount() {
  Preferences nvs;
  nvs.begin("wavr-node", false);
  nvs.putUInt("pressCnt", pressCount);
  nvs.end();
}

static void factoryReset() {
  Preferences nvs;
  nvs.begin("wavr-node", false);
  nvs.clear();   // wipes Wi-Fi creds, Wavr URL, token, TOFU pin, seq
                  // checkpoint AND pressCnt -- intentional: a re-flash/
                  // re-enroll gets a brand-new node_id server-side, whose
                  // own press_count high-water mark starts at 0 too.
  nvs.end();
  ESP.restart();   // never returns
}

void setup() {
  Serial.begin(115200);
  StatusLed::begin();
  StatusLed::set(StatusLed::State::kConnecting);
  KillSwitch::begin();
  loadPressCount();

  if (!WifiProvision::isProvisioned()) {
    StatusLed::set(StatusLed::State::kProvisioning);
    WifiProvision::runPortal();   // never returns (reboots on submit)
  }

  if (!WifiProvision::joinStoredWifi()) {
    // Backoff, don't spin-retry a dead router forever: reboot after a short
    // delay and try again. Combined with WifiProvision::maintainConnection()
    // in loop() (for drops AFTER a successful join), this is what lets a
    // node survive a router reboot/outage without physical intervention.
    delay(5000);
    ESP.restart();
  }

  if (!WavrClient::begin()) {
    // Enrollment failed (bad/expired code, Wavr unreachable, ...). Back off
    // and retry rather than looping tight or wiping provisioning -- an
    // operator may still be mid-setup, or Wavr may be mid-restart.
    StatusLed::set(StatusLed::State::kError);
    delay(10000);
    ESP.restart();
  }

  sensor.begin();
  OtaUpdate::begin();
  StatusLed::set(StatusLed::State::kActive);
}

void loop() {
  WifiProvision::maintainConnection();

  // -- kill-switch: the ONLY path from disabled back to active --------------
  switch (KillSwitch::poll()) {
    case KillSwitch::Event::kShortPress:
      pressCount++;
      savePressCount();   // persist BEFORE the network round-trip: if the
                           // POST below never lands (Wi-Fi drop, Wavr down),
                           // the counter is still durably advanced, and the
                           // NEXT press is still > whatever the server last
                           // actually accepted -- robust to any number of
                           // missed sends, never just to a clean reboot.
      WavrClient::sendReactivate(pressCount);
      break;
    case KillSwitch::Event::kFactoryReset:
      factoryReset();   // never returns
      break;
    default:
      break;
  }

  // -- OTA (local network only) ----------------------------------------------
  OtaUpdate::tick();

  uint32_t now = millis();
  bool online = (WiFi.status() == WL_CONNECTED);

  // -- heartbeat: poll the remote kill command ---------------------------------
  // "low-power poll": while disabled (sensing == false) the node backs off to
  // WAVR_HEARTBEAT_DISABLED_MS instead of the normal cadence -- less radio/
  // HTTPS activity while it waits for either a physical reactivate press or a
  // revoke. Flips back to WAVR_HEARTBEAT_MS the moment a heartbeat reports
  // "run" again below.
  static uint32_t lastHeartbeat = 0;
  uint32_t heartbeatIntervalMs = sensing ? WAVR_HEARTBEAT_MS
                                          : WAVR_HEARTBEAT_DISABLED_MS;
  if (online && now - lastHeartbeat >= heartbeatIntervalMs) {
    lastHeartbeat = now;
    switch (WavrClient::sendHeartbeat()) {
      case WavrClient::Command::kRun:
        sensing = true;
        StatusLed::set(StatusLed::State::kActive);
        break;
      case WavrClient::Command::kSleep:
        sensing = false;   // remote-OFF reached the hardware
        StatusLed::set(StatusLed::State::kDisabled);
        break;
      case WavrClient::Command::kRevoked:
        factoryReset();   // token is dead -- re-flash/re-enroll to come back
        break;
      case WavrClient::Command::kUnreachable:
        // Wavr is down/unreachable right now -- keep the current sensing
        // state and just retry next heartbeat. Do NOT sleep or reset on a
        // transient network blip: fail gracefully, not brick.
        break;
    }
  }

  // -- sensing + telemetry -------------------------------------------------------
  // "sleep" means STOP SENSING, not just stop sending: while disabled the
  // driver itself is never polled (no UART/GPIO reads), on top of the
  // low-power heartbeat cadence above -- the node goes properly dark until a
  // physical kill-switch press (or a remote revoke -> factory reset) changes
  // that.
  if (sensing) sensor.poll();
  static uint32_t lastTelemetry = 0;
  if (online && sensing && sensor.hasReading() &&
      now - lastTelemetry >= WAVR_TELEMETRY_MS) {
    lastTelemetry = now;
    StaticJsonDocument<256> doc;
    sensor.buildTelemetry(doc);
    WavrClient::sendTelemetry(doc);
  }

  StatusLed::tick();
  delay(5);
}
