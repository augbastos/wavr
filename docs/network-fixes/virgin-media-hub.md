# Virgin Media Hub — fixing discovery

Applies to Virgin Media **Hub 3 / Hub 4 / Hub 5** (default admin at `http://192.168.0.1`).

**When Wavr points here:** `AP_ISOLATION_OR_MDNS_FILTERING` or `SECOND_NETWORK_VLAN` — Wavr can see
devices on the LAN but they don't answer name-discovery.

## 1. Don't run Wavr (or the devices) on the Guest network

Virgin Hubs isolate the **Guest Wi-Fi** by design — clients on it can't see each other, which
kills discovery. This is the most common Virgin cause.

- Admin → **Advanced settings → Wireless → Guest network**.
- Make sure Wavr's host and the devices you want detected are on the **main** SSID, not the guest
  one. (Guest isolation is a feature; just don't put your smart-home on it.)

## 2. Main-network client isolation

The Hub in **router mode** doesn't expose a classic per-client "AP isolation" toggle for the main
network — multicast/mDNS normally passes there. If discovery still fails on the main network and
the guide above didn't help, the isolation is almost certainly coming from something *else* on
the path (a mesh extender, a powerline adapter, or a second router), not the Hub itself.

## 3. Modem mode + your own router

If you run the Hub in **modem mode** (Advanced settings → Modem mode) behind your own router,
then *your router's* isolation / VLAN / guest rules are in charge — follow that device's guide
([TP-Link](tp-link.md), [UniFi](unifi.md), etc.), not this one.

## 4. Band steering

Virgin Hubs use one SSID for 2.4/5 GHz with steering. Cheap IoT sensors and BLE nodes that only
do 2.4 GHz can get stranded. Hub 3 lets you split the bands (give 2.4 GHz its own SSID); Hub 4/5
hide this — if a 2.4 GHz-only device won't stay visible, that's the likely reason.

---
See also: [wavr-network-requirements.md](wavr-network-requirements.md) ·
[known-limitations.md](known-limitations.md). Exact menu labels vary by firmware — match on the
*words* (isolation, guest, modem mode), not the exact path.
