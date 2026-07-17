# Vodafone router — fixing discovery (TEMPLATE)

> **This guide is a stub.** The structure below is correct; the exact menu paths for your specific
> Vodafone model still need verifying. Contributions welcome (PRs to `docs/network-fixes/`).

Applies to **Vodafone Broadband** routers (e.g. THG3000 / Vodafone Station; admin usually at
`http://192.168.1.1`).

**When Wavr points here:** `AP_ISOLATION_OR_MDNS_FILTERING` or `SECOND_NETWORK_VLAN`.

## 1. Client / AP isolation
- Admin → **Wi-Fi → Advanced settings** → look for **"Isolate clients" / "AP isolation"** and
  switch it **off**.
- _TODO: confirm the exact menu path on current Vodafone Station firmware._

## 2. Guest network
- Vodafone guest Wi-Fi is isolated by default — keep Wavr + devices on the **main** SSID.
- _TODO: confirm guest-network menu location._

## 3. Same subnet / second network
- Keep Wavr's host and your sensors on the same subnet; if a mesh or second router is in play,
  that's the likely source of a second network.
- _TODO: note whether Vodafone Station exposes VLAN/IoT segmentation on this model._

## 4. Multicast / mDNS
- _TODO: verify whether this firmware filters multicast on guest vs main._

---
General principles: [wavr-network-requirements.md](wavr-network-requirements.md) ·
[known-limitations.md](known-limitations.md). Until this stub is filled in, those two cover the
"what" even without the Vodafone-specific "where".
