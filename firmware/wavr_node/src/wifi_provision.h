#pragma once
#include <Arduino.h>

// SoftAP + captive portal, used ONLY on a fresh/unprovisioned node, plus a
// non-blocking Wi-Fi reconnect watchdog for after that. Collects home Wi-Fi
// credentials, the Wavr LAN base URL, and a one-time enrollment code,
// persists them to NVS, then reboots into normal operation -- the node is
// never "half online" during setup.
namespace WifiProvision {

// True once NVS holds {ssid, pass, url, provisioned=true} from a prior run.
bool isProvisioned();

// Blocks forever serving the captive portal; reboots on submit. Call this
// from setup() when !isProvisioned(). Never returns.
void runPortal();

// Joins the stored Wi-Fi network (used once at boot, after provisioning).
// Returns false on timeout -- caller should back off and retry, NOT
// re-provision (only a factory reset clears stored credentials).
bool joinStoredWifi();

// Non-blocking watchdog: call every loop() iteration once joinStoredWifi()
// has succeeded once. Reconnects with exponential backoff on a drop (router
// reboot, AP restart, RF glitch...) and, as a last resort, restarts the MCU
// if it has been unreachable past a long ceiling -- some Wi-Fi driver states
// only clear on reboot. This is what lets a node survive a router reboot
// with zero physical intervention.
void maintainConnection();

}  // namespace WifiProvision
