# Vodafone router — fixing discovery

Applies to **Vodafone Broadband** routers — the **Ultra Hub** (Ireland), THG3000 / Vodafone
Station variants (admin usually at `http://192.168.1.1`; login on the sticker under/behind the
router, typically user `vodafone` + the printed password).

**When Wavr points here:** `AP_ISOLATION_OR_MDNS_FILTERING` or `SECOND_NETWORK_VLAN`.

## The honest headline: there is nothing to toggle

**Field-verified on a Vodafone Ultra Hub 7 (Ireland, 2026-07, Expert Mode ON), menu by menu:**
the stock firmware exposes **none** of the classic switches. Every admin page was checked:

- **Wi-Fi → General**: guest toggle, Wi-Fi on/off, Compatibility Mode — no isolation.
- **Wi-Fi → Wi-Fi Settings**: only radio Mode / Channel / Bandwidth / DFS per band.
- **Wi-Fi → Steering**: band steering only.
- **Internet → Firewall**: firewall on/off + deny-ping-WAN only.
- **Internet →** Port Forwarding / Block Devices / DMZ / DNS / DynDNS — nothing relevant.
- **Settings → Local Network**: DHCP pool / IP / static reservations only — no IGMP/multicast.
- **Settings → UPnP**: IGD port-mapping (WAN) only — not a LAN mDNS/SSDP control.
- **Settings →** Password / LED / HTTPS-for-LAN / Restart / Eco Mode — nothing relevant.

So: **no "AP isolation" / "client isolation" checkbox, no mDNS/multicast filter, no IGMP
snooping control, and no bridge/modem mode exposed.** If you've hunted the menus and found
nothing, **you didn't miss it — it genuinely isn't there.** This is common on ISP-locked
firmware, and it's exactly why Wavr's diagnosis never *demands* router access to be useful.

**Cross-host proof the filtering is real** (Ultra Hub 7, 2026-07): with the Core sending an SSDP
M-SEARCH from one device, a second device on the *same subnet* received **zero** of those
packets — the hub drops client-to-client multicast. So the block is genuine (not a Wavr false
alarm), and there is no in-UI setting to lift it.

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
