// A serial port, read-only, for a radar attached by USB-UART. Platform code
// lives behind this so the node loop is the same everywhere.
#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>

namespace wavr {

class SerialPort {
 public:
  virtual ~SerialPort() = default;
  // Up to `len` bytes, waiting at most `timeout_ms`. 0 = nothing arrived;
  // negative = the port failed (unplugged).
  virtual long read(uint8_t* buf, size_t len, int timeout_ms) = 0;
};

// "COM3" / "/dev/ttyUSB0" at `baud`, 8N1. nullptr + error on failure, or where
// the platform cannot set that rate.
std::unique_ptr<SerialPort> open_serial(const std::string& name, int baud, std::string* error);

}  // namespace wavr
