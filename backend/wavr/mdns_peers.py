"""mDNS/DNS-SD peer discovery for cross-instance pairing (2026-07-09 design
spec, Phase 1). Core already self-advertises `_wavr._tcp` from the native
Kotlin launcher (`core-launcher`, commit 3af4787) -- that side is UNCHANGED
by this module. What's new here:

  * BROWSING for `_wavr._tcp` on the LAN -- needed by BOTH Desktop and Core's
    Python backend (neither browses today; only Mobile's capacitor-zeroconf
    does, for a different purpose -- pairing AS a companion, not peer
    discovery).
  * Desktop's OWN self-advertise -- Desktop has no Kotlin/NsdManager
    equivalent, so it advertises the same `_wavr._tcp` TXT shape
    (`{v, path, role}`) via the `zeroconf` Python package instead.

`zeroconf` is a LAZY import (only inside the real, non-injected code paths)
behind the new `[mdns]` extra -- a base install that never touches
peer-discovery code never needs it installed, same pattern as
`[camera]`/`[mmwave]`/`[ble]`. Every public function takes an injectable
factory so this module is fully unit-testable without the dependency
installed and with zero real network (see `tests/test_mdns_peers.py`)."""
from __future__ import annotations

from dataclasses import dataclass

_SERVICE_TYPE = "_wavr._tcp.local."


@dataclass(frozen=True)
class DiscoveredPeer:
    name: str
    host: str
    port: int
    role: str
    # OPAQUE Space id (first 16 chars), advertised so a device joining the
    # network can tell "these two Cores serve the same Space" apart from "these
    # are two unrelated Wavr installs on one LAN" -- WITHOUT the Space's human
    # name ever going onto the wire. Naming your home is for you; broadcasting
    # that name to every device on the segment is a disclosure nobody asked for.
    # "" when the peer predates this field or has no Space yet.
    space_id: str = ""
    # The advertising Core's own id, the epoch it believes it is primary at, its
    # status, and the Wavr Protocol version it speaks. ALL HINTS: anything on the
    # segment can broadcast these. They make the Discovery Inbox card meaningful
    # and let a joiner group Cores by Space; they never reach the leadership
    # logic, which only listens to PAIRED peers over a pinned channel.
    core_id: str = ""
    epoch: int = 0
    status: str = ""
    protocol_version: int = 0


def _display_name(service_name: str) -> str:
    """Human-readable instance name from a DNS-SD service name. Real
    zeroconf `ServiceInfo` has no single canonical 'display name' field
    distinct from the DNS-SD instance name baked into the service name
    itself, so this strips the `_wavr._tcp.local.` suffix and unescapes the
    one DNS-label escape mDNS tooling commonly emits for a literal space
    (`\\032`)."""
    return service_name.replace("\\032", " ").split("." + _SERVICE_TYPE)[0]


def _collect_peers(zc, names) -> list[DiscoveredPeer]:
    """Shared browse->DiscoveredPeer parsing for both the injected-fake path
    and the real `zeroconf.Zeroconf` path -- both only need `.get_service_
    info(type_, name)` returning an object with `.parsed_addresses()`,
    `.port`, and `.properties`, which `zeroconf.ServiceInfo` satisfies."""
    found = []
    for name in names:
        info = zc.get_service_info(_SERVICE_TYPE, name)
        if info is None:
            continue
        addrs = info.parsed_addresses()
        if not addrs:
            continue
        props = info.properties or {}
        # Bounded + charset-clamped: this is attacker-controllable text off the
        # LAN, and it ends up in an admin UI list.
        def _txt(key: str, limit: int = 32) -> str:
            raw = props.get(key.encode(), b"").decode(errors="replace")[:limit]
            return "".join(c for c in raw if c.isalnum() or c in "-_")

        def _txt_int(key: str) -> int:
            raw = _txt(key, 12)
            return int(raw) if raw.isdigit() else 0

        # `role` and the display name were the two fields NOT going through
        # `_txt` -- which contradicted this block's own claim to be bounding
        # attacker-controllable text off the LAN. No exploit today (every
        # consumer uses textContent), but code should do what it says.
        found.append(DiscoveredPeer(
            name=_display_name(name)[:64], host=addrs[0], port=info.port,
            role=_txt("role", 16),
            space_id=_txt("sid", 16), core_id=_txt("cid", 40),
            epoch=_txt_int("ep"), status=_txt("st", 16),
            protocol_version=_txt_int("pv"),
        ))
    return found


