"""A3.1 Wake-on-LAN route tests. Zero real sockets -- the UDP send seam is
injected. Proves: opt-in 503 gate, MAC validation, broadcast/port allowlist
(no unicast-to-internet primitive), and the require_local CSRF gate."""
from fastapi.testclient import TestClient

from wavr.app import create_app


def _client(**kw):
    return TestClient(create_app(sources=[], **kw), headers={"X-Wavr-Local": "1"})


def test_wol_503_when_disabled(monkeypatch):
    monkeypatch.delenv("WAVR_NET_WOL", raising=False)
    with _client(wol_send=lambda *a: None) as c:
        assert c.post("/api/wol", json={"mac": "aa:bb:cc:dd:ee:ff"}).status_code == 503


def test_wol_sends_via_injected_seam(monkeypatch):
    monkeypatch.setenv("WAVR_NET_WOL", "1")
    sent = []
    with _client(wol_send=lambda p, b, port: sent.append((p, b, port))) as c:
        r = c.post("/api/wol", json={"mac": "aa-bb-cc-dd-ee-ff"})
        assert r.status_code == 200
        body = r.json()
        assert body["sent"] is True
        assert body["mac"] == "aa:bb:cc:dd:ee:ff"   # normalized
        assert body["bytes"] == 102
    assert len(sent) == 1
    packet, broadcast, port = sent[0]
    assert len(packet) == 102 and broadcast == "255.255.255.255" and port == 9


def test_wol_400_on_bad_mac(monkeypatch):
    monkeypatch.setenv("WAVR_NET_WOL", "1")
    with _client(wol_send=lambda *a: None) as c:
        assert c.post("/api/wol", json={"mac": "not-a-mac"}).status_code == 400


def test_wol_accepts_subnet_directed_broadcast(monkeypatch):
    monkeypatch.setenv("WAVR_NET_WOL", "1")
    with _client(wol_send=lambda *a: None) as c:
        r = c.post("/api/wol",
                   json={"mac": "aa:bb:cc:dd:ee:ff", "broadcast": "192.168.1.255", "port": 7})
        assert r.status_code == 200
        assert r.json()["broadcast"] == "192.168.1.255"


def test_wol_rejects_routable_broadcast(monkeypatch):
    monkeypatch.setenv("WAVR_NET_WOL", "1")
    with _client(wol_send=lambda *a: None) as c:
        r = c.post("/api/wol", json={"mac": "aa:bb:cc:dd:ee:ff", "broadcast": "8.8.8.8"})
        assert r.status_code == 400   # can't aim WoL at an internet host


def test_wol_rejects_disallowed_port(monkeypatch):
    monkeypatch.setenv("WAVR_NET_WOL", "1")
    with _client(wol_send=lambda *a: None) as c:
        r = c.post("/api/wol", json={"mac": "aa:bb:cc:dd:ee:ff", "port": 22})
        assert r.status_code == 400   # only 0/7/9 allowed


def test_wol_requires_local_header(monkeypatch):
    monkeypatch.setenv("WAVR_NET_WOL", "1")
    app = create_app(sources=[], wol_send=lambda *a: None)
    with TestClient(app) as c:   # no X-Wavr-Local header
        assert c.post("/api/wol", json={"mac": "aa:bb:cc:dd:ee:ff"}).status_code == 403
