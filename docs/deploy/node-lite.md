# Deploy the native Node on Linux and OpenWrt

Node Lite is the native `wavr node` role. It sends sensor evidence to a
separate Wavr Core; it is not a reduced Core. The Core must have
`WAVR_MULTIDEVICE=1` and `WAVR_NODES_ENABLED=1`, a reachable local TLS URL,
and an operator at its Nodes panel. The Node pins the Core certificate on
first enrollment and sends its bearer token only to that pinned certificate
afterward. Compare the first pin with the Core out of band.

## Check the hardware first

Use a binary built for the exact CPU architecture and ABI. Release packages
contain `manifest.json` and `SHA256SUMS`; verify the archive locally before
installation. No installation command here downloads or runs remote code.

```sh
WAVR_BIN=/path/to/wavr sh scripts/hw/check_node_linux.sh
python3 scripts/hw/check_ld2450.py --port /dev/ttyUSB0 --seconds 10 --min-frames 3
```

Both checks put a one-line PASS/FAIL/CANNOT TEST verdict first, followed by
evidence. The Python radar probe requires the optional `pyserial` mmwave extra
and `PYTHONPATH` pointing to this checkout's `backend` directory; it imports
the product's `take_ld2450_frame` and `parse_ld2450_frame`. Zero targets in
valid frames is a PASS when the field of view is empty. `check_node_linux.sh`
can also accept a Core URL and certificate pin as its two arguments to run
`wavr status`; remote status may need an operator-provided token in
`WAVR_LOCAL_TOKEN`. Never put a token into command history or diagnostic logs.

The UWB Core route brokers session parameters; it does not read a radio or
accept a range measurement. If you have a *local* Nearby Interaction bridge
that exposes a JSON reading (`discovery_token`, `distance_m`, optional
`direction`) over HTTP, run `python3 scripts/hw/check_uwb.py --source
http://127.0.0.1:PORT/reading`. The probe uses the Core's existing Apple
reading parser. A reachable broker session alone is not a UWB reading PASS, and
a PASS proves only that the bridge produces a parseable reading -- not that the
Core ingests it (UWB readings reach fusion through the provider ingest route).

ESP32 and Android TV checklists are in [esp32-node.md](../../scripts/hw/esp32-node.md)
and [android-tv.md](../../scripts/hw/android-tv.md).

## Linux, Pi or another SBC with systemd

The unit in `native/packaging/linux/wavr-node.service` runs as a dedicated
`wavr-node` user, stores its token-bearing state under `/var/lib/wavr-node`,
limits writable paths, and allows a single serial device. It will restart on
ordinary failure, but **never** on exit code 3 (revoked). The default serial
device is `/dev/ttyUSB0`. Adjust both `ExecStart` and `DeviceAllow` with
`systemctl edit wavr-node` if the sensor appears elsewhere. The service user
must have serial access through `dialout` or the actual device's group.

```sh
sudo sh native/packaging/linux/install-node.sh /path/to/verified/wavr
sudo -u wavr-node /usr/local/bin/wavr node enroll \
  --url https://CORE:PORT --code ONE_TIME_CODE \
  --state /var/lib/wavr-node/node.json
sudo systemctl enable --now wavr-node.service
sudo systemctl status wavr-node.service
sudo journalctl -u wavr-node.service -n 50
```

`install-node.sh` copies the binary and unit, creates the service account and
state directory, and reloads systemd. It does not enable or start the unit.
Mint the one-time code in the Core's **Nodes → Add a node** screen, selecting
the real room and sensor type. Run enrollment with a protected terminal: the
code appears briefly in process arguments and shell history. Confirm the
certificate fingerprint printed by enrollment matches the Core's certificate
before starting the service. `wavr node status --state
/var/lib/wavr-node/node.json` shows state without showing the bearer token.
Test disable/reactivate with the native CLI's explicit `wavr node reactivate
--state /var/lib/wavr-node/node.json` command after disabling in the Core.
Unlike the ESP32 firmware's button, the native CLI has no physical button
gate; protect shell access to the state file.

## OpenWrt

Read [the feasibility and install notes](../../native/packaging/openwrt/README.md).
Copy the matching static musl binary and `wavr-node.init` to the router,
configure the serial path, enroll into `/overlay/wavr-node/node.json`, then
enable/start the procd service. The script does not respawn a revoked node.
The Node's ~6–9 MB measured RSS elsewhere is not a router measurement; confirm
free flash, RAM, USB serial support and long-term behavior on the target.

## Result interpretation

`PASS` means the requested local probe met its threshold. `FAIL` means the
probe ran but its evidence did not meet it. `CANNOT TEST` means a binary,
device, parser, optional tool or reading source is unavailable. Exit codes are
0, 1 and 2 respectively for the automated probes. Hardware validation is
pending until the owner runs these commands on the target devices.
