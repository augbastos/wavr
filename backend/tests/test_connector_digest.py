"""Daily proactive digest (C2, project_wavr_agentic_home_mission /
DESIGN-external-connectors.md section 3.3): `connectors/notify/digest.py`.

Covers: `compose_digest()` is pure/local (uses only `OccupancyLog`'s public
read API, never egresses), the composed text/fields carry no family
geometry/identity (house-level facts + counts only -- never a room name),
and `send_digest()` is gated default-OFF (a no-op with zero calls when
nothing is enabled/configured) with a Telegram-first / ntfy-fallback route.
"""
from __future__ import annotations

from datetime import datetime, timezone

from wavr.connectors.notify import digest
from wavr.connectors.notify import telegram
from wavr.occupancy_log import OccupancyLog

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 2, tzinfo=timezone.utc)


def _log_one_room_empty_workday(room: str = "kitchen") -> OccupancyLog:
    """A single-room house: home overnight, leaves at 09:00, returns at 18:00
    -- exactly the "house empty 09:00-18:00" example from the design brief."""
    log = OccupancyLog(":memory:", retention_days=None)
    log.append_if_changed(room, True, 0.9, None, "2025-12-31T22:00:00+00:00")  # prior/seed
    log.append_if_changed(room, False, 0.9, None, "2026-01-01T09:00:00+00:00")
    log.append_if_changed(room, True, 0.9, None, "2026-01-01T18:00:00+00:00")
    return log


class FakeStore:
    def __init__(self, enabled: dict | None = None):
        self._enabled = dict(enabled or {})

    def is_enabled(self, connector_id: str) -> bool:
        return bool(self._enabled.get(connector_id, False))


class FakePost:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, url, payload, headers=None, timeout=None):
        self.calls.append({"url": url, "payload": payload})
        return {"ok": True}


# --------------------------------------------------------------------------- #
# Honesty: the module must disclose that empty-window TIMING itself is
# burglary-sensitive (not just identity/geometry) -- a regression guard so a
# future edit can't silently walk back this disclosure.
# --------------------------------------------------------------------------- #
def test_module_docstring_discloses_empty_window_timing_is_sensitive():
    doc = digest.__doc__.lower()
    assert "burglary" in doc or "schedule" in doc
    assert "unprompted" in doc or "proactively" in doc


def test_send_digest_docstring_points_to_the_honesty_note():
    doc = (digest.send_digest.__doc__ or "").lower()
    assert "unprompted" in doc


# --------------------------------------------------------------------------- #
# house_empty_windows: pure reconstruction from OccupancyLog's public API
# --------------------------------------------------------------------------- #
def test_house_empty_windows_single_room_workday():
    log = _log_one_room_empty_workday()
    windows = digest.house_empty_windows(log, start=START, end=END)
    assert windows == [{"start": "2026-01-01T09:00:00+00:00",
                        "end": "2026-01-01T18:00:00+00:00"}]


def test_house_empty_windows_no_rooms_at_all_makes_no_claim():
    log = OccupancyLog(":memory:", retention_days=None)
    windows = digest.house_empty_windows(log, start=START, end=END)
    assert windows == []  # no rooms at all -> no data -> no claim


def test_house_empty_windows_treats_unknown_prior_state_as_occupied():
    # No row exists BEFORE `start` for this room -- the state at `start` is
    # unknown. house_empty_windows must NOT fabricate "empty" for that unknown
    # span (conservative: unknown => occupied) -- only the observed
    # 09:00-onward empty span counts.
    log = OccupancyLog(":memory:", retention_days=None)
    log.append_if_changed("kitchen", False, 0.9, None, "2026-01-01T09:00:00+00:00")
    windows = digest.house_empty_windows(log, start=START, end=END)
    assert windows == [{"start": "2026-01-01T09:00:00+00:00",
                        "end": "2026-01-02T00:00:00+00:00"}]


def test_house_empty_windows_multi_room_requires_all_empty():
    log = OccupancyLog(":memory:", retention_days=None)
    # kitchen empty all day, living room occupied all day -> house never empty
    log.append_if_changed("kitchen", False, 0.9, None, "2025-12-31T22:00:00+00:00")
    log.append_if_changed("living_room", True, 0.9, None, "2025-12-31T22:00:00+00:00")
    windows = digest.house_empty_windows(log, start=START, end=END,
                                          rooms=["kitchen", "living_room"])
    assert windows == []


