#include "status_led.h"
#include "config.h"

static StatusLed::State current = StatusLed::State::kConnecting;
static uint32_t lastToggleMs = 0;
static bool ledOn = false;

static void writeLed(bool on) {
  bool level = WAVR_LED_ACTIVE_LOW ? !on : on;
  digitalWrite(WAVR_LED_PIN, level ? HIGH : LOW);
  ledOn = on;
}

void StatusLed::begin() {
  pinMode(WAVR_LED_PIN, OUTPUT);
  writeLed(false);
}

void StatusLed::set(State s) { current = s; }

void StatusLed::tick() {
  uint32_t now = millis();

  // kActive uses a short duty-cycle blip instead of a 50% blink, so a
  // healthy node reads as "quiet", not as busy as the setup/fault states.
  if (current == State::kActive) {
    bool shouldOn = (now % 2000) < 60;
    if (shouldOn != ledOn) writeLed(shouldOn);
    return;
  }

  uint32_t period;
  switch (current) {
    case State::kProvisioning: period = 150;  break;   // fast: "waiting for you"
    case State::kConnecting:   period = 500;  break;   // slow: "working on it"
    case State::kDisabled:     period = 1500; break;   // slow pulse: "off, but fine"
    case State::kError:        period = 100;  break;   // rapid: "something's wrong"
    default:                   period = 1000; break;
  }
  if (now - lastToggleMs >= period) {
    lastToggleMs = now;
    writeLed(!ledOn);
  }
}
