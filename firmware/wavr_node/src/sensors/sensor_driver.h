#pragma once
#include <ArduinoJson.h>

// Every sensor driver (LD2450, PIR, and any future BLE/environmental driver)
// implements this tiny interface. main.cpp only ever talks to a
// SensorDriver*, so adding a new sensor is "write a driver + flip
// WAVR_SENSOR_DRIVER" -- provisioning, enrollment, heartbeat, the
// kill-switch, OTA, and the status LED are all sensor-agnostic and never
// change when a new driver is added.
class SensorDriver {
 public:
  virtual ~SensorDriver() {}

  // Called once from setup(), after Wi-Fi/enrollment succeed.
  virtual void begin() = 0;

  // Called every loop() iteration. Drivers that read a UART/GPIO should do it
  // here (non-blocking) and cache the latest reading internally; do not
  // block or delay() inside poll().
  virtual void poll() = 0;

  // True if there is a current reading worth sending. main.cpp only POSTs
  // telemetry when this is true AND the WAVR_TELEMETRY_MS timer has elapsed.
  // A driver whose sensor free-runs (like LD2450, which streams frames
  // continuously) can return true only when a fresh frame arrived; a driver
  // that samples a static GPIO (like PIR) should return true on every call
  // once begin() has run, so Wavr's freshness-decay fusion window (see
  // fusion.py) always sees a recent read instead of one stale "presence"
  // reading that ages out while the person is still there.
  virtual bool hasReading() = 0;

  // Fills `doc` with the sensor-specific telemetry fields documented in
  // NODE_PROTOCOL.md (e.g. "ld2450_frames" for LD2450, or
  // "presence"/"motion"/"targets" for a decoded sensor). Must NOT set "seq"
  // -- WavrClient owns the monotonic seq counter and stamps it itself.
  virtual void buildTelemetry(JsonDocument& doc) = 0;

  // Short machine name matching wavr/nodes.py's SENSOR_MODALITY map (ld2450,
  // pir, ble_beacon, generic, environmental). Purely informational on the
  // node side for logging -- the operator's choice on Wavr's *Add a node*
  // screen is what actually decides room/modality/trust, never anything the
  // node self-reports (see NODE_PROTOCOL.md's anti-spoof rules).
  virtual const char* sensorTypeHint() = 0;
};
