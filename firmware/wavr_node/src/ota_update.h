#pragma once
// Local-network-only OTA hook (ArduinoOTA / espota, UDP discovery + TCP port
// 3232, both LAN-local). No internet, no cloud update server -- an operator
// on the same LAN pushes a new build with
//   pio run -e esp32dev-ota -t upload
// gated by the password in config.h. main.cpp calls OtaUpdate::begin() once
// after Wi-Fi + enrollment succeed, and OtaUpdate::tick() every loop().
namespace OtaUpdate {
void begin();
void tick();
}  // namespace OtaUpdate
