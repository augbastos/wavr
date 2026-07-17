# eir router — fixing discovery

Applies to **eir F3000 / F2000** and similar eir-supplied routers (default admin usually at
`http://192.168.1.254`, some models `http://192.168.1.1`; login is the printed admin password).

**When Wavr points here:** `AP_ISOLATION_OR_MDNS_FILTERING` or `SECOND_NETWORK_VLAN`.

## 1. Turn off client / AP isolation

- Admin → **Wi-Fi → Advanced** (may be under **Network → WLAN → Advanced**).
- Look for **"Isolate clients"**, **"AP isolation"**, or **"Client isolation"** and switch it
  **off** for the band(s) your devices use.
- Apply and re-run Wavr's diagnosis.

## 2. Get everything off the Guest Wi-Fi

The eir **Guest network** is isolated by default (that's its job). If Wavr's host or your smart
devices are on the guest SSID, discovery can't cross it.

- Admin → **Wi-Fi → Guest network** → confirm it's separate, and move Wavr + devices to the main
  SSID.

## 3. Same subnet for host and sensors

eir routers keep 2.4 GHz and 5 GHz on the **same subnet** by default, so a device on either band
is reachable — good for Wavr. If you added a **second router or mesh** behind the eir box, that
device may be handing out a different subnet; put Wavr on the same one as the sensors, or bridge
the second router.

## 4. Multicast / mDNS

eir firmware generally passes multicast on the main network. If names still never resolve after
steps 1–3, it's more likely the devices themselves don't announce over mDNS/SSDP than an eir
filter — see [known-limitations.md](known-limitations.md).

---
See also: [wavr-network-requirements.md](wavr-network-requirements.md). Firmware revisions move
these menus around — match on the words, not the exact path.
