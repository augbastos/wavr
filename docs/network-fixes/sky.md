# Sky router — fixing discovery

Applies to the **Sky Hub** and **Sky Broadband Hub / Sky Max Hub** (Sagemcom; default admin at
`http://192.168.0.1`).

**When Wavr points here:** `AP_ISOLATION_OR_MDNS_FILTERING` or `SECOND_NETWORK_VLAN`.

## 1. Check Sky Broadband Shield first (it's not isolation, but people confuse it)

**Sky Broadband Shield** is content filtering, not client isolation — it won't block LAN
discovery. If you were about to disable it hoping to fix Wavr, that's the wrong lever; leave your
filtering as you like it and use the steps below.

## 2. Client isolation / guest network

- Older **Sky Hubs** lock down the admin UI and expose almost no wireless toggles — there's often
  no isolation switch to flip. If discovery fails on a locked-down Sky Hub, the most reliable fix
  is to **put your own router/AP behind it** (Sky Hub in effect as modem) and manage isolation
  there.
- **Sky Broadband Hub / Max Hub** expose more under **Advanced settings → Wireless**. Look for a
  guest network and make sure Wavr + devices are on the **main** SSID, not guest.

## 3. Same subnet

Sky Hubs keep 2.4/5 GHz on one subnet, so band doesn't split reachability. If you run a **mesh or
second router**, that's where a second subnet/VLAN would come from — put Wavr on the same network
as the devices, or bridge the extra hop.

## 4. If nothing is toggle-able

Sky's stock firmware is deliberately minimal. When it exposes no isolation control and discovery
still fails, the practical path is your **own AP/router downstream** — then follow that device's
guide ([TP-Link](tp-link.md), [UniFi](unifi.md)).

---
See also: [wavr-network-requirements.md](wavr-network-requirements.md) ·
[known-limitations.md](known-limitations.md).
