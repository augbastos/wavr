"""A3.2 diagnostics route tests. Zero real subprocess/socket -- runner + probe +
DNS transports injected. Proves: opt-in 503 gate, the target validator rejects
shell metacharacters + hostile targets (no command injection), traceroute is
invoked with an argv LIST, dnsbench defaults to LAN-only (zero egress) and sorts
fastest-first, and the require_local CSRF gate."""
import pytest
from fastapi.testclient import TestClient

from wavr import diagnostics
from wavr.app import create_app


def _client(**kw):
    return TestClient(create_app(sources=[], **kw), headers={"X-Wavr-Local": "1"})


def test_diag_503_when_disabled(monkeypatch):
    monkeypatch.delenv("WAVR_NET_DIAGNOSTICS", raising=False)
    with _client() as c:
        assert c.post("/api/diag/ping", json={"host": "192.168.1.1"}).status_code == 503


def test_ping_via_injected_probe(monkeypatch):
    monkeypatch.setenv("WAVR_NET_DIAGNOSTICS", "1")

    async def fake_probe(ip, port, timeout):
        return 12.5

    with _client(ping_probe=fake_probe) as c:
        r = c.post("/api/diag/ping", json={"host": "192.168.1.1", "count": 2})
        assert r.status_code == 200
        body = r.json()
        assert body["received"] == 2
        assert body["avg_ms"] == 12.5
        assert body["host"] == "192.168.1.1"


def test_traceroute_uses_argv_list_no_shell(monkeypatch):
    monkeypatch.setenv("WAVR_NET_DIAGNOSTICS", "1")
    captured = {}

    async def fake_runner(argv, timeout):
        captured["argv"] = argv
        return 0, "1  192.168.1.1  1.2 ms\n2  10.0.0.1  5.0 ms\n"

    with _client(traceroute_runner=fake_runner) as c:
        r = c.post("/api/diag/traceroute", json={"host": "example.com"})
        assert r.status_code == 200
        hops = r.json()["hops"]
        assert hops[0]["hop"] == 1 and hops[0]["hosts"] == ["192.168.1.1"]

    argv = captured["argv"]
    assert isinstance(argv, list)              # argv LIST, never a shell string
    assert "example.com" in argv
    for elem in argv:                          # no shell metacharacter anywhere
        assert not any(ch in elem for ch in ";|&`$()<>\n\"'\\ ")


@pytest.mark.parametrize("hostile", [
    "127.0.0.1; rm -rf /",
    "$(reboot)",
    "`whoami`",
    "8.8.8.8 && curl evil.example",
    "a|b",
    "foo\nbar",
    "--option",
    "google.com/../x",
    "host name",
])
def test_traceroute_rejects_hostile_target(monkeypatch, hostile):
    monkeypatch.setenv("WAVR_NET_DIAGNOSTICS", "1")
    called = {"n": 0}

    async def fake_runner(argv, timeout):
        called["n"] += 1
        return 0, ""

    with _client(traceroute_runner=fake_runner) as c:
        r = c.post("/api/diag/traceroute", json={"host": hostile})
        assert r.status_code == 400
    assert called["n"] == 0    # runner NEVER reached with a hostile target


def test_validate_target_unit_rejects_metacharacters_accepts_clean():
    for bad in ["a;b", "a b", "a|b", "$(x)", "`x`", "a&b", "a\\b", "a>b", "-x", ""]:
        with pytest.raises(ValueError):
            diagnostics.validate_target(bad)
    assert diagnostics.validate_target(" 192.168.0.1 ") == "192.168.0.1"
    assert diagnostics.validate_target("host.example.com") == "host.example.com"


def test_dnsbench_defaults_to_lan_only_zero_egress(monkeypatch):
    monkeypatch.setenv("WAVR_NET_DIAGNOSTICS", "1")
    monkeypatch.setattr(diagnostics, "guess_gateway", lambda: "192.168.1.1")
    queried = []

    async def fake_query(resolver, name, timeout):
        queried.append(resolver)
        return 5.0

    with _client(dns_query_fn=fake_query) as c:
        r = c.post("/api/diag/dns", json={"host": "example.com"})
        assert r.status_code == 200
        body = r.json()
        assert body["egress"] is False
        assert [row["resolver"] for row in body["results"]] == ["192.168.1.1"]
    assert queried == ["192.168.1.1"]    # only the LAN gateway, no public resolver


