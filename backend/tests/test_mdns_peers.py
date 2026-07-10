"""Tests for `wavr.mdns_peers` (browse + Desktop self-advertise, Phase 1
peer discovery). Every case injects a fake standing in for `zeroconf` --
the real package is NOT a base dependency (lazy-imported behind the
`[mdns]` extra) and never touches the network in CI."""
from wavr.mdns_peers import DiscoveredPeer, advertise_self, browse_wavr_peers


class _FakeInfo:
    """Stands in for `zeroconf.ServiceInfo` as read by `browse_wavr_peers`:
    only `.parsed_addresses()`, `.port`, and `.properties` are consumed."""

    def __init__(self, host, port, role):
        self.port = port
        self.properties = {b"role": role.encode()}
        self._host = host

    def parsed_addresses(self):
        return [self._host]


class _FakeZeroconf:
    """Minimal fake standing in for `zeroconf.Zeroconf`: the injected
    factory returns an object with `.service_names()` + `.get_service_info
    (type_, name)` (browse) and `.register_service(info)` /
    `.unregister_service(info)` + `.close()` (advertise). Real zeroconf
    usage is exercised only manually (no hardware/network in CI)."""

    def __init__(self, services):
        self._services = services  # {dns_sd_name: _FakeInfo}
        self.registered = []
        self.closed = False

    def service_names(self):
        return list(self._services)

    def get_service_info(self, type_, name, timeout=3000):
        return self._services.get(name)

    def register_service(self, info):
        self.registered.append(info)

    def unregister_service(self, info):
        self.registered.remove(info)

    def close(self):
        self.closed = True


def test_browse_returns_discovered_peers():
    fake = _FakeZeroconf({
        "Wavr Core._wavr._tcp.local.": _FakeInfo("192.168.1.57", 8000, "core"),
        "Wavr Desktop._wavr._tcp.local.": _FakeInfo("192.168.1.227", 8000, "desktop"),
    })

    found = browse_wavr_peers(zeroconf_factory=lambda: fake)

    assert len(found) == 2
    assert all(isinstance(p, DiscoveredPeer) for p in found)
    names = {p.name for p in found}
    assert names == {"Wavr Core", "Wavr Desktop"}
    core = next(p for p in found if p.role == "core")
    assert core.name == "Wavr Core"
    assert core.host == "192.168.1.57"
    assert core.port == 8000
    desktop = next(p for p in found if p.role == "desktop")
    assert desktop.host == "192.168.1.227"
    assert desktop.port == 8000


def test_browse_empty_when_nothing_discovered():
    fake = _FakeZeroconf({})
    assert browse_wavr_peers(zeroconf_factory=lambda: fake) == []


def test_browse_ignores_services_of_a_different_type():
    fake = _FakeZeroconf({
        "Some Printer._ipp._tcp.local.": _FakeInfo("192.168.1.9", 631, ""),
    })
    assert browse_wavr_peers(zeroconf_factory=lambda: fake) == []


def test_advertise_self_registers_and_returns_stoppable_handle():
    fake_zc = _FakeZeroconf({})
    # Real callers never pass info_factory -- the real path lazily builds an
    # actual zeroconf.ServiceInfo. Tests supply the exact object that should
    # round-trip through register_service/unregister_service.
    fake_info = object()

    handle = advertise_self(
        "Wavr Desktop", 8000, role="desktop",
        zeroconf_factory=lambda: fake_zc,
        info_factory=lambda: fake_info,
    )

    assert fake_zc.registered == [fake_info]
    assert fake_zc.closed is False

    handle.stop()

    assert fake_zc.registered == []
    assert fake_zc.closed is True
