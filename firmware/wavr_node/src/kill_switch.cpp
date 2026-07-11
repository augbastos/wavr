#include "kill_switch.h"
#include "config.h"

static const uint32_t kDebounceMs = 30;   // tune per switch if yours is noisier

static bool lastRawState = false;
static uint32_t lastChangeMs = 0;
static bool wasDown = false;
static uint32_t downSinceMs = 0;
static bool resetFired = false;

void KillSwitch::begin() {
  pinMode(WAVR_KILL_SWITCH_PIN, INPUT_PULLUP);
}

KillSwitch::Event KillSwitch::poll() {
  bool raw = (digitalRead(WAVR_KILL_SWITCH_PIN) == LOW);
  uint32_t now = millis();

  if (raw != lastRawState) {
    lastRawState = raw;
    lastChangeMs = now;
  }
  // Only trust the raw read once it has been stable for kDebounceMs; until
  // then keep reporting whatever the last debounced state was.
  bool debounced = (now - lastChangeMs >= kDebounceMs) ? raw : wasDown;

  Event ev = Event::kNone;
  if (debounced && !wasDown) {
    downSinceMs = now;
    resetFired = false;
  } else if (debounced && wasDown && !resetFired &&
             now - downSinceMs >= WAVR_FACTORY_RESET_MS) {
    resetFired = true;   // fire exactly once per hold, however long it continues
    ev = Event::kFactoryReset;
  } else if (!debounced && wasDown && !resetFired) {
    ev = Event::kShortPress;   // released before crossing the reset threshold
  }

  wasDown = debounced;
  return ev;
}
