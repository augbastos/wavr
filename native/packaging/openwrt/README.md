# OpenWrt native Node

The OpenWrt target is the native Node only. Match the binary's CPU architecture,
endianness, ABI and kernel to the router before copying it. The package is a
statically linked musl executable; mbedTLS is linked into it, so there is no
separate TLS runtime package. Wavr Core does not run on this class of router.

## FEASIBILITY

Release package binaries are approximately 1.5–2.3 MB; platform measurements
elsewhere in this repository put the Node at approximately 6–9 MB RSS. Those
numbers are for other devices, not a router measurement. A router with at least
16 MB flash and 128 MB RAM, plus enough *free writable overlay* for the binary
and state, is a plausible trial target. 4/8 MB flash or 32/64 MB RAM devices
are poor candidates, especially with a USB serial adapter and the rest of
OpenWrt running. Check actual free overlay and memory before installing.

MIPS little and big endian and ARM/x86 static builds exist, but **no binary has
been run on an OpenWrt router**. Serial permissions, USB driver availability,
hotplug naming, kernel compatibility, TLS handshake memory and long-term
stability remain untested on routers.

## Manual installation

1. Copy the matching `wavr` executable to `/usr/bin/wavr` and make it executable.
2. Copy `wavr-node.init` to `/etc/init.d/wavr-node` and make it executable.
3. Set `SENSOR` in the init script to the real serial port. The account running
   procd must be allowed to open it; procd runs this service as root, as it
   does most OpenWrt services. Protect the router's admin access accordingly.
4. Enroll once with `wavr node enroll --url https://CORE:PORT --code CODE
   --state /overlay/wavr-node/node.json` after creating `/overlay/wavr-node`
   with mode 0700. Use the same state path for the service.
5. Run `/etc/init.d/wavr-node enable` and `/etc/init.d/wavr-node start`, then
   inspect `logread -e wavr` and the Core's Nodes panel.

The init script deliberately does not respawn: a revoked Node exits 3 and must
stay stopped. The state is under persistent `/overlay`, not volatile `/var`.
The node rewrites that state file about once a minute, so on a router it writes to `/overlay` flash that often -- a wear consideration
on small NOR/NAND parts that was not measured.
