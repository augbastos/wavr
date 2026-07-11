#include "ld2450_driver.h"
#include <Arduino.h>   // snprintf via the core's stdio chain -- .cpp files under
                        // src/ get no implicit Arduino.h the way .ino files do

void Ld2450Driver::begin() {
  _serial.begin(_baud, SERIAL_8N1, _rxPin, _txPin);
}

void Ld2450Driver::poll() {
  while (_serial.available()) {
    uint8_t b = _serial.read();
    if (_len < (int)sizeof(_buf)) _buf[_len++] = b;
    // A complete LD2450 report frame is 30 bytes: header AA FF 03 00, three
    // 8-byte target slots, footer 55 CC. We scan the buffer for a header once
    // we see the footer land at the end, matching the module's fixed-length
    // framing (see NODE_PROTOCOL.md's telemetry section).
    if (_len >= 30 && _buf[_len - 2] == 0x55 && _buf[_len - 1] == 0xCC) {
      for (int i = 0; i + 30 <= _len; i++) {
        if (_buf[i] == 0xAA && _buf[i + 1] == 0xFF && _buf[i + 2] == 0x03 &&
            _buf[i + 3] == 0x00) {
          for (int j = 0; j < 30; j++) {
            snprintf(_frameHex + j * 2, 3, "%02x", _buf[i + j]);
          }
          _haveFrame = true;
          break;
        }
      }
      _len = 0;   // frame consumed (or the buffer held garbage) -- reset for the next one
    }
  }
}

void Ld2450Driver::buildTelemetry(JsonDocument& doc) {
  JsonArray frames = doc.createNestedArray("ld2450_frames");
  if (_haveFrame) frames.add(_frameHex);
  _haveFrame = false;
}