# --------------------------------------------------------------------------- #
# compose_digest: pure/local, no egress, privacy-bounded
# --------------------------------------------------------------------------- #
def test_compose_digest_matches_the_design_brief_example():
    log = _log_one_room_empty_workday()
    result = digest.compose_digest(
        occupancy_log=log, house_status={"status": "ok"},
        alert_count=0, new_device_count=1, start=START, end=END, now=END)

    assert "house empty 09:00-18:00" in result["text"]
    assert "1 new device" in result["text"]
    assert result["date"] == "2026-01-01"
    # only 1 day of history -> is_unusual() can't be trusted yet (honest, not fabricated)
    assert result["fields"]["routine_status"] == "insufficient_data"
    assert set(result["fields"].keys()) <= digest._ALLOWED_FIELD_KEYS


def test_compose_digest_with_no_inputs_never_crashes():
    result = digest.compose_digest(alert_count=0, new_device_count=0, now=END)
    assert result["fields"]["empty_windows"] == []
    assert result["fields"]["routine_status"] == "insufficient_data"
    assert "house_status" not in result["fields"]  # additive-optional, omitted when absent
    assert set(result["fields"].keys()) <= digest._ALLOWED_FIELD_KEYS


def test_compose_digest_pluralizes_counts():
    result = digest.compose_digest(alert_count=2, new_device_count=3, now=END)
    assert "3 new devices" in result["text"]
    assert "2 alerts" in result["text"]


def test_compose_digest_surfaces_house_status_only_when_not_ok():
    ok = digest.compose_digest(house_status={"status": "ok"}, now=END)
    assert "house status" not in ok["text"]
    alert = digest.compose_digest(house_status={"status": "alert"}, now=END)
    assert "house status: alert" in alert["text"]


def test_compose_digest_never_leaks_identity_geometry_or_room_names():
    log = _log_one_room_empty_workday()
    result = digest.compose_digest(occupancy_log=log, house_status={"status": "ok"},
                                    alert_count=1, new_device_count=1,
                                    start=START, end=END, now=END)
    blob = str(result).lower()
    # house-level only: never a room name, never raw sensing/identity detail.
    for forbidden in ("kitchen", "mac", "identity", "vitals", "coordinate",
                      "frame", "target", "rtsp"):
        assert forbidden not in blob


# --------------------------------------------------------------------------- #
# send_digest: gated, default-OFF, Telegram-first / ntfy-fallback
# --------------------------------------------------------------------------- #
def test_send_digest_is_noop_when_nothing_enabled():
    result = digest.send_digest({"text": "house empty 09:00-18:00"})
    assert result == {"ok": False, "status": "no_enabled_connector", "via": None}


def test_send_digest_routes_through_telegram_when_enabled():
    seen = []

    def fake_telegram_send(**kwargs):
        seen.append(kwargs)
        return {"ok": True, "status": "sent"}

    result = digest.send_digest({"text": "house empty 09:00-18:00"},
                                 telegram_send=fake_telegram_send)

    assert result == {"ok": True, "status": "sent", "via": "telegram"}
    assert seen[0]["kind"] == "digest"
    assert seen[0]["summary"] == "house empty 09:00-18:00"


def test_send_digest_falls_back_to_ntfy_when_telegram_not_ok():
    ntfy_calls = []
    result = digest.send_digest(
        {"text": "x"},
        telegram_send=lambda **kw: {"ok": False, "status": "disabled"},
        ntfy_notify=lambda msg: ntfy_calls.append(msg))

    assert result == {"ok": True, "status": "sent", "via": "ntfy"}
    assert ntfy_calls == ["Wavr daily digest: x"]


def test_send_digest_never_double_sends_when_telegram_succeeds():
    ntfy_calls = []
    result = digest.send_digest(
        {"text": "x"},
        telegram_send=lambda **kw: {"ok": True, "status": "sent"},
        ntfy_notify=lambda msg: ntfy_calls.append(msg))

    assert result["via"] == "telegram"
    assert ntfy_calls == []  # ntfy never touched once telegram already delivered it


def test_send_digest_integration_zero_network_when_telegram_disabled(monkeypatch):
    # Full stack: a disabled broker + the REAL telegram connector + a fake HTTP
    # client, routed through send_digest -- proves the whole chain fails
    # closed with zero network attempted end to end.
    monkeypatch.setenv("WAVR_TELEGRAM_TOKEN", "tok")
    monkeypatch.setenv("WAVR_TELEGRAM_CHAT_ID", "chat")
    post = FakePost()
    send = telegram.make_telegram_send(FakeStore(), post=post)  # broker: nothing enabled

    result = digest.send_digest({"text": "house empty 09:00-18:00"}, telegram_send=send)

    assert result["ok"] is False
    assert post.calls == []
