"""NOTIFY-surface connectors (project_wavr_connectors_vision /
DESIGN-external-connectors.md section 3.3): the shared `connectors/http.py`
egress chokepoint + the Telegram connector (`connectors/notify/telegram.py`).

Covers the non-negotiable guardrails from the task brief:
  * fail-closed default-OFF: `send()` is a no-op with a clear "disabled"
    result when the broker says the connector is off, and ZERO network is
    attempted (a fake broker + a fake HTTP client prove the call count, no
    real network involved anywhere in this file).
  * no token value is ever logged, even on transport failure.
  * the outbound text is bounded to kind/severity/room/summary -- never a raw
    camera/occupancy/MAC/credential field.
"""
from __future__ import annotations

import logging

from wavr.connectors.http import guarded_call
from wavr.connectors.notify import telegram

TOKEN = "123456:AAFAKE-TOKEN-VALUE-NEVER-LOGGED"
CHAT_ID = "-100999888"


class FakeStore:
    """Minimal broker double: only the `is_enabled(id) -> bool` contract
    `guarded_call`/`make_telegram_send` actually need."""

    def __init__(self, enabled: dict | None = None):
        self._enabled = dict(enabled or {})

    def is_enabled(self, connector_id: str) -> bool:
        return bool(self._enabled.get(connector_id, False))


class FakePost:
    """Fake HTTP transport: records every call (url/payload/timeout), never
    touches the network. Optionally raises to exercise the failure path."""

    def __init__(self, raise_exc: Exception | None = None):
        self.calls: list[dict] = []
        self._raise = raise_exc

    def __call__(self, url: str, payload: dict, headers: dict | None = None,
                 timeout: float | None = None) -> dict:
        self.calls.append({"url": url, "payload": payload, "timeout": timeout})
        if self._raise:
            raise self._raise
        return {"ok": True}


# --------------------------------------------------------------------------- #
# connectors/http.py: the shared chokepoint
# --------------------------------------------------------------------------- #
def test_guarded_call_disabled_never_invokes_fn():
    store = FakeStore()  # nothing enabled
    called = []
    result = guarded_call(store, "anything", lambda: called.append(1) or {"ok": True})
    assert result == {"ok": False, "status": "disabled"}
    assert called == []


def test_guarded_call_enabled_invokes_fn():
    store = FakeStore({"anything": True})
    result = guarded_call(store, "anything", lambda: {"ok": True, "status": "sent"})
    assert result == {"ok": True, "status": "sent"}


def test_guarded_call_custom_disabled_result():
    store = FakeStore()
    result = guarded_call(store, "x", lambda: {"ok": True},
                           disabled_result={"ok": False, "status": "off"})
    assert result == {"ok": False, "status": "off"}


# --------------------------------------------------------------------------- #
# connectors/notify/telegram.py: build_alert_text (the derived-only allowlist)
# --------------------------------------------------------------------------- #
def test_build_alert_text_includes_only_the_four_allowlisted_fields():
    text = telegram.build_alert_text("rogue_device", "alert", "kitchen",
                                     "unrecognized device on the network")
    assert "rogue_device" in text
    assert "ALERT" in text
    assert "kitchen" in text
    assert "unrecognized device on the network" in text


def test_build_alert_text_omits_absent_room_and_summary():
    text = telegram.build_alert_text("routine_anomaly", "note", None, "")
    assert "room:" not in text


def test_build_alert_text_truncates_oversized_summary():
    text = telegram.build_alert_text("k", "note", None, "x" * 5000)
    assert len(text) <= telegram._MAX_TEXT_LEN


# --------------------------------------------------------------------------- #
# _sanitize_summary / build_alert_text: runtime blocklist on the free-text
# `summary` field (the allowlist bounds WHICH fields travel; this bounds
# what's inside the one field that's still free text).
# --------------------------------------------------------------------------- #
def test_sanitize_summary_redacts_mac_like_token():
    out = telegram._sanitize_summary("device AA:BB:CC:DD:EE:FF joined")
    assert "AA:BB:CC:DD:EE:FF" not in out
    assert "[redacted]" in out


def test_sanitize_summary_redacts_coordinate_like_pair():
    out = telegram._sanitize_summary("last seen near 52.6598, -8.6112")
    assert "52.6598" not in out and "-8.6112" not in out
    assert "[redacted]" in out


def test_sanitize_summary_redacts_rtsp_url():
    out = telegram._sanitize_summary("stream at rtsp://192.168.1.42:554/live")
    assert "rtsp://" not in out
    assert "[redacted]" in out


def test_sanitize_summary_redacts_frame_token():
    out = telegram._sanitize_summary("attached frame_00123.jpg for context")
    assert "frame_00123.jpg" not in out
    assert "[redacted]" in out


def test_sanitize_summary_passes_through_clean_text_unchanged():
    out = telegram._sanitize_summary("unrecognized device on the network")
    assert out == "unrecognized device on the network"


def test_sanitize_summary_empty_string_passes_through():
    assert telegram._sanitize_summary("") == ""


def test_build_alert_text_redacts_mac_and_coordinate_and_rtsp_and_frame():
    text = telegram.build_alert_text(
        "rogue_device", "alert", "kitchen",
        "MAC AA:BB:CC:DD:EE:FF at 52.6598, -8.6112 via rtsp://cam.local/frame_1.jpg")
    assert "AA:BB:CC:DD:EE:FF" not in text
    assert "52.6598" not in text and "-8.6112" not in text
    assert "rtsp://" not in text
    assert "frame_1.jpg" not in text


