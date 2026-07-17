# Vodafone router — fixing discovery

Applies to **Vodafone Broadband** routers — the **Ultra Hub** (Ireland), THG3000 / Vodafone
Station variants (admin usually at `http://192.168.1.1`; login on the sticker under/behind the
router, typically user `vodafone` + the printed password).

**When Wavr points here:** `AP_ISOLATION_OR_MDNS_FILTERING` or `SECOND_NETWORK_VLAN`.

## The honest headline: there may be nothing to toggle

**Field-verified on an Ultra Hub (Ireland, 2026-07):** the stock firmware exposes essentially
**none** of the classic switches — no "AP isolation" / "client isolation" checkbox, no
mDNS/multicast filter setting, no IGMP controls in the user-visible UI. If you've hunted the
menus and found nothing, **you didn't miss it — it isn't there.** This is common on ISP-locked
firmware, and it's exactly why Wavr's diagnosis never *demands* router access to be useful.

What that means in practice:

- **You can't turn the relevant filters on or off yourself.** Whatever multicast behavior the
  hub has is baked into the firmware.
- A "your network is blocking discovery" verdict on this hub can't be confirmed or fixed from
  the router UI. The two useful moves are below.

## 1. What you CAN check

- **Guest Wi-Fi**: the guest network isolates clients by design. Make sure Wavr's host and the
  devices you want discovered are all on the **main** SSID. (This one usually *is* visible in
  the Vodafone app / web UI.)
- **One network, not two**: if you added mesh units or a second router behind the hub, that's
  where a second subnet/DHCP usually comes from — keep Wavr on the same network as the devices.

## 2. The escalation path (advanced users)

If discovery genuinely stays dead on the main network and you need it, the practical route on
ISP-locked hubs is **your own router or access point behind the Vodafone hub** (ideally with the
hub in bridge/modem mode if your plan allows it, or with the hub's Wi-Fi turned off). Then follow
your own router's guide ([TP-Link](tp-link.md), [UniFi](unifi.md)) — those actually expose the
switches.

## 3. Don't confuse these

- The Vodafone app's parental controls / "safety" filters are content filtering, not LAN
  isolation — they don't affect discovery either way.

---
See also: [wavr-network-requirements.md](wavr-network-requirements.md) ·
[known-limitations.md](known-limitations.md) — including why Wavr deliberately under-claims when
it can't prove which side is filtering.
