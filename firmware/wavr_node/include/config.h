#pragma once
// ---------------------------------------------------------------------------
// Wavr sensor node - board wiring + build-time configuration.
//
// Edit the values below to match your actual wiring/deployment; nothing else
// in this firmware needs to change to move a pin, retime a loop, or swap the
// sensor driver. See firmware/README.md for the flash + wiring walkthrough
// and firmware/NODE_PROTOCOL.md for the wire contract this firmware speaks.
// ---------------------------------------------------------------------------

// -- HLK-LD2450 mmWave radar (UART2) -----------------------------------------
// LD2450 TX -> ESP32 GPIO16 (RX2), LD2450 RX -> ESP32 GPIO17 (TX2).
// The LD2450 module ships as a 5V part on most breakout boards; the ESP32's
// UART pins are NOT 5V tolerant -- use a 3.3V-logic variant or a level
// shifter on RX. Power the LD2450 from 5V per its datasheet regardless.
#define WAVR_LD2450_RX_PIN   16
#define WAVR_LD2450_TX_PIN   17
#define WAVR_LD2450_BAUD     256000

// -- Kill-switch input (physical button OR jumper) ---------------------------
// Defaults to the onboard BOOT button (GPIO0, active-LOW via internal
// pull-up -- no external resistor needed). To use an external momentary
// button or a bare 2-pin jumper instead: wire it between this GPIO and GND
// and pick any free GPIO that is NOT a strapping pin (0/2/5/12/15 are
// sampled at boot on most ESP32 modules). A jumper works identically to a
// button here -- briefly bridging it to GND reads the same as a press.
//
// Short press  (< WAVR_FACTORY_RESET_MS) -> physical reactivate. This is the
//   ONLY disabled -> active edge in the whole system (see NODE_PROTOCOL.md);
//   Wavr has no remote "enable".
// Long hold    (>= WAVR_FACTORY_RESET_MS) -> factory reset: wipes NVS
//   (Wi-Fi creds, Wavr URL, token) and reboots back into SoftAP setup.
#define WAVR_KILL_SWITCH_PIN     0
#define WAVR_FACTORY_RESET_MS    3000UL

// -- Status LED ---------------------------------------------------------------
// Onboard LED on most ESP32 DevKitC-style boards is GPIO2, active-HIGH. Set
// WAVR_LED_ACTIVE_LOW to 1 if your board's LED (or an external one you wired)
// is active-low.
#define WAVR_LED_PIN            2
#define WAVR_LED_ACTIVE_LOW     0

// -- Timing --------------------------------------------------------------------
#define WAVR_HEARTBEAT_MS            30000UL   // poll the kill-switch command
#define WAVR_HEARTBEAT_DISABLED_MS   120000UL  // "low-power poll": slower cadence
                                                // used while remote-disabled
                                                // (sleep) -- less radio/HTTPS
                                                // activity until a physical
                                                // kill-switch press brings the
                                                // node back to WAVR_HEARTBEAT_MS
#define WAVR_TELEMETRY_MS            1000UL    // sensor -> Wavr cadence
#define WAVR_WIFI_CONNECT_TIMEOUT_MS 20000UL   // first-boot join attempt

// -- OTA (local network only: ArduinoOTA/espota, never internet/cloud) --------
// Hostname advertised is WAVR_OTA_HOSTNAME_PREFIX + the last 2 MAC bytes
// (matches the SoftAP name scheme, e.g. "wavr-node-a1b2"). Change the
// password per deployment and keep it in sync with `--auth=` in
// platformio.ini's [env:esp32dev-ota].
#define WAVR_OTA_HOSTNAME_PREFIX "wavr-node-"
#define WAVR_OTA_PASSWORD        "wavr-node-ota"

// -- Sensor driver selection ----------------------------------------------------
// One firmware image = one sensor: the operator commits to a sensor_type at
// flash time, matching what they declare on Wavr's *Add a node* screen (the
// node never gets to choose its own room/modality -- see NODE_PROTOCOL.md).
// To ship a PIR node instead of the default LD2450 build, compile with
// `-D WAVR_SENSOR_DRIVER=WAVR_SENSOR_PIR` (see platformio.ini's
// [env:esp32dev-pir]) rather than editing this file.
#define WAVR_SENSOR_LD2450 1
#define WAVR_SENSOR_PIR    2
#ifndef WAVR_SENSOR_DRIVER
#define WAVR_SENSOR_DRIVER WAVR_SENSOR_LD2450
#endif

// -- PIR driver pin (only wired up when WAVR_SENSOR_DRIVER == WAVR_SENSOR_PIR) --
// HC-SR501-class module OUT pin -> this GPIO. Powered from 5V or 3.3V
// depending on the module; OUT is a 3.3V logic signal on the common
// HC-SR501 variant, safe straight into the ESP32.
#define WAVR_PIR_PIN 27