# --------------------------------------------------------------------------- #
# make_telegram_send: fail-closed default-OFF + zero network when disabled
# --------------------------------------------------------------------------- #
def test_send_is_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("WAVR_TELEGRAM_TOKEN", TOKEN)
    monkeypatch.setenv("WAVR_TELEGRAM_CHAT_ID", CHAT_ID)
    store = FakeStore()  # connector not enabled
    post = FakePost()
    send = telegram.make_telegram_send(store, post=post)

    result = send("rogue_device", "alert", "kitchen", "unrecognized device")

    assert result == {"ok": False, "status": "disabled"}
    assert post.calls == []  # zero network attempted, even though creds ARE set


def test_send_is_noop_when_enabled_but_unconfigured(monkeypatch):
    monkeypatch.delenv("WAVR_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("WAVR_TELEGRAM_CHAT_ID", raising=False)
    store = FakeStore({"telegram": True})
    post = FakePost()
    send = telegram.make_telegram_send(store, post=post)

    result = send("rogue_device", "alert", "kitchen", "unrecognized device")

    assert result == {"ok": False, "status": "unconfigured"}
    assert post.calls == []


def test_send_succeeds_when_enabled_and_configured(monkeypatch):
    monkeypatch.setenv("WAVR_TELEGRAM_TOKEN", TOKEN)
    monkeypatch.setenv("WAVR_TELEGRAM_CHAT_ID", CHAT_ID)
    store = FakeStore({"telegram": True})
    post = FakePost()
    send = telegram.make_telegram_send(store, post=post)

    result = send("rogue_device", "alert", "kitchen", "unrecognized device")

    assert result["ok"] is True
    assert result["status"] == "sent"
    assert len(post.calls) == 1
    call = post.calls[0]
    assert call["url"] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert call["payload"]["chat_id"] == CHAT_ID
    text = call["payload"]["text"]
    assert "kitchen" in text and "rogue_device" in text
    # derived-only: never a raw camera/occupancy/MAC/credential field leaks in
    for forbidden in ("mac", "frame", "vitals", "coordinate", "rtsp"):
        assert forbidden not in text.lower()
    assert TOKEN not in text  # the token is a URL-path credential, never message text


def test_send_redacts_summary_before_it_reaches_the_wire(monkeypatch):
    # Full stack: a caller passes a summary that (by bug or bad input) carries
    # a MAC + coordinate + rtsp/frame reference -- none of it may reach the
    # payload actually posted to Telegram's API.
    monkeypatch.setenv("WAVR_TELEGRAM_TOKEN", TOKEN)
    monkeypatch.setenv("WAVR_TELEGRAM_CHAT_ID", CHAT_ID)
    store = FakeStore({"telegram": True})
    post = FakePost()
    send = telegram.make_telegram_send(store, post=post)

    result = send("rogue_device", "alert", "kitchen",
                   "MAC AA:BB:CC:DD:EE:FF near 52.6598, -8.6112 rtsp://cam.local/frame_9.jpg")

    assert result["ok"] is True
    text = post.calls[0]["payload"]["text"]
    assert "AA:BB:CC:DD:EE:FF" not in text
    assert "52.6598" not in text and "-8.6112" not in text
    assert "rtsp://" not in text and "frame_9.jpg" not in text
    assert "[redacted]" in text


def test_send_respects_custom_connector_and_env_names(monkeypatch):
    monkeypatch.setenv("WAVR_TG_TOKEN_2", TOKEN)
    monkeypatch.setenv("WAVR_TG_CHAT_2", CHAT_ID)
    store = FakeStore({"telegram-2": True})
    post = FakePost()
    send = telegram.make_telegram_send(
        store, connector_id="telegram-2",
        token_env="WAVR_TG_TOKEN_2", chat_id_env="WAVR_TG_CHAT_2", post=post)

    result = send("fall_suspected", "alert", "living room", "possible fall")

    assert result["ok"] is True
    assert len(post.calls) == 1


def test_send_treats_api_level_rejection_as_error(monkeypatch):
    # Telegram answers HTTP 200 with {"ok": false, ...} for several real
    # failures (chat not found, bot blocked, ...) -- the POST doesn't raise,
    # but it still must NOT be reported as delivered.
    monkeypatch.setenv("WAVR_TELEGRAM_TOKEN", TOKEN)
    monkeypatch.setenv("WAVR_TELEGRAM_CHAT_ID", CHAT_ID)
    store = FakeStore({"telegram": True})

    def post(url, payload, headers=None, timeout=None):
        return {"ok": False, "description": "Forbidden: bot was blocked by the user"}

    send = telegram.make_telegram_send(store, post=post)
    result = send("rogue_device", "alert", "kitchen", "unrecognized device")
    assert result == {"ok": False, "status": "error"}


def test_send_failure_returns_error_and_never_logs_the_token(monkeypatch, caplog):
    monkeypatch.setenv("WAVR_TELEGRAM_TOKEN", TOKEN)
    monkeypatch.setenv("WAVR_TELEGRAM_CHAT_ID", CHAT_ID)
    store = FakeStore({"telegram": True})
    post = FakePost(raise_exc=RuntimeError(f"boom while posting to bot{TOKEN}"))
    send = telegram.make_telegram_send(store, post=post)

    with caplog.at_level(logging.WARNING):
        result = send("rogue_device", "alert", "kitchen", "unrecognized device")

    assert result == {"ok": False, "status": "error"}
    assert TOKEN not in caplog.text
    assert len(post.calls) == 1  # the attempt happened; only the log stayed clean
