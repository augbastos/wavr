# TP-Link — fixing discovery

Applies to **TP-Link Archer** routers (web admin at `http://192.168.0.1` or `http://tplinkwifi.net`)
and **TP-Link Deco** mesh (managed in the Deco app).

**When Wavr points here:** `AP_ISOLATION_OR_MDNS_FILTERING` or `SECOND_NETWORK_VLAN`.

## Archer (web UI)

### 1. Turn off AP Isolation
- **Advanced → Wireless → Wireless Settings**.
- Untick **"AP Isolation"** for each band (2.4 GHz and 5 GHz have separate checkboxes).
- Save and re-run Wavr's diagnosis.

### 2. Guest network
- **Advanced → Guest Network**: guest clients are isolated from the LAN. Keep Wavr + your devices
  on the **main** network, not the guest SSID.

## Deco (app)

### 1. IoT Network = a separate, isolated network
Deco's **IoT Network** feature puts smart devices on their own isolated segment — great for
security, fatal for cross-device discovery. If your sensors are on the Deco IoT network and Wavr
isn't (or vice-versa), they can't see each other.
- Deco app → **More → IoT Network**. Either turn it off, or make sure **both** Wavr's host and the
  devices you want detected are on the **same** network (both on IoT, or both on main).

### 2. Client isolation
- Deco app → **More → Advanced → (look for) Client Isolation / Access Control**. Recent Deco
  firmware hides a global AP-isolation toggle; if you can't find one, the IoT-network split above
  is almost always the real cause.

## 3. Multicast / mDNS
TP-Link passes multicast on the main network by default. On Archer, avoid enabling any
"IGMP Snooping" strict mode without a querier if discovery dies minutes after it starts — see
[known-limitations.md](known-limitations.md).

---
See also: [wavr-network-requirements.md](wavr-network-requirements.md).
