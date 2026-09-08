"""What Wavr says about encryption has to be true of the connection saying it.

`WAVR_MULTIDEVICE` is a feature flag. It is `serve.py` that acts on it, and
`serve.py` is not the only launcher -- its own docstring says so:
`backend/Dockerfile` and `scripts/wavr.ps1` run `uvicorn wavr.app:app` directly
and are plain HTTP whatever the flag says. Setting the flag in `.env`, which is
the documented way to turn it on, therefore produced an install that:

  * reported `features.tls: true` on an unencrypted socket -- a privacy receipt
    read by somebody deciding whether their tokens are on the Wi-Fi in clear;
  * printed `https://<lan-ip>:<port>` as the address to open, which no browser
    can connect to;
  * put that same unreachable address inside the pairing QR, which a phone acts
    on without a human reading it first; and
  * showed a certificate fingerprint and asked the operator to compare it
    against the warning their phone's browser shows -- on a plain-HTTP page,
    which produces no warning and no certificate to compare.

The last one is the reason this file exists. A verification ceremony with
nothing behind it is worse than no ceremony at all, because the operator
finishes it believing they checked something. The fingerprint came off
`~/.wavr/cert.pem`, a file that outlives the process that generated it, so "a
cert exists" was never evidence that "this connection is encrypted".

Every case below runs the SAME Core -- flag on, certificate on disk -- and
changes only the scheme of the connection, which is exactly the variable the
product was ignoring.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.sources.simulated import SimulatedSource
from wavr.storage import Storage
from wavr.tls import cert_fingerprint, ensure_cert

LOCAL = {"X-Wavr-Local": "1"}
LAN_IP = "192.168.1.1"          # fixed, never a real household address
PLAIN = "http://testserver"
TLS = "https://testserver"


@pytest.fixture
def core(tmp_path, monkeypatch):
    """A Core configured EXACTLY like the install that told the lie.

    `WAVR_MULTIDEVICE` on, `WAVR_PEERS_ENABLED` on, and a real self-signed
    certificate sitting on disk -- the state a machine is in after it has run
    the TLS launcher once, and the state `.env` puts a Docker install in from
    the very first boot.

    Returns a builder rather than an app: the MCP session manager refuses a
    second run on one instance, so each connection scheme gets its own.
    """
    cert = str(tmp_path / "cert.pem")
    key = str(tmp_path / "key.pem")
    ensure_cert(cert, key, LAN_IP)
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_PEERS_ENABLED", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "claim.db"))
    monkeypatch.setenv("WAVR_TLS_CERT", cert)
    monkeypatch.setenv("WAVR_TLS_KEY", key)
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: LAN_IP)

    def build():
        return create_app(
            sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
            storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))

    build.fingerprint = cert_fingerprint(cert)
    return build


def test_a_plain_http_core_does_not_claim_tls(core):
    """The flag is on, the certificate is on disk, and the socket is not TLS."""
    with TestClient(core(), base_url=PLAIN) as c:
        features = c.get("/api/status", headers=LOCAL).json()["features"]
        assert features["tls"] is False, (
            "the privacy receipt claims encryption on a plain-HTTP socket; "
            "somebody reads this row to decide whether their tokens are in "
            "clear on the Wi-Fi")
        assert features["multidevice"] is True, (
            "the multidevice flag itself is still on -- this test is about the "
            "two being DIFFERENT questions, not about turning the flag off")


def test_a_plain_http_core_hands_out_addresses_it_can_answer(core):
    with TestClient(core(), base_url=PLAIN) as c:
        status = c.get("/api/setup/status", headers=LOCAL).json()
        assert status["lan_url"] == f"http://{LAN_IP}:8000", status["lan_url"]
        assert status["lan_hostname_url"].startswith("http://"), (
            status["lan_hostname_url"])

        code = c.post("/api/pair-code", headers=LOCAL,
                      json={"role": "user"}).json()
        # The QR carries this one. A phone dials it without a human reading it,
        # so an address the socket cannot answer fails with no explanation.
        assert code["lan_url"] == f"http://{LAN_IP}:8000", code["lan_url"]

        guest = c.post("/api/guest/invite", headers=LOCAL,
                       json={"hours": 4}).json()
        assert guest["lan_url"] == f"http://{LAN_IP}:8000", guest["lan_url"]


def test_a_plain_http_core_does_not_stage_a_certificate_ceremony(core):
    """The one that is worse than a wrong URL.

    The pairing screen shows a fingerprint and asks the operator to compare it
    against the certificate warning on their phone. A plain-HTTP page shows no
    warning and presents no certificate, so the comparison cannot be made --
    and the operator who "did it" walks away believing the channel was checked.

    Empty here is what the frontend already gates the QR block and the
    fingerprint disclosure on, so the honest screen is the typed-code flow.
    """
    with TestClient(core(), base_url=PLAIN) as c:
        assert not c.get("/api/setup/status", headers=LOCAL
                         ).json()["cert_fingerprint"]

        code = c.post("/api/pair-code", headers=LOCAL,
                      json={"role": "user"}).json()
        assert not code["cert_fingerprint"]
        assert code["verify6"] is None, (
            "the 6-digit code is derived from the fingerprint, so it inherits "
            "the same emptiness rather than becoming a number to compare "
            "against nothing")

        guest = c.post("/api/guest/invite", headers=LOCAL,
                       json={"hours": 4}).json()
        assert not guest["cert_fingerprint"]
        assert guest["verify6"] is None

        pending = c.get("/api/pairings/pending", headers=LOCAL)
        if pending.status_code == 200:
            assert not pending.json().get("cert_fingerprint"), (
                "the approve-on-the-Core screen shows the same fingerprint for "
                "the same eyeball compare, from the same source")


def test_a_plain_http_core_refuses_to_join_a_peer_mesh_and_says_why(core):
    """Two Cores linking exchange `central` tokens, the widest credential here.

    The receiving side already refused a plain-HTTP peer. What it could not do
    is name the cause: its message is about `peer_base_url`, which points at the
    OTHER Core's address when the problem is this one's own socket. Refusing
    here, before anything is minted or sent, is the half that can say so.
    """
    with TestClient(core(), base_url=PLAIN) as c:
        r = c.post("/api/peers/confirm", headers=LOCAL, json={
            "peer_base_url": "https://192.168.1.20:8000", "peer_name": "Core",
            "peer_code": "12345678", "peer_fingerprint": "CORE-FP"})
        assert r.status_code == 409, (r.status_code, r.text)
        detail = r.json()["detail"].lower()
        assert "plain http" in detail and "wavr.serve" in detail, detail


def test_the_same_core_over_tls_claims_everything_it_should(core):
    """The other half, and the reason this cannot be fixed by hardcoding `http`.

    Same process, same flag, same certificate. Served over TLS, every claim
    comes back -- which is what makes the plain-HTTP answers above evidence
    about the connection rather than a feature that was removed.
    """
    fp = core.fingerprint
    assert fp, "the fixture did not write a readable certificate"

    with TestClient(core(), base_url=TLS) as c:
        assert c.get("/api/status", headers=LOCAL).json()["features"]["tls"] is True

        status = c.get("/api/setup/status", headers=LOCAL).json()
        assert status["lan_url"] == f"https://{LAN_IP}:8000"
        assert status["lan_hostname_url"].startswith("https://")
        assert status["cert_fingerprint"] == fp

        code = c.post("/api/pair-code", headers=LOCAL,
                      json={"role": "user"}).json()
        assert code["lan_url"] == f"https://{LAN_IP}:8000"
        assert code["cert_fingerprint"] == fp
        assert code["verify6"] and len(code["verify6"]) == 6


def test_one_connections_answer_never_reaches_another(core):
    """Two connections at once, and neither may be told the other's answer.

    Not hypothetical. uvicorn rewrites the scheme from `X-Forwarded-Proto` only
    for peers in `forwarded_allow_ips`, which defaults to 127.0.0.1 — so a Core
    behind a loopback proxy that terminates TLS sees https for proxied requests
    and http for direct LAN ones, in the same process, alternating. Recording
    the answer in one field on `app.state` hands whoever asks whatever somebody
    else's last request happened to set, and what it hands them is a claim
    about whether their own traffic is encrypted.

    Interleaved on purpose: a single pass would pass with the leak, because
    each read would follow its own write.
    """
    app = core()
    with TestClient(app, base_url=PLAIN) as plain:
        # A second client on the SAME app: the outer `with` has already run the
        # lifespan, and running it twice trips the MCP session manager.
        secure = TestClient(app, base_url=TLS)
        for _ in range(3):
            assert secure.get("/api/status", headers=LOCAL
                              ).json()["features"]["tls"] is True
            assert plain.get("/api/status", headers=LOCAL
                             ).json()["features"]["tls"] is False, (
                "the plain-HTTP reader was handed the TLS connection's answer")
            assert plain.get("/api/setup/status", headers=LOCAL
                             ).json()["lan_url"].startswith("http://")
            assert secure.get("/api/setup/status", headers=LOCAL
                              ).json()["lan_url"].startswith("https://")


@pytest.mark.parametrize("base,scheme", [(PLAIN, "http"), (TLS, "https")])
def test_one_answer_reaches_every_screen(core, base, scheme):
    """Four surfaces, one verdict.

    Before this, the scheme was written out separately at each site from the
    same flag -- which is the shape that eventually disagrees, because somebody
    fixes one of them. They are now the same call, and this is the test that
    notices if a fifth site starts deciding for itself.
    """
    with TestClient(core(), base_url=base) as c:
        tls = c.get("/api/status", headers=LOCAL).json()["features"]["tls"]
        status = c.get("/api/setup/status", headers=LOCAL).json()
        code = c.post("/api/pair-code", headers=LOCAL,
                      json={"role": "user"}).json()
        guest = c.post("/api/guest/invite", headers=LOCAL,
                       json={"hours": 4}).json()

    said = {
        "features.tls": "https" if tls else "http",
        "setup lan_url": status["lan_url"].split(":", 1)[0],
        "setup lan_hostname_url": status["lan_hostname_url"].split(":", 1)[0],
        "pair lan_url": code["lan_url"].split(":", 1)[0],
        "guest lan_url": guest["lan_url"].split(":", 1)[0],
    }
    assert set(said.values()) == {scheme}, said