def test_dnsbench_public_resolvers_sorted_fastest_first(monkeypatch):
    monkeypatch.setenv("WAVR_NET_DIAGNOSTICS", "1")

    async def fake_query(resolver, name, timeout):
        return {"1.1.1.1": 30.0, "8.8.8.8": 10.0}[resolver]

    with _client(dns_query_fn=fake_query) as c:
        r = c.post("/api/diag/dns",
                   json={"host": "example.com", "resolvers": ["1.1.1.1", "8.8.8.8"]})
        body = r.json()
        assert body["egress"] is True
        assert [row["resolver"] for row in body["results"]] == ["8.8.8.8", "1.1.1.1"]


def test_dnsbench_rejects_non_ip_resolver(monkeypatch):
    monkeypatch.setenv("WAVR_NET_DIAGNOSTICS", "1")
    with _client(dns_query_fn=lambda *a: None) as c:
        r = c.post("/api/diag/dns", json={"host": "example.com", "resolvers": ["evil.example"]})
        assert r.status_code == 400


def test_ping_egress_flag_lan_vs_public(monkeypatch):
    # Audit fix (findings 1/4): ping/traceroute now carry the same honest
    # `egress` signal dnsbench already exposes -- False for a LAN target, True
    # for a public one -- so /api/status and the Tools tile disclose reach.
    monkeypatch.setenv("WAVR_NET_DIAGNOSTICS", "1")

    async def fake_probe(ip, port, timeout):
        return 1.0

    with _client(ping_probe=fake_probe) as c:
        assert c.post("/api/diag/ping",
                      json={"host": "192.168.1.1"}).json()["egress"] is False
        assert c.post("/api/diag/ping",
                      json={"host": "8.8.8.8"}).json()["egress"] is True


def test_traceroute_egress_flag(monkeypatch):
    monkeypatch.setenv("WAVR_NET_DIAGNOSTICS", "1")

    async def fake_runner(argv, timeout):
        return 0, "1  10.0.0.1  1.0 ms\n"

    with _client(traceroute_runner=fake_runner) as c:
        assert c.post("/api/diag/traceroute",
                      json={"host": "10.0.0.1"}).json()["egress"] is False
        assert c.post("/api/diag/traceroute",
                      json={"host": "example.com"}).json()["egress"] is True


def test_is_egress_target_unit():
    for lan in ["192.168.1.1", "10.0.0.5", "172.16.0.1", "127.0.0.1",
                "169.254.1.1", "printer.local", "PRINTER.LOCAL"]:
        assert diagnostics.is_egress_target(lan) is False
    for pub in ["8.8.8.8", "1.1.1.1", "example.com", "9.9.9.9"]:
        assert diagnostics.is_egress_target(pub) is True


def test_dnsbench_caps_resolver_list(monkeypatch):
    # Audit fix (finding 2): a huge caller-supplied resolvers list is capped to
    # 16 so one /api/diag/dns call can't run for minutes (queries are awaited
    # sequentially with a per-resolver timeout).
    monkeypatch.setenv("WAVR_NET_DIAGNOSTICS", "1")
    queried = []

    async def fake_query(resolver, name, timeout):
        queried.append(resolver)
        return 1.0

    many = [f"10.0.0.{i}" for i in range(1, 60)]
    with _client(dns_query_fn=fake_query) as c:
        r = c.post("/api/diag/dns", json={"host": "example.com", "resolvers": many})
        assert r.status_code == 200
    assert len(queried) == 16


def test_diag_unknown_kind_404(monkeypatch):
    monkeypatch.setenv("WAVR_NET_DIAGNOSTICS", "1")
    with _client() as c:
        assert c.post("/api/diag/bogus", json={"host": "192.168.1.1"}).status_code == 404


def test_diag_requires_local_header(monkeypatch):
    monkeypatch.setenv("WAVR_NET_DIAGNOSTICS", "1")
    app = create_app(sources=[])
    with TestClient(app) as c:   # no X-Wavr-Local header
        assert c.post("/api/diag/ping", json={"host": "192.168.1.1"}).status_code == 403
