# Ubiquiti UniFi — fixing discovery (TEMPLATE)

> **This guide is a stub.** UniFi is prosumer and highly configurable, so paths vary a lot by
> controller version. The structure below is correct; verify exact locations in your Network app.
> Contributions welcome (PRs to `docs/network-fixes/`).

Applies to **UniFi** (Dream Machine / Cloud Gateway / self-hosted Network controller).

**When Wavr points here:** `AP_ISOLATION_OR_MDNS_FILTERING` or `SECOND_NETWORK_VLAN` — and on UniFi
this is very often a *deliberate* config you set up, not an accident.

## 1. Client / device isolation
- Network app → **Settings → WiFi → (your SSID) → Advanced** → check **"Client Device Isolation"**
  is **off** for the network Wavr lives on.
- _TODO: confirm label on your controller version (it has moved between "Isolation" and "Client
  Device Isolation")._

## 2. VLANs are the usual UniFi cause
- If you put IoT devices on a dedicated **IoT VLAN / network** (very common on UniFi), Wavr must
  be able to reach it. Either put Wavr's host on the same VLAN, or add firewall/mDNS rules that
  let discovery cross.
- Network app → **Settings → Networks** to see your VLANs and which SSID maps to which.

## 3. mDNS across VLANs
- UniFi has an **mDNS / "Multicast DNS" reflector** setting (Network → Settings → Services, older
  versions under the network's advanced options). If you run multiple VLANs and want discovery to
  cross them, this must be **on** for the relevant networks.
- _TODO: confirm the exact toggle name on current UniFi OS._

## 4. IGMP snooping
- If discovery works briefly then dies, check **IGMP snooping** has a querier on the segment — see
  [known-limitations.md](known-limitations.md).

---
General principles: [wavr-network-requirements.md](wavr-network-requirements.md).
