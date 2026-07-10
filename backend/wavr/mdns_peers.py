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
        role = (info.properties or {}).get(b"role", b"").decode(errors="replace")
        found.append(DiscoveredPeer(
            name=_display_name(name), host=addrs[0], port=info.port, role=role,
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


def _build_service_info(name: str, port: int, role: str):
    from zeroconf import ServiceInfo  # lazy: real path only
    import socket

    local_ip = socket.gethostbyname(socket.gethostname())
    return ServiceInfo(
        _SERVICE_TYPE, f"{name}.{_SERVICE_TYPE}",
        addresses=[socket.inet_aton(local_ip)], port=port,
        properties={"v": "1", "path": "/", "role": role},
        server=f"{name.lower().replace(' ', '-')}.local.",
    )


def advertise_self(name: str, port: int, role: str = "desktop",
                    zeroconf_factory=None, info_factory=None) -> _AdvertiseHandle:
    """Register THIS instance as `_wavr._tcp` (Desktop's own advertise; Core
    already does this natively via core-launcher). Returns a handle with
    `.stop()` to unregister + close.

    `zeroconf_factory` and `info_factory` are both injectable (used by
    tests, see `test_advertise_self_registers_and_returns_stoppable_
    handle`). Real callers pass neither: the real path lazily builds a live
    `zeroconf.Zeroconf()` and an actual `zeroconf.ServiceInfo`."""
    zc = zeroconf_factory() if zeroconf_factory is not None else _real_zeroconf()
    info = info_factory() if info_factory is not None else _build_service_info(name, port, role)
    zc.register_service(info)
    return _AdvertiseHandle(zc, info)


def _real_zeroconf():
    from zeroconf import Zeroconf  # lazy: real path only
    return Zeroconf()
