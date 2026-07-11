#pragma once
#include "sensor_driver.h"

// HC-SR501-class passive-IR motion sensor. A concrete, working alternative to
// LD2450 -- proof that the SensorDriver seam actually holds, not a stub.
// Reports coarse presence/motion only: PIR is deliberately NEVER allowed to
// assert a person count (see COUNTING_SENSORS in wavr/nodes.py -- only
// radar-class sensors may count discrete targets).
class PirDriver : public SensorDriver {
 public:
  explicit PirDriver(int pin) : _pin(pin) {}

  void begin() override;
  void poll() override;
  bool hasReading() override { return true; }   // see .cpp: reported every tick, not just on change
  void buildTelemetry(JsonDocument& doc) override;
  const char* sensorTypeHint() override { return "pir"; }

 private:
  int _pin;
  bool _presence = false;
  uint32_t _lastMotionMs = 0;
};