def browse_wavr_peers(timeout: float = 3.0, zeroconf_factory=None) -> list[DiscoveredPeer]:
    """Blocking snapshot browse: return whatever `_wavr._tcp` services are
    known. `zeroconf_factory` (injectable, used by tests) must return an
    object exposing `.service_names()` and `.get_service_info(type_, name)`
    -- see `_FakeZeroconf` in the test module. Without it, this listens for
    `timeout` seconds over a real `zeroconf.ServiceBrowser` before reading
    back whatever it found, in the same shape."""
    if zeroconf_factory is not None:
        zc = zeroconf_factory()
        names = [n for n in zc.service_names() if n.endswith(_SERVICE_TYPE)]
        return _collect_peers(zc, names)

    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf  # lazy: real path only
    import time

    class _CollectingListener(ServiceListener):
        def __init__(self):
            self.names: set[str] = set()

        def add_service(self, zc, type_, name):
            self.names.add(name)

        def update_service(self, zc, type_, name):
            self.names.add(name)

        def remove_service(self, zc, type_, name):
            self.names.discard(name)

    zc = Zeroconf()
    listener = _CollectingListener()
    browser = ServiceBrowser(zc, _SERVICE_TYPE, listener)
    try:
        time.sleep(timeout)
        return _collect_peers(zc, sorted(listener.names))
    finally:
        browser.cancel()
        zc.close()


class _AdvertiseHandle:
    """Returned by `advertise_self`; call `.stop()` from the app lifespan's
    shutdown path (same pattern as every other background resource in
    app.py -- MQTT publisher, camera sources, etc.) to unregister and close
    cleanly."""

    def __init__(self, zc, info):
        self._zc = zc
        self._info = info

    def stop(self) -> None:
        self._zc.unregister_service(self._info)
        self._zc.close()


def txt_properties(role: str, space_id: str = "", core_id: str = "",
                   epoch: int = 0, status: str = "",
                   protocol_version: int = 1) -> dict:
    """The DNS-SD TXT record this Core advertises.

    Extracted from `_build_service_info` so it can be tested without zeroconf
    installed -- these values are read by anything on the segment and are the
    input to another Core's Discovery Inbox, so their shape and bounds matter
    more than the ServiceInfo plumbing around them.

    A STANDBY advertises no epoch and no status: only a Core that believes it is
    authoritative says so, which is what makes a disagreement detectable at all.
    None of it is trusted -- see `api_peers.core_status` for the authenticated
    channel that actually feeds leadership."""
    props = {"v": "1", "path": "/", "role": role, "pv": str(int(protocol_version))}
    if space_id:
        props["sid"] = str(space_id)[:16]
    if core_id:
        props["cid"] = str(core_id)[:40]
    # ONLY a Core that believes it is authoritative advertises a claim. The gate
    # used to be `if status:`, and a Core that joined a Space carries
    # `status="standby"` -- non-empty -- so every standby advertised an epoch
    # too, which is ambient noise rather than a signal. A disagreement is only
    # detectable if the advertisement means something.
    if status == "primary":
        props["st"] = str(status)[:16]
        props["ep"] = str(int(epoch))
    return props


def _build_service_info(name: str, port: int, role: str, space_id: str = "",
                        core_id: str = "", epoch: int = 0, status: str = "",
                        protocol_version: int = 1):
    from zeroconf import ServiceInfo  # lazy: real path only
    import socket

    local_ip = socket.gethostbyname(socket.gethostname())
    props = txt_properties(role, space_id, core_id, epoch, status,
                           protocol_version)
    return ServiceInfo(
        _SERVICE_TYPE, f"{name}.{_SERVICE_TYPE}",
        addresses=[socket.inet_aton(local_ip)], port=port,
        properties=props,
        server=f"{name.lower().replace(' ', '-')}.local.",
    )


def advertise_self(name: str, port: int, role: str = "desktop",
                    zeroconf_factory=None, info_factory=None,
                    space_id: str = "", core_id: str = "", epoch: int = 0,
                    status: str = "", protocol_version: int = 1) -> _AdvertiseHandle:
    """Register THIS instance as `_wavr._tcp` (Desktop's own advertise; Core
    already does this natively via core-launcher). Returns a handle with
    `.stop()` to unregister + close.

    `zeroconf_factory` and `info_factory` are both injectable (used by
    tests, see `test_advertise_self_registers_and_returns_stoppable_
    handle`). Real callers pass neither: the real path lazily builds a live
    `zeroconf.Zeroconf()` and an actual `zeroconf.ServiceInfo`."""
    zc = zeroconf_factory() if zeroconf_factory is not None else _real_zeroconf()
    info = (info_factory() if info_factory is not None
            else _build_service_info(name, port, role, space_id, core_id,
                                     epoch, status, protocol_version))
    zc.register_service(info)
    return _AdvertiseHandle(zc, info)


def _real_zeroconf():
    from zeroconf import Zeroconf  # lazy: real path only
    return Zeroconf()
