#pragma once
#include <Arduino.h>

// Debounced physical input. Wired either as the onboard BOOT button or an
// external momentary button/jumper to GND (see config.h) -- both read
// identically here (active-LOW with an internal pull-up).
//
// This is the ONLY path back from a remote-disabled node to active (see
// NODE_PROTOCOL.md's kill-switch state machine): Wavr can disable a node
// remotely, but re-enabling it requires someone physically AT the node.
namespace KillSwitch {

enum class Event { kNone, kShortPress, kFactoryReset };

void begin();

// Call every loop() iteration (non-blocking). Returns kShortPress once per
// press shorter than WAVR_FACTORY_RESET_MS -- the caller should bump its own
// press_count and call WavrClient::sendReactivate(). Returns kFactoryReset
// once a hold has crossed that threshold -- the caller decides what "factory
// reset" means (this module never touches NVS/reboots itself), keeping
// main.cpp the single place that owns that decision.
Event poll();

}  // namespace KillSwitch
