"""A5.2 ARP device-blocking tests. ZERO real packets -- the ARP-send transport is
injected everywhere. Proves the full guardrail set: triple gate (flag/CSRF/confirm),
gateway hard-deny + inventory-only target denylist, honest 503 when unavailable,
auto-expiry + corrective-ARP reversibility, shutdown restore, and MCP exclusion."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from wavr import arp_block
from wavr.arp_block import ArpBlocker, validate_target
from wavr.app import create_app


def _dev(mac, ip, is_gateway=False):
    return SimpleNamespace(mac=mac, ip=ip, is_gateway=is_gateway)


GW = _dev("gg:gg:gg:gg:gg:gg", "192.168.1.1", is_gateway=True)
T1 = _dev("aa:bb:cc:dd:ee:01", "192.168.1.50")
INV = [GW, T1]
LOCAL = "192.168.1.10"


# ---- validate_target: the target denylist (AP1/AP2) --------------------------

def test_validate_ok_for_inventory_lan_host():
    assert validate_target("aa:bb:cc:dd:ee:01", inventory=INV, gateway=GW,
                           local_ip=LOCAL) == ("aa:bb:cc:dd:ee:01", "192.168.1.50")


def test_validate_rejects_gateway_by_mac():
    with pytest.raises(ValueError):
        validate_target("gg:gg:gg:gg:gg:gg", inventory=INV, gateway=GW, local_ip=LOCAL)


def test_validate_rejects_gateway_by_flag():
    gw2 = _dev("11:11:11:11:11:11", "192.168.1.1", is_gateway=True)
    with pytest.raises(ValueError):
        validate_target("11:11:11:11:11:11", inventory=[gw2, T1], gateway=gw2, local_ip=LOCAL)


def test_validate_rejects_off_inventory_mac():
    with pytest.raises(ValueError):
        validate_target("de:ad:be:ef:00:00", inventory=INV, gateway=GW, local_ip=LOCAL)


def test_validate_rejects_out_of_subnet():
    far = _dev("aa:bb:cc:dd:ee:02", "10.9.9.9")
    with pytest.raises(ValueError):
        validate_target("aa:bb:cc:dd:ee:02", inventory=[GW, far], gateway=GW, local_ip=LOCAL)


def test_validate_rejects_metadata_and_linklocal():
    meta = _dev("aa:bb:cc:dd:ee:03", "169.254.169.254")
    with pytest.raises(ValueError):
        validate_target("aa:bb:cc:dd:ee:03", inventory=[GW, meta], gateway=GW, local_ip=LOCAL)


def test_validate_rejects_self_and_public():
    pub = _dev("aa:bb:cc:dd:ee:04", "8.8.8.8")
    with pytest.raises(ValueError):
        validate_target("aa:bb:cc:dd:ee:04", inventory=[GW, pub], gateway=GW, local_ip=LOCAL)


# ---- A5 hardening: fail-CLOSED denylist (no silent bypass) --------------------

def test_validate_rejects_empty_local_ip():
    # Unknown host LAN IP must refuse ALL targets, not silently drop the self-host
    # and same-/24 guards (which would widen the allowed set to any inventory MAC).
    off = _dev("aa:bb:cc:dd:ee:05", "10.55.55.55")       # unrelated subnet
    with pytest.raises(ValueError):
        validate_target("aa:bb:cc:dd:ee:05", inventory=[GW, off], gateway=GW, local_ip="")
    selfdev = _dev("aa:bb:cc:dd:ee:06", LOCAL)            # this host's own IP
    with pytest.raises(ValueError):
        validate_target("aa:bb:cc:dd:ee:06", inventory=[GW, selfdev], gateway=GW, local_ip="")


def test_validate_rejects_when_gateway_not_identified():
    # Gateway detection failing (gateway=None) must refuse ALL blocks -- the real
    # gateway is private, in-/24 and in the ARP inventory, so it would otherwise
    # pass every remaining check = whole-LAN DoS.
    with pytest.raises(ValueError):
        validate_target("aa:bb:cc:dd:ee:01", inventory=INV, gateway=None, local_ip=LOCAL)


def test_validate_denies_independent_gateway_ip():
    # The gateway deny-set includes an INDEPENDENTLY derived gateway IP, so the guard
    # does not rest on the inventory flag alone -- even when the flagged gateway's own
    # IP differs, a target matching the independent IP is rejected.
    gw_flag = _dev("gg:gg:gg:gg:gg:gg", "192.168.1.254", is_gateway=True)
    tgt = _dev("aa:bb:cc:dd:ee:07", "192.168.1.60")
    with pytest.raises(ValueError):
        validate_target("aa:bb:cc:dd:ee:07", inventory=[gw_flag, tgt], gateway=gw_flag,
                        local_ip=LOCAL, gateway_ip="192.168.1.60")


def test_validate_rejects_uppercase_ipv6_metadata():
    # Canonicalization: a differently-cased textual form of the ULA metadata address
    # must still be recognized (not only the exact literal 'fd00:ec2::254').
    meta = _dev("aa:bb:cc:dd:ee:08", "FD00:EC2::254")
    with pytest.raises(ValueError):
        validate_target("aa:bb:cc:dd:ee:08", inventory=[GW, meta], gateway=GW, local_ip=LOCAL)


# ---- ArpBlocker: transport seam, TTL, reversibility --------------------------

async def test_block_emits_poison_and_lists():
    sink = []
    b = ArpBlocker(send=lambda ip, mac, gw, restore: sink.append((ip, mac, gw, restore)))
    res = await b.block("aa:bb:cc:dd:ee:01", inventory=INV, gateway=GW, local_ip=LOCAL)
    assert res["blocked"] is True and res["ip"] == "192.168.1.50"
    assert sink == [("192.168.1.50", "aa:bb:cc:dd:ee:01", "192.168.1.1", False)]
    assert b.list_blocks()[0]["mac"] == "aa:bb:cc:dd:ee:01"
    await b.stop()


async def test_unblock_sends_corrective_arp():
    sink = []
    b = ArpBlocker(send=lambda ip, mac, gw, restore: sink.append(restore))
    await b.block("aa:bb:cc:dd:ee:01", inventory=INV, gateway=GW, local_ip=LOCAL)
    await b.unblock("aa:bb:cc:dd:ee:01", inventory=INV, gateway=GW, local_ip=LOCAL)
    assert sink[-1] is True            # corrective ARP
    assert b.list_blocks() == []
    await b.stop()


async def test_ttl_expiry_auto_unblocks_with_corrective():
    t = {"now": 1000.0}
    sink = []
    b = ArpBlocker(send=lambda ip, mac, gw, restore: sink.append(restore),
                   ttl=60.0, clock=lambda: t["now"])
    await b.block("aa:bb:cc:dd:ee:01", inventory=INV, gateway=GW, local_ip=LOCAL)
    t["now"] = 2000.0                  # jump well past TTL
    b._tick()
    assert b.list_blocks() == []
    assert sink[-1] is True            # expiry sent corrective ARP
    await b.stop()


async def test_shutdown_restores_all_blocks():
    sink = []
    b = ArpBlocker(send=lambda ip, mac, gw, restore: sink.append((mac, restore)))
    await b.block("aa:bb:cc:dd:ee:01", inventory=INV, gateway=GW, local_ip=LOCAL)
    await b.stop()
    assert ("aa:bb:cc:dd:ee:01", True) in sink   # corrective ARP on shutdown
    assert b.list_blocks() == []


def test_unavailable_when_no_transport():
    assert ArpBlocker(send=None).available() is False
    assert ArpBlocker(send=lambda *a, **k: None).available() is True


# ---- Route: POST /api/block triple gate + MCP exclusion ----------------------

def _client(monkeypatch, enable=True, arp_send=(lambda *a, **k: None), header=True):
    if enable:
        monkeypatch.setenv("WAVR_NET_BLOCKING", "1")
    else:
        monkeypatch.delenv("WAVR_NET_BLOCKING", raising=False)
    app = create_app(sources=[], arp_send=arp_send)
    hdr = {"X-Wavr-Local": "1"} if header else {}
    return TestClient(app, headers=hdr)


def test_block_503_when_flag_off(monkeypatch):
    with _client(monkeypatch, enable=False) as c:
        r = c.post("/api/block", json={"mac": "aa:bb:cc:dd:ee:01", "confirm": True})
        assert r.status_code == 503


def test_block_503_when_transport_unavailable(monkeypatch):
    # Flag ON but no elevated ARP-send transport -> honest 503, never a silent no-op.
    with _client(monkeypatch, enable=True, arp_send=None) as c:
        r = c.post("/api/block", json={"mac": "aa:bb:cc:dd:ee:01", "confirm": True})
        assert r.status_code == 503
        assert "faking" in r.json()["detail"]


def test_block_409_without_confirm(monkeypatch):
    with _client(monkeypatch, enable=True) as c:
        r = c.post("/api/block", json={"mac": "aa:bb:cc:dd:ee:01"})
        assert r.status_code == 409


def test_block_403_without_local_header(monkeypatch):
    with _client(monkeypatch, enable=True, header=False) as c:
        r = c.post("/api/block", json={"mac": "aa:bb:cc:dd:ee:01", "confirm": True})
        assert r.status_code == 403


def test_block_400_for_off_inventory_target(monkeypatch):
    # No inventory scan has run (sources=[]), so any MAC is off-inventory -> 400,
    # not a block. Confirms the denylist runs even past the triple gate.
    with _client(monkeypatch, enable=True) as c:
        r = c.post("/api/block", json={"mac": "de:ad:be:ef:00:00", "confirm": True})
        assert r.status_code == 400


def test_block_400_bad_action(monkeypatch):
    with _client(monkeypatch, enable=True) as c:
        r = c.post("/api/block",
                   json={"mac": "aa:bb:cc:dd:ee:01", "action": "nuke", "confirm": True})
        assert r.status_code == 400


def test_status_features_exposes_blocking_flag(monkeypatch):
    with _client(monkeypatch, enable=True) as c:
        assert c.get("/api/status").json()["features"]["blocking"] is True
    with _client(monkeypatch, enable=False) as c:
        assert c.get("/api/status").json()["features"]["blocking"] is False


class _FakeInv:
    """Test seam for the NetworkInventoryService: a fixed device list so the route's
    200 success path is deterministic + packet-free. start/stop are async no-ops so
    it drops into create_app(net_inventory=...) and the lifespan teardown cleanly."""
    def __init__(self, devices):
        self._d = list(devices)
    def latest_inventory(self):
        return list(self._d)
    async def start(self):
        pass
    async def stop(self):
        pass


def test_block_and_unblock_end_to_end_200(monkeypatch):
    # End-to-end HTTP success: seed a flagged gateway + a non-gateway LAN target in the
    # SAME /24 as this host, then confirm POST /api/block returns 200 with the full
    # shape, and POST {action: unblock} WITHOUT confirm halts it (blocked False).
    from wavr.sources.network import _local_ipv4
    local = _local_ipv4()
    if not local or "." not in local:
        import pytest as _pt
        _pt.skip("no local IPv4 to derive a same-/24 target")
    base, last = local.rsplit(".", 1)
    last = int(last)
    tgt_last = 2 if last != 2 else 3          # in-/24, != this host, != gateway '.1'
    gw = _dev("gg:gg:gg:gg:gg:gg", f"{base}.1", is_gateway=True)
    tgt = _dev("aa:bb:cc:dd:ee:77", f"{base}.{tgt_last}")

    monkeypatch.setenv("WAVR_NET_BLOCKING", "1")
    app = create_app(sources=[], arp_send=lambda *a, **k: None,
                     net_inventory=_FakeInv([gw, tgt]))
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        r = c.post("/api/block", json={"mac": "aa:bb:cc:dd:ee:77", "confirm": True})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["blocked"] is True
        assert body["mac"] == "aa:bb:cc:dd:ee:77"
        assert body["ip"] == f"{base}.{tgt_last}"
        assert body["ttl"] > 0 and "note" in body
        # Reversibility: unblock must work WITHOUT confirm (halt a live block anytime).
        r2 = c.post("/api/block", json={"mac": "aa:bb:cc:dd:ee:77", "action": "unblock"})
        assert r2.status_code == 200, r2.text
        assert r2.json()["blocked"] is False


def test_block_rejects_when_gateway_not_in_inventory(monkeypatch):
    # Fail closed at the route: an inventory with a valid target but NO flagged gateway
    # (detection missed) must 400, not block -- the gateway hard-deny can't be verified.
    from wavr.sources.network import _local_ipv4
    local = _local_ipv4()
    if not local or "." not in local:
        import pytest as _pt
        _pt.skip("no local IPv4 to derive a same-/24 target")
    base, last = local.rsplit(".", 1)
    tgt_last = 2 if int(last) != 2 else 3
    tgt = _dev("aa:bb:cc:dd:ee:88", f"{base}.{tgt_last}")   # no is_gateway device present
    monkeypatch.setenv("WAVR_NET_BLOCKING", "1")
    app = create_app(sources=[], arp_send=lambda *a, **k: None,
                     net_inventory=_FakeInv([tgt]))
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        r = c.post("/api/block", json={"mac": "aa:bb:cc:dd:ee:88", "confirm": True})
        assert r.status_code == 400
        assert "gateway" in r.json()["detail"].lower()


def test_block_rejects_multidevice_central_peer(tmp_path, monkeypatch):
    # Red-team mitigation #2 (loopback-root ONLY) -- the single most important add.
    # Even an authenticated multidevice 'central' peer, who CAN change other state,
    # must NOT reach /api/block. This closes the F-C bypass: a paired/stolen central
    # token would otherwise wield the inward ARP-attack primitive header-less
    # (require_local lets 'central' through without the X-Wavr-Local CSRF header;
    # require_root does not).
    from wavr.storage import Storage
    from wavr.camera_store import CameraStore
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_NET_BLOCKING", "1")          # flag ON so 403 isn't a masked 503
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "md.db"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    app = create_app(sources=[], storage=Storage(":memory:"),
                     camera_store=CameraStore(":memory:"),
                     arp_send=lambda *a, **k: None)         # transport available -> not a 503
    central = TestClient(app)
    code = central.post("/api/pair-code", json={"role": "central"},
                        headers={"X-Wavr-Local": "1"}).json()["code"]
    peer = TestClient(app, client=("192.168.1.50", 12345))
    token = peer.post("/api/pair", json={"code": code, "device_name": "phone"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    # Sanity: this central peer genuinely CAN change other state (header-less)...
    assert peer.post("/api/system/toggle", json={"on": False}, headers=auth).status_code == 200
    # ...but is refused on the ARP-block primitive -- 403 (not 409/503), so only the
    # loopback-root guard produced it (confirm=true is sent; flag+transport are on).
    assert peer.post("/api/block",
                     json={"mac": "aa:bb:cc:dd:ee:01", "confirm": True},
                     headers=auth).status_code == 403
    # The read-only audit view is likewise loopback-root only.
    assert peer.get("/api/block", headers=auth).status_code == 403


def test_status_features_expose_a5_hardening_flags(monkeypatch):
    # api_token (bool only, never the secret) + health_gate (F6 always-on) surfaced
    # so the Privacy & Egress view is honest about the local-API posture.
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    with _client(monkeypatch, enable=False) as c:
        feats = c.get("/api/status").json()["features"]
        assert feats["api_token"] is False
        assert feats["health_gate"] is True
    monkeypatch.setenv("WAVR_LOCAL_TOKEN", "s3cr3t-value")
    app = create_app(sources=[], arp_send=None)
    with TestClient(app, headers={"X-Wavr-Local": "1", "X-Wavr-Token": "s3cr3t-value"}) as c:
        body = c.get("/api/status")
        assert body.status_code == 200
        feats = body.json()["features"]
        assert feats["api_token"] is True          # token required
        assert "s3cr3t-value" not in body.text     # never leak the secret


def test_blocking_is_permanently_excluded_from_mcp():
    # MCP is read-only-by-construction. Assert structurally (no dependence on the
    # optional [mcp] extra): (1) no block/arp/spoof/deauth @server.tool is defined,
    # (2) the sensitive-hint denylist backstops any HA-name indirection, (3) the
    # permanent-exclusion warning is present at the extension point.
    import inspect
    import wavr.mcp as mcp
    src = inspect.getsource(mcp.build_mcp_server)
    lowered = src.lower()
    for term in ("block", "arp", "spoof", "deauth"):
        assert f'def {term}' not in lowered and f'"{term}' not in lowered
    assert '@server.tool()' in src   # sanity: we are reading the right function
    assert "PERMANENTLY OUT OF MCP SCOPE" in src
    assert "arp_block" in mcp._SENSITIVE_HINTS and "deauth" in mcp._SENSITIVE_HINTS
