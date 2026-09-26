# ESP32 Node hardware check

Run from `firmware/wavr_node` with a connected board. PlatformIO downloads its
pinned toolchain on the first build. These commands have **not** been run on a
physical board in this repository.

| Environment | Build | Upload | Serial monitor |
|---|---|---|---|
| LD2450 (`esp32dev`) | `pio run -e esp32dev` | `pio run -e esp32dev -t upload` | `pio run -e esp32dev -t monitor` |
| PIR (`esp32dev-pir`) | `pio run -e esp32dev-pir` | `pio run -e esp32dev-pir -t upload` | `pio run -e esp32dev-pir -t monitor` |
| OTA (`esp32dev-ota`) | `pio run -e esp32dev-ota` | `pio run -e esp32dev-ota -t upload` | `pio run -e esp32dev-ota -t monitor` |

For the OTA environment, first flash over USB and replace the placeholder
`upload_port` in `platformio.ini` with the node's actual LAN address. Keep its
`--auth` setting in sync with `WAVR_OTA_PASSWORD` in `include/config.h`.
For the host unit test, run `pio test -e native` as well.

## Enroll and validate

1. Wire the LD2450 and kill button per [NODE-ONBOARDING.md](../../docs/NODE-ONBOARDING.md):
   radar TX to ESP32 GPIO16, radar RX to GPIO17 (level shift if needed), 5V
   and common ground. PIR uses GPIO27 for OUT. Check the module logic level.
2. Start a Core with `WAVR_MULTIDEVICE=1` and `WAVR_NODES_ENABLED=1` and its
   LAN TLS URL. In the Core's **Nodes → Add a node** screen choose the name,
   existing room, and `ld2450` or `pir` sensor type; copy the one-time code.
3. Flash, power and monitor the board. Join its `wavr-node-XXXX` SoftAP, open
   the captive portal and enter Wi-Fi, the Core HTTPS URL and the code within
   five minutes. Compare the certificate fingerprint printed by the node with
   the Core's serving certificate out of band before trusting the node.
4. Confirm the Nodes panel shows it **active** and live telemetry reaches the
   chosen room. A radar can report zero targets when nobody is in view;
   validate with a person moving in its field of view.
5. In the Core, disable the node. Confirm telemetry is rejected/stops and the
   board stops sensing after its next heartbeat. A remote OFF must never turn
   back ON by itself; a short **physical** button press is required to
   reactivate. A hold of at least three seconds factory resets it.

**PASS** requires build and upload success, visible enrollment on the Core,
telemetry reaching the selected room, and the physical kill switch behavior
above. A build-only result is not a hardware PASS. If any check cannot be
observed, report **CANNOT TEST** with the missing device or evidence.
