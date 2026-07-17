# Discovery diagnosis — known limitations

Wavr's `discovery_reach` check correlates how many devices are reachable (ARP) against how many
answer a name-discovery (mDNS/SSDP) probe. When many devices are reachable but the mesh is
silent, PR2 tries to name the cause honestly. It follows one hard rule:

> **Never blame the router without proving the hub itself can receive inbound LAN multicast.**

To honour that rule the hub runs a *viability probe*: it joins the two common discovery groups
(mDNS `224.0.0.251`, SSDP `239.255.255.250`), disables multicast loopback so its own packets
never count, provokes traffic, and listens for any multicast packet from another host.

- Viability **proven** → a silent mesh is the *network* segmenting/filtering discovery
  (`AP_ISOLATION_OR_MDNS_FILTERING`, or `SECOND_NETWORK_VLAN` when subnets/DHCP servers differ).
- Viability **not proven** → the fault is placed on the *hub's environment*
  (`HOST_MULTICAST_UNAVAILABLE`), never on the router.

This is deliberately conservative. The verdict is always a hypothesis ("provavelmente"), never
CONFIRMED (ADR-0003). The cases below are where a single host genuinely cannot tell more, and
Wavr chooses to under-claim rather than accuse the wrong component:

## 1. A hub that can't receive LAN multicast reads the same as full AP isolation

Both a proot/container Core (which never receives inbound LAN multicast) and a router with
*total* client-to-client isolation (which blocks all multicast, including from the router) leave
the hub with **zero** foreign multicast. From one host these are indistinguishable, so Wavr
reports `HOST_MULTICAST_UNAVAILABLE` in both — the copy names *both* possibilities and asks the
user to try the Core outside a container **or** check AP isolation. The clean test is to run the
Core on a normal laptop on the same network (see `TESTPLAN` T3): there, true AP isolation shows
as `AP_ISOLATION_OR_MDNS_FILTERING`.

## 2. IGMP snooping without a querier

A switch doing IGMP snooping but with **no** multicast querier on the segment prunes multicast
groups after a few minutes. Discovery then works right after a device (or Wavr) rejoins a group
and dies minutes later. A short probe window can catch either state, so the verdict may flip
between runs. This is a real network misconfiguration, not a Wavr bug.

## 3. A network of "dumb" devices with no mDNS/SSDP talkers

Some networks are perfectly healthy yet have **no** devices that announce themselves over
mDNS/SSDP (e.g. only appliances that speak neither). Discovery is legitimately silent and there
is nothing to fix. Wavr can't distinguish "nobody is talking" from "talk is being filtered" when
the hub also receives no other multicast — another reason `HOST_MULTICAST_UNAVAILABLE` is worded
as a possibility, not an accusation.

## 4. The probe is passive + single-shot

The viability and responder probes listen for a few seconds and provoke once. They do not send
repeated queries or run continuously, to keep the check cheap and side-effect-free (it never
touches the router). A device that only answers on a longer interval can be missed in one run.
