"""A3.3 speed-test route tests. NO real egress -- run_speedtest is injected, and
the ndt7 unit tests inject the locate + download transports. Proves the three
gates (opt-in 503, provider dispatch, per-invocation confirm 409), the egress
disclosure, and the require_local CSRF gate."""
from fastapi.testclient import TestClient

from wavr import speedtest
from wavr.app import create_app


def _client(**kw):
    return TestClient(create_app(sources=[], **kw), headers={"X-Wavr-Local": "1"})


def _fake_speedtest(result):
    def _fn(provider):
        return {**result, "provider": result.get("provider", provider)}
    return _fn


def test_speedtest_503_when_disabled(monkeypatch):
    monkeypatch.delenv("WAVR_NET_SPEEDTEST", raising=False)
    with _client(speedtest_fn=_fake_speedtest({})) as c:
        assert c.post("/api/speedtest", json={"confirm": True}).status_code == 503


def test_speedtest_409_without_confirm(monkeypatch):
    monkeypatch.setenv("WAVR_NET_SPEEDTEST", "1")
    with _client(speedtest_fn=_fake_speedtest({})) as c:
        assert c.post("/api/speedtest", json={}).status_code == 409
        assert c.post("/api/speedtest", json={"confirm": False}).status_code == 409


def test_speedtest_200_cloudflare_default_discloses(monkeypatch):
    monkeypatch.setenv("WAVR_NET_SPEEDTEST", "1")
    monkeypatch.delenv("WAVR_SPEEDTEST_PROVIDER", raising=False)
    seen = {}

    def fn(provider):
        seen["provider"] = provider
        return {"provider": "cloudflare", "server": None, "latency_ms": 5.0,
                "download_mbps": 100.0, "upload_mbps": 20.0, "disclosed_egress": True}

    with _client(speedtest_fn=fn) as c:
        r = c.post("/api/speedtest", json={"confirm": True})
        assert r.status_code == 200
        body = r.json()
        assert body["provider"] == "cloudflare"
        assert "cloudflare" in body["disclosure"].lower()
        assert "publish" not in body["disclosure"].lower()
    assert seen["provider"] == "cloudflare"   # default provider, not ndt7


def test_speedtest_ndt7_only_reachable_via_provider_flag(monkeypatch):
    # The IP-publishing ndt7 path is chosen by config, never the request body.
    monkeypatch.setenv("WAVR_NET_SPEEDTEST", "1")
    monkeypatch.setenv("WAVR_SPEEDTEST_PROVIDER", "ndt7")
    seen = {}

    def fn(provider):
        seen["provider"] = provider
        return {"provider": "ndt7", "server": "mlab1-lhr01", "latency_ms": 8.0,
                "download_mbps": 250.0, "upload_mbps": None, "disclosed_egress": True}

    with _client(speedtest_fn=fn) as c:
        r = c.post("/api/speedtest", json={"confirm": True})
        assert r.status_code == 200
        body = r.json()
        assert body["provider"] == "ndt7"
        # ndt7 disclosure must name the permanent public-IP publication.
        assert "publish" in body["disclosure"].lower()
        assert "public ip" in body["disclosure"].lower()
    assert seen["provider"] == "ndt7"


def test_speedtest_body_cannot_force_ndt7(monkeypatch):
    # A stray provider field in the body must NOT reach the ndt7 path when the
    # config provider is cloudflare (the second gate).
    monkeypatch.setenv("WAVR_NET_SPEEDTEST", "1")
    monkeypatch.setenv("WAVR_SPEEDTEST_PROVIDER", "cloudflare")
    seen = {}

    def fn(provider):
        seen["provider"] = provider
        return {"provider": provider}

    with _client(speedtest_fn=fn) as c:
        r = c.post("/api/speedtest", json={"confirm": True, "provider": "ndt7"})
        assert r.status_code == 200
    assert seen["provider"] == "cloudflare"


def test_speedtest_502_on_backend_error(monkeypatch):
    monkeypatch.setenv("WAVR_NET_SPEEDTEST", "1")

    def boom(provider):
        raise RuntimeError("network down")

    with _client(speedtest_fn=boom) as c:
        assert c.post("/api/speedtest", json={"confirm": True}).status_code == 502


