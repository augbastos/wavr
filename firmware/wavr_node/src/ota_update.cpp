#include "ota_update.h"
#include "config.h"
#include "status_led.h"
#include <Arduino.h>   // snprintf -- .cpp files under src/ get no implicit Arduino.h
#include <ArduinoOTA.h>
#include <WiFi.h>

void OtaUpdate::begin() {
  uint8_t mac[6];
  WiFi.macAddress(mac);
  char hostname[32];
  snprintf(hostname, sizeof(hostname), "%s%02x%02x", WAVR_OTA_HOSTNAME_PREFIX,
           mac[4], mac[5]);
  ArduinoOTA.setHostname(hostname);
  ArduinoOTA.setPassword(WAVR_OTA_PASSWORD);

  ArduinoOTA.onStart([]() { StatusLed::set(StatusLed::State::kConnecting); });
  ArduinoOTA.onError([](ota_error_t) { StatusLed::set(StatusLed::State::kError); });
  // onEnd reboots via the ArduinoOTA library itself once the flash write
  // completes; nothing extra to do here.

  ArduinoOTA.begin();
}

void OtaUpdate::tick() { ArduinoOTA.handle(); }
