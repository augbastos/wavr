"""A5.1 local-API hardening tests: F6 (gate the side-effecting GET /api/health),
the optional same-machine local-API token, and the optional /api/v1 alias. All
default-off paths must be byte-identical to before; the token/versioning must never
become an auth-bypass."""
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.storage import Storage
from wavr.hub import Hub
from wavr.fusion import FusionEngine
from wavr.camera_store import CameraStore


async def _up() -> bool:
    return True


def _app(**kw):
    return create_app(sources=[], storage=Storage(":memory:"), hub=Hub(),
                      fusion=FusionEngine(), camera_store=CameraStore(":memory:"),
                      health_resolvers={}, **kw)


# ---- A5.1-F6: GET /api/health now requires X-Wavr-Local -----------------------

def test_health_403_without_local_header(monkeypatch):
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    with TestClient(_app(health_check=_up)) as c:      # no default header
        assert c.get("/api/health").status_code == 403


def test_health_200_with_local_header(monkeypatch):
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    with TestClient(_app(health_check=_up), headers={"X-Wavr-Local": "1"}) as c:
        r = c.get("/api/health")
        assert r.status_code == 200 and r.json()["gateway"]["ok"] is True


def test_healthz_still_open_no_header(monkeypatch):
    # Liveness probe must NOT regress -- /healthz stays open and egress-free.
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    with TestClient(_app()) as c:
        assert c.get("/healthz").status_code == 200


# ---- A5.1: optional local-API token ------------------------------------------

def test_token_unset_is_noop(monkeypatch):
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    with TestClient(_app()) as c:
        r = c.get("/api/status")
        assert r.status_code == 200
        assert "local_token" not in r.text and "token" not in r.json()["features"]


def test_token_set_requires_it_on_api(monkeypatch):
    monkeypatch.setenv("WAVR_LOCAL_TOKEN", "s3cr3t-value")
    with TestClient(_app()) as c:
        assert c.get("/api/status").status_code == 401                       # no token
        assert c.get("/api/status",
                     headers={"X-Wavr-Token": "s3cr3t-value"}).status_code == 200
        assert c.get("/api/status",
                     headers={"Authorization": "Bearer s3cr3t-value"}).status_code == 200
        assert c.get("/api/status",
                     headers={"X-Wavr-Token": "wrong"}).status_code == 401


def test_token_nonascii_header_is_clean_401_not_500(monkeypatch):
    # Crash-on-hostile-input guard: a loopback request with a NON-ASCII token header
    # must fail CLOSED with 401, never 500. hmac.compare_digest raises TypeError on
    # str with non-ASCII, so before the byte-encode fix this surfaced as HTTP 500.
    # Bytes header values are decoded latin-1 by Starlette -> a non-ASCII str reaches
    # the compare. raise_server_exceptions=False so a regression shows as 500 (assert
    # fails) instead of bubbling out of the client.
    monkeypatch.setenv("WAVR_LOCAL_TOKEN", "s3cr3t-value")
    with TestClient(_app(), raise_server_exceptions=False) as c:
        assert c.get("/api/status",
                     headers={"X-Wavr-Token": b"caf\xc3\xa9"}).status_code == 401
        assert c.get("/api/status",
                     headers={"Authorization": b"Bearer n\xc3\xb6pe"}).status_code == 401


def test_token_set_still_allows_bootstrap_and_liveness(monkeypatch):
    monkeypatch.setenv("WAVR_LOCAL_TOKEN", "s3cr3t-value")
    with TestClient(_app()) as c:
        assert c.get("/healthz").status_code == 200        # exempt
        assert c.get("/").status_code == 200               # shell exempt (bootstrap)


def test_token_never_appears_in_status_body(monkeypatch):
    monkeypatch.setenv("WAVR_LOCAL_TOKEN", "s3cr3t-value")
    with TestClient(_app(), headers={"X-Wavr-Token": "s3cr3t-value"}) as c:
        assert "s3cr3t-value" not in c.get("/api/status").text


# ---- A5.1: /api/v1 alias (default OFF) ---------------------------------------

def test_v1_alias_absent_by_default(monkeypatch):
    monkeypatch.delenv("WAVR_API_V1", raising=False)
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    with TestClient(_app(), headers={"X-Wavr-Local": "1"}) as c:
        assert c.get("/api/v1/status").status_code == 404


def test_v1_alias_matches_unversioned(monkeypatch):
    monkeypatch.setenv("WAVR_API_V1", "1")
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    with TestClient(_app(), headers={"X-Wavr-Local": "1"}) as c:
        assert c.get("/api/v1/status").json() == c.get("/api/status").json()


def test_v1_alias_does_not_bypass_f6(monkeypatch):
    # Versioning must inherit the SAME gates -- /api/v1/health still needs the header.
    monkeypatch.setenv("WAVR_API_V1", "1")
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    with TestClient(_app(health_check=_up)) as c:               # no header
        assert c.get("/api/v1/health").status_code == 403
    with TestClient(_app(health_check=_up), headers={"X-Wavr-Local": "1"}) as c:
        assert c.get("/api/v1/health").status_code == 200


def test_v1_alias_does_not_bypass_token(monkeypatch):
    monkeypatch.setenv("WAVR_API_V1", "1")
    monkeypatch.setenv("WAVR_LOCAL_TOKEN", "s3cr3t-value")
    with TestClient(_app(), headers={"X-Wavr-Local": "1"}) as c:
        assert c.get("/api/v1/status").status_code == 401       # token still required
        assert c.get("/api/v1/status",
                     headers={"X-Wavr-Token": "s3cr3t-value"}).status_code == 200
