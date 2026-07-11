#pragma once
#include <Arduino.h>

// Non-blocking status LED. Call StatusLed::set(state) whenever the node's
// state changes and StatusLed::tick() once per loop() -- it owns all the
// blink timing so nothing else in this firmware needs delay().
namespace StatusLed {

enum class State {
  kProvisioning,   // SoftAP portal open, waiting for the operator -- fast blink
  kConnecting,     // joining Wi-Fi / enrolling -- slow blink
  kActive,         // sensing, telemetry flowing -- brief heartbeat blip, mostly off
  kDisabled,       // remote-OFF (kill-switch) -- slow pulse
  kError,          // unreachable / enrollment failed -- rapid blink
};

void begin();
void set(State s);
void tick();

}  // namespace StatusLed