def test_speedtest_requires_local_header(monkeypatch):
    monkeypatch.setenv("WAVR_NET_SPEEDTEST", "1")
    app = create_app(sources=[], speedtest_fn=_fake_speedtest({}))
    with TestClient(app) as c:   # no header
        assert c.post("/api/speedtest", json={"confirm": True}).status_code == 403


def test_speedtest_info_pre_egress_disclosure_cloudflare(monkeypatch):
    # Audit fix (finding 3): the provider + its disclosure are retrievable BEFORE
    # any egress, so the consent modal can warn correctly before confirm=true.
    monkeypatch.setenv("WAVR_NET_SPEEDTEST", "1")
    monkeypatch.delenv("WAVR_SPEEDTEST_PROVIDER", raising=False)
    with _client() as c:
        r = c.get("/api/speedtest/info")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True
        assert body["provider"] == "cloudflare"
        assert body["publishes_ip"] is False
        assert "publish" not in body["disclosure"].lower()


def test_speedtest_info_pre_egress_discloses_ndt7_ip_publication(monkeypatch):
    # The ndt7/M-Lab path must be knowable pre-egress: publishes_ip True + the
    # disclosure names the permanent public-IP publication, so the modal renders
    # the correct bold warning BEFORE the user can send confirm=true.
    monkeypatch.setenv("WAVR_NET_SPEEDTEST", "1")
    monkeypatch.setenv("WAVR_SPEEDTEST_PROVIDER", "ndt7")
    with _client() as c:
        body = c.get("/api/speedtest/info").json()
        assert body["provider"] == "ndt7"
        assert body["publishes_ip"] is True
        assert "publish" in body["disclosure"].lower()
        assert "public ip" in body["disclosure"].lower()


def test_speedtest_info_reports_disabled(monkeypatch):
    monkeypatch.delenv("WAVR_NET_SPEEDTEST", raising=False)
    with _client() as c:
        body = c.get("/api/speedtest/info").json()
        assert body["enabled"] is False   # no egress; still tells the UI it's off


# --- ndt7 client units (injected transports, zero real M-Lab contact) --------
def test_ndt7_pick_download_url_parses_locate():
    locate = {"results": [{"machine": "mlab1-lhr01",
                           "urls": {"wss:///ndt/v7/download": "wss://mlab1-lhr01/ndt/v7/download?access_token=T",
                                    "wss:///ndt/v7/upload": "wss://mlab1-lhr01/ndt/v7/upload?access_token=T"}}]}
    server = speedtest.pick_download_url(locate)
    assert server == ("mlab1-lhr01", "wss://mlab1-lhr01/ndt/v7/download?access_token=T")


def test_ndt7_pick_download_url_bad_shape_returns_none():
    assert speedtest.pick_download_url({}) is None
    assert speedtest.pick_download_url({"results": [{"urls": {}}]}) is None


def test_run_speedtest_ndt7_with_injected_transports():
    result = speedtest.run_speedtest(
        "ndt7",
        locate_fn=lambda: {"results": [{"machine": "mlab1-x",
                                        "urls": {"wss:///ndt/v7/download": "wss://mlab1-x/ndt/v7/download"}}]},
        ndt7_download_fn=lambda url: 321.0,
        latency_fn=lambda: 9.0)
    assert result["provider"] == "ndt7"
    assert result["server"] == "mlab1-x"
    assert result["download_mbps"] == 321.0
    assert result["latency_ms"] == 9.0
    assert result["upload_mbps"] is None   # documented limitation, not faked


def test_run_speedtest_ndt7_no_server_returns_nulls():
    result = speedtest.run_speedtest("ndt7", locate_fn=lambda: {}, latency_fn=lambda: 1.0)
    assert result["provider"] == "ndt7"
    assert result["server"] is None
    assert result["download_mbps"] is None


def test_run_speedtest_cloudflare_with_injected_transports():
    result = speedtest.run_speedtest(
        "cloudflare",
        latency_fn=lambda: 4.0, download_fn=lambda: 88.0, upload_fn=lambda: 12.0)
    assert result["provider"] == "cloudflare"
    assert result["download_mbps"] == 88.0
    assert result["upload_mbps"] == 12.0
    assert result["disclosed_egress"] is True
