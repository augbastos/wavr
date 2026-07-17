# Network fixes — when Wavr can't discover your devices

Wavr's network doctor (`discovery_reach`) can tell you *that* discovery is failing and offer a
best-effort *hypothesis* about why (never a CONFIRMED verdict — ADR-0003). When the cause points
at your network, these guides show where to fix it.

## Start here

- **[wavr-network-requirements.md](wavr-network-requirements.md)** — the five things Wavr needs
  from your LAN, and a 2-minute self-check. Read this first.
- **[known-limitations.md](known-limitations.md)** — where the diagnosis honestly under-claims
  (why it says "can't confirm" instead of blaming your router).

## Per-router guides

| Router | Status |
|--------|--------|
| [Virgin Media Hub](virgin-media-hub.md) | complete |
| [eir](eir.md) | complete |
| [Sky](sky.md) | complete |
| [TP-Link](tp-link.md) | complete |
| [Vodafone](vodafone.md) | field-verified (Ultra Hub, IE) — stock firmware exposes no isolation/multicast toggles |
| [UniFi](unifi.md) | stub — help wanted |

## How the doctor's cause maps to a fix

| Cause (from the verdict) | What it means | Guide section |
|--------------------------|---------------|---------------|
| `AP_ISOLATION_OR_MDNS_FILTERING` | Hub receives multicast, devices don't answer → the network is filtering discovery | "Client / AP isolation" in your router's guide |
| `SECOND_NETWORK_VLAN` | Devices span more than one network / DHCP server | "Same subnet / VLAN" in your router's guide |
| `HOST_MULTICAST_UNAVAILABLE` | The hub Wavr runs on isn't receiving LAN multicast (often a container) | Run the Core outside the container, or check AP isolation — [requirements](wavr-network-requirements.md) |
| `MULTICAST_DEAD_UNKNOWN` | Discovery is silent but the cause couldn't be confirmed | Work through the [requirements](wavr-network-requirements.md) self-check |

Your exact firmware may label things differently — the guides name **what** to look for
(isolation, guest, mDNS, VLAN), not just where.
