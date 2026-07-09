import json
import pytest
from wavr.peer_client import PeerClientError, get_json, post_json


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
