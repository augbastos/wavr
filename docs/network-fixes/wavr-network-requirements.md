# Wavr — minimum network requirements

Wavr senses presence by *seeing devices on your LAN* (ARP / mDNS / SSDP / DHCP / NetBIOS / SNMP)
and, optionally, BLE / mmWave radar / cameras. If the network hides devices from each other,
Wavr goes blind — through no fault of its own. These are the conditions under which Wavr can
actually see your home.

## The five requirements

| # | Requirement | Why Wavr needs it | Symptom when it's wrong |
|---|-------------|-------------------|-------------------------|
| 1 | **Client / AP isolation OFF** | Isolation blocks device-to-device traffic, which kills mDNS/multicast and ARP visibility — the backbone of discovery. | Wavr sees almost nothing, or only the router. Discovery "just doesn't work." **(the #1 cause)** |
| 2 | **Multicast / mDNS allowed** | mDNS (Bonjour) + SSDP are how phones, TVs, printers, and IoT announce themselves. Many "guest" or "IoT-hardened" configs drop multicast. | Named devices never resolve; the list stays anonymous MACs. |
| 3 | **Wavr + your devices on the same subnet / VLAN** | Wavr can only fuse what it can reach. An IoT VLAN your phone can't route into makes the sensors invisible to the Wavr host, and vice-versa. | Sensors are "online" on the router but Wavr never lists them. |
| 4 | **2.4 GHz reachable (not force-steered to 5 GHz)** | BLE proximity + most IoT/mmWave nodes live on 2.4 GHz. Aggressive band-steering can strand them. | BLE presence and cheap sensors flicker or disappear. |
| 5 | **A router that isn't saturated** | On a crowded LAN (>~30 chatty devices) scan responses get dropped; detection becomes intermittent. | Presence flaps: "home", then "away", then "home". |

## Quick self-check (2 minutes)

1. In your router admin, look for **"AP isolation" / "client isolation" / "guest network isolation"** → make sure it's **off** for the network Wavr and your devices are on.
2. Confirm Wavr's host and the devices you care about are on the **same Wi-Fi/SSID and subnet** (not a separate "IoT" or "Guest" network).
3. If your router force-uses 5 GHz, keep a **2.4 GHz SSID** available for sensors/IoT.
4. Reboot the router if it's been up for months and is juggling a lot of devices.

## Honest scope

- Wavr is **local-only**: it never phones home. That also means it can only work with what your
  LAN exposes to it — it can't reach around an isolating router.
- None of the above is a Wavr bug; they're properties of *your* network. But they are the single
  biggest reason a fresh install "sees nothing," so getting them right is step zero.
- A misconfigured network can't be detected as such from inside Wavr with certainty — the app can
  only observe "few/no devices found" and *suggest* these checks. **Any auto-diagnosis of a
  network pathology is a hypothesis to confirm, not a verdict** (ADR-0003). See
  [known-limitations.md](known-limitations.md) for exactly where the diagnosis under-claims.

## Per-router guides

If Wavr's diagnosis points at your network, the brand guides in this folder show where the
relevant settings live: [Virgin Media Hub](virgin-media-hub.md) · [Eir](eir.md) · [Sky](sky.md)
· [TP-Link](tp-link.md) · [Vodafone](vodafone.md) · [UniFi](unifi.md). Your exact firmware may
label things slightly differently — the guides name what to look for, not just where.
