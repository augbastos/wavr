#pragma once
#include <HardwareSerial.h>
#include "sensor_driver.h"

// HLK-LD2450 24GHz mmWave position radar (first target sensor). Forwards RAW
// 30-byte report frames as hex -- Wavr parses them SERVER-SIDE with the
// already-tested parse_ld2450_frame (see wavr/nodes.py / wavr/sources/mmwave.py)
// so the wire parser has exactly one implementation instead of a firmware
// copy that can silently drift from the backend's.
class Ld2450Driver : public SensorDriver {
 public:
  Ld2450Driver(HardwareSerial& serial, int rxPin, int txPin, uint32_t baud)
      : _serial(serial), _rxPin(rxPin), _txPin(txPin), _baud(baud) {}

  void begin() override;
  void poll() override;
  bool hasReading() override { return _haveFrame; }
  void buildTelemetry(JsonDocument& doc) override;
  const char* sensorTypeHint() override { return "ld2450"; }

 private:
  HardwareSerial& _serial;
  int _rxPin, _txPin;
  uint32_t _baud;
  uint8_t _buf[64] = {0};
  int _len = 0;
  char _frameHex[61] = {0};   // 30 raw bytes -> 60 hex chars + NUL
  bool _haveFrame = false;
};
