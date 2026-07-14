#include "pir_driver.h"
#include <Arduino.h>

// A PIR module's OUT pin drops LOW a few seconds after the last motion
// (module-dependent hold time -- ~2-5s on a typical HC-SR501 at its default
// trimmer setting). We stretch that into a slightly longer software hold so
// a single blip doesn't flap presence between consecutive telemetry ticks.
static const uint32_t kHoldMs = 4000;

void PirDriver::begin() { pinMode(_pin, INPUT); }

void PirDriver::poll() {
  if (digitalRead(_pin) == HIGH) {
    _lastMotionMs = millis();
    _presence = true;
  } else if (millis() - _lastMotionMs > kHoldMs) {
    _presence = false;
  }
}

void PirDriver::buildTelemetry(JsonDocument& doc) {
  // Sent every telemetry tick (not just on a presence/absence edge): Wavr's
  // fusion decays a modality's trust over a freshness window (fusion.py,
  // ~90s default) if it stops hearing from that source, so an edge-only PIR
  // would have a long, still-true "presence" reading silently decay to zero
  // trust while the person is still sitting in the room. A continuous
  // low-rate report of the CURRENT state avoids that -- it mirrors how the
  // LD2450 module itself free-runs and keeps re-reporting while a target is
  // in view.
  doc["presence"] = _presence;
  doc["motion"] = _presence ? 1.0 : 0.0;
}
