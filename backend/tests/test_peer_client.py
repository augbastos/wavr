import json
import pytest
from wavr.peer_client import MAX_PEER_BODY, PeerClientError, get_json, post_json


def test_post_json_happy_path():
    calls = []

    def fake_transport(method, url, headers, body, pinned_fingerprint, timeout):
        calls.append((method, url, headers, body, pinned_fingerprint))
        return json.dumps({"ok": True}).encode()

    result = post_json("https://192.168.1.57:8000", "/api/peers/redeem",
                        {"code": "123"}, token="tok-abc",
                        pinned_fingerprint="AA:BB", transport=fake_transport)
    assert result == {"ok": True}
    method, url, headers, body, fp = calls[0]
    assert method == "POST"
    assert url == "https://192.168.1.57:8000/api/peers/redeem"
    assert headers["Authorization"] == "Bearer tok-abc"
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"code": "123"}
    assert fp == "AA:BB"


def test_post_json_without_token_omits_auth_header():
    def fake_transport(method, url, headers, body, pinned_fingerprint, timeout):
        assert "Authorization" not in headers
        return b"{}"
    post_json("https://x:8000", "/api/peers/exchange", {}, token=None,
              transport=fake_transport)


def test_get_json_happy_path():
    def fake_transport(method, url, headers, body, pinned_fingerprint, timeout):
        assert method == "GET"
        assert body is None
        return json.dumps({"peers": []}).encode()
    result = get_json("https://x:8000", "/api/peers", token="t",
                       transport=fake_transport)
    assert result == {"peers": []}


def test_transport_error_raises_peer_client_error():
    def failing_transport(*a, **k):
        raise OSError("connection refused")
    with pytest.raises(PeerClientError):
        post_json("https://x:8000", "/api/peers/exchange", {}, transport=failing_transport)


def test_bad_json_response_raises_peer_client_error():
    def bad_json_transport(*a, **k):
        return b"not json"
    with pytest.raises(PeerClientError):
        post_json("https://x:8000", "/api/peers/exchange", {}, transport=bad_json_transport)


# --------------------------------------------------------------------------
# _default_transport (the REAL transport) -- I1 + §E hardenings. Driven with a
# fake http.client.HTTPSConnection so no real socket/TLS is opened: it records
# the call ORDER so we can prove the pin is verified BEFORE any request bytes.
# --------------------------------------------------------------------------
import wavr.peer_client as peer_client
from wavr.tls import format_fingerprint

_DER = b"fake-der-cert-bytes"
_FP = format_fingerprint(_DER)


class _FakeSock:
    def __init__(self, events, der=_DER):
        self._events = events
        self._der = der

    def settimeout(self, t):
        self._events.append(("settimeout", t))

    def getpeercert(self, binary_form=False):
        self._events.append(("getpeercert", binary_form))
        return self._der


class _FakeResp:
    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self._body = body

    def read(self, amt=None):
        return self._body if amt is None else self._body[:amt]


class _FakeConn:
    def __init__(self, events, der=_DER, status=200, body=b"{}"):
        self._events = events
        self.sock = _FakeSock(events, der)
        self._status = status
        self._body = body

    def connect(self):
        self._events.append(("connect",))

    def request(self, method, path, body=None, headers=None):
        self._events.append(("request", method, path, body))

    def getresponse(self):
        self._events.append(("getresponse",))
        return _FakeResp(self._status, self._body)

    def close(self):
        self._events.append(("close",))


def _patch_conn(monkeypatch, **kw):
    events = []
    monkeypatch.setattr(peer_client.http.client, "HTTPSConnection",
                        lambda *a, **k: _FakeConn(events, **kw))
    return events


def test_pin_is_verified_before_request_is_sent(monkeypatch):
    # I1: the pinned-cert check (getpeercert) MUST happen before request() puts the
    # bearer token / pairing code on the wire.
    events = _patch_conn(monkeypatch)
    peer_client._default_transport(
        "POST", "https://192.168.1.57:8000/api/peers/redeem",
        {"Authorization": "Bearer secret"}, b'{"code":"x"}', _FP, 5.0)
    names = [e[0] for e in events]
    assert names.index("connect") < names.index("getpeercert") < names.index("request")


def test_mismatched_pin_never_sends_the_request(monkeypatch):
    # A MitM cert (wrong fingerprint) must be rejected with NO request() call at all --
    # the credential never leaves the process.
    events = _patch_conn(monkeypatch, der=b"different-cert")
    with pytest.raises(PeerClientError):
        peer_client._default_transport(
            "POST", "https://192.168.1.57:8000/api/peers/redeem",
            {"Authorization": "Bearer secret"}, b'{"code":"x"}', _FP, 5.0)
    assert "request" not in [e[0] for e in events]


def test_error_message_omits_observed_fingerprint(monkeypatch):
    # §B: the mismatch error must not leak the observed cert fingerprint (exfil oracle).
    events = _patch_conn(monkeypatch, der=b"different-cert")
    with pytest.raises(PeerClientError) as exc:
        peer_client._default_transport(
            "POST", "https://x:8000/p", {}, b"{}", _FP, 5.0)
    assert format_fingerprint(b"different-cert") not in str(exc.value)


def test_oversized_response_is_rejected(monkeypatch):
    events = _patch_conn(monkeypatch, body=b"x" * (MAX_PEER_BODY + 10))
    with pytest.raises(PeerClientError, match="maximum size"):
        peer_client._default_transport("GET", "https://x:8000/p", {}, None, None, 5.0)


def test_http_error_status_body_not_echoed(monkeypatch):
    # §B: a 4xx/5xx from the peer surfaces as status only, never the raw body bytes.
    events = _patch_conn(monkeypatch, status=500, body=b"SENSITIVE-INTERNAL-DETAIL")
    with pytest.raises(PeerClientError) as exc:
        peer_client._default_transport("GET", "https://x:8000/p", {}, None, None, 5.0)
    assert "SENSITIVE-INTERNAL-DETAIL" not in str(exc.value)
