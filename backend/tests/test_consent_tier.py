"""Adversarial tests for the per-device CONSENT-TIER gating (privacy centerpiece).

TIERS are monotone and SUBTRACTIVE (green ⊃ yellow ⊃ red): consent can only ever DROP or
REDUCE what a device contributes -- it NEVER raises the phone weight (0.5), the
present_confidence (0.8), the threshold, or the A1.3 coarse floor.

  green  -> full telemetry ingested; device NAMED in who's-home; coarse presence vote.
  yellow -> telemetry ingested but REDUCED server-side (rssi/ssid/bssid=None, sensors={});
            device votes present but ANONYMOUS (never named in who's-home).
  red    -> telemetry DROPPED server-side (never reaches the hub); not present; not named.

Same forged-in-subnet TestClient technique as test_telemetry_sensor.py (real middleware +
routes), and the same injected-clock / FakeHub technique as test_phone_source.py for the
source-level checks. No real sleeps; every clock is injected.
"""
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.devices import DeviceStore, VALID_CONSENT
from wavr.fusion import FusionEngine, _DEFAULT_FRESHNESS_S, _DEFAULT_STALE_S
from wavr.events import SensingEvent
from wavr.sources.phone import PhoneSensorSource
from wavr.sources.simulated import SimulatedSource
from wavr.storage import Storage
from wavr.telemetry import PerDeviceRateLimiter, TelemetryReading

CSRF = {"X-Wavr-Local": "1"}        # loopback root's CSRF header
_FRESHNESS = _DEFAULT_FRESHNESS_S   # 30s
_STALE = _DEFAULT_STALE_S           # 90s


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    """The multidevice app plus references to the pieces a consent test inspects: the
    injected Storage (row-count guard) and the DeviceStore db path (schema guard)."""
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    db_path = str(tmp_path / "md.db")
    monkeypatch.setenv("WAVR_DB", db_path)
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    storage = Storage(":memory:")
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=storage, camera_store=CameraStore(":memory:"))
    return SimpleNamespace(app=app, storage=storage, db_path=db_path)


def _pair(app, role="sensor"):
    """Central (loopback root) mints a code; a forged in-subnet LAN peer redeems it.
    Returns (peer_client, auth_headers, device_id). New devices default to green."""
    central = TestClient(app)
    code = central.post("/api/pair-code", json={"role": role}, headers=CSRF).json()["code"]
    peer = TestClient(app, client=("192.168.1.50", 12345))
    body = peer.post("/api/pair", json={"code": code, "device_name": f"{role}-dev"}).json()
    return peer, {"Authorization": f"Bearer {body['token']}"}, body["device_id"]


def _sample_payload():
    return {
        "device": "phone",
        "sensors": {"accel": [0.0, 0.1, 9.8], "gyro": [0.0, 0.0, 0.0], "pressure": [1013.2]},
        "battery_pct": 72, "charging": "CHARGING", "rssi": -47, "ssid": "home",
        "bssid": "aa:bb:cc:dd:ee:ff",
    }


class _Clock:
    """Injectable clock the test advances by hand (no real time)."""

    def __init__(self, base=None):
        from datetime import datetime, timezone
        self.t = base or datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        from datetime import timedelta
        self.t = self.t + timedelta(seconds=seconds)


class _FakeHub:
    """TelemetryHub double: get() returns queued readings, then raises TimeoutError to
    simulate a silent tick (exactly what asyncio.wait_for raises), so the source runs with
    zero real waiting."""

    def __init__(self, readings=()):
        self._readings = list(readings)

    async def get(self):
        if self._readings:
            return self._readings.pop(0)
        raise TimeoutError


def _reading(device_id, ts=None):
    ts = ts or _Clock().t
    return TelemetryReading(device_id=device_id, ts=ts.isoformat())


# --- 1: RED dropped server-side ----------------------------------------------------

def test_1_red_dropped_server_side(ctx):
    peer, auth, device_id = _pair(ctx.app)
    hub = ctx.app.state.telemetry_hub
    assert hub.qsize() == 0

    # withdraw to red
    r = peer.post("/api/consent", json={"level": "red"}, headers=auth)
    assert r.status_code == 200 and r.json() == {"device_id": device_id, "level": "red"}

    # red POST: accepted False, consent red, HTTP 200 (NOT 4xx), nothing enqueued
    rp = peer.post("/api/telemetry", json=_sample_payload(), headers=auth)
    assert rp.status_code == 200
    assert rp.json() == {"accepted": False, "device_id": device_id, "consent": "red"}
    assert hub.qsize() == 0                       # dropped server-side, never reached the hub

    # flip green: the IDENTICAL POST is now accepted and enqueued (qsize 1)
    assert peer.post("/api/consent", json={"level": "green"},
                     headers=auth).status_code == 200
    rg = peer.post("/api/telemetry", json=_sample_payload(), headers=auth)
    assert rg.status_code == 200
    assert rg.json() == {"accepted": True, "device_id": device_id}
    assert hub.qsize() == 1


# --- 2: RED -> no phone vote end-to-end --------------------------------------------

async def test_2_red_no_phone_vote_end_to_end(ctx):
    peer, auth, device_id = _pair(ctx.app)
    peer.post("/api/consent", json={"level": "red"}, headers=auth)
    r = peer.post("/api/telemetry", json=_sample_payload(), headers=auth)
    assert r.json()["accepted"] is False

    hub = ctx.app.state.telemetry_hub
    assert hub.qsize() == 0                       # nothing for the source to fold

    # The phone source's ONLY input is that (empty) hub -> the fused casa vote is absent.
    src = PhoneSensorSource(hub, get_consent=lambda d: "red", now_fn=_Clock(), tick=0.01)
    agen = src.events()
    ev = await agen.__anext__()
    await agen.aclose()
    assert ev.room == "casa"
    assert ev.presence is False                   # no phone presence for casa
    assert ev.confidence == 0.0
    assert src.whos_home() == []


# --- 3: YELLOW reduced --------------------------------------------------------------

def test_3_yellow_reduced(ctx):
    peer, auth, device_id = _pair(ctx.app)
    assert peer.post("/api/consent", json={"level": "yellow"},
                     headers=auth).status_code == 200

    r = peer.post("/api/telemetry", json=_sample_payload(), headers=auth)
    assert r.status_code == 200
    assert r.json() == {"accepted": True, "device_id": device_id}   # still votes present

    reading = ctx.app.state.telemetry_hub._q.get_nowait()
    assert reading.device_id == device_id
    # network-locating identifiers + raw sensors stripped server-side
    assert reading.rssi is None
    assert reading.ssid is None
    assert reading.bssid is None
    assert reading.sensors == {}
    # battery/charging survive (no location; harmless for the coarse vote)
    assert reading.battery_pct == 72
    assert reading.charging == "CHARGING"


# --- 4: body can't claim a higher level; effective tier is the stored column --------

def test_4_body_cannot_claim_higher_level(ctx):
    peer, auth, device_id = _pair(ctx.app)
    # store red first
    assert peer.post("/api/consent", json={"level": "red"}, headers=auth).status_code == 200

    # POST {"consent":"green"} to the consent endpoint -> 422 (extra=forbid, wrong field).
    assert peer.post("/api/consent", json={"consent": "green"},
                     headers=auth).status_code == 422
    # a body trying to name another device -> 422 (extra=forbid): consent is self-scoped.
    assert peer.post("/api/consent", json={"level": "green", "device_id": "other"},
                     headers=auth).status_code == 422
    # a telemetry body trying to smuggle a higher tier inline -> 422 (TelemetryPayload
    # forbids extra keys too).
    assert peer.post("/api/telemetry", json={**_sample_payload(), "consent": "green"},
                     headers=auth).status_code == 422

    # effective tier is the STORED column: a clean telemetry POST is still dropped as red.
    rp = peer.post("/api/telemetry", json=_sample_payload(), headers=auth)
    assert rp.json() == {"accepted": False, "device_id": device_id, "consent": "red"}


# --- 5: RED pauses identity binding (whos_home omits + stale-entry skip) ------------

async def test_5_red_pauses_identity_binding():
    clock = _Clock()
    consent = {"devA": "green"}
    labels = {"devA": "Augusto's Phone"}

    # (a) green -> named. Flip to red -> whos_home omits the label IMMEDIATELY (binding
    #     paused) even though the coarse entry lingers within freshness.
    src = PhoneSensorSource(_FakeHub([_reading("devA")]),
                            get_label=labels.get, get_consent=consent.get, now_fn=clock)
    agen = src.events()
    await agen.__anext__()
    assert src.whos_home() == ["Augusto's Phone"]
    consent["devA"] = "red"
    assert src.whos_home() == []                  # not named -> identity binding paused
    await agen.aclose()

    # (b) defense-in-depth: a reading admitted microseconds before the RED flip must not
    #     linger -- the consume-side re-check skips recording _last_seen when now-red.
    clock2 = _Clock()
    consent2 = {"devA": "red"}                     # already red by the time it is consumed
    src2 = PhoneSensorSource(_FakeHub([_reading("devA")]),
                             get_consent=consent2.get, now_fn=clock2)
    agen2 = src2.events()
    ev = await agen2.__anext__()
    await agen2.aclose()
    assert ev.presence is False                    # the stale-red reading was NOT recorded
    assert src2._last_seen == {}


# --- 6: re-consent resumes (no backfill of the red gap) ----------------------------

async def test_6_reconsent_resumes_no_backfill():
    clock = _Clock()
    consent = {"devA": "green"}
    labels = {"devA": "phoneA"}
    hub = _FakeHub([_reading("devA", clock.t)])    # reading1 pre-loaded (green era)
    src = PhoneSensorSource(hub, get_label=labels.get, get_consent=consent.get,
                            now_fn=clock, freshness_s=_FRESHNESS)
    agen = src.events()

    ev1 = await agen.__anext__()                   # reading1 -> present + named
    assert ev1.presence is True
    assert src.whos_home() == ["phoneA"]

    # withdraw red: the app would now DROP posts, so the source only sees silence.
    consent["devA"] = "red"
    assert src.whos_home() == []                   # gone (binding paused)

    # age past freshness with NO new votes (no backfill of the red gap).
    clock.advance(_FRESHNESS + 1)
    ev_absent = await agen.__anext__()             # silent tick -> pruned
    assert ev_absent.presence is False
    assert src.whos_home() == []

    # re-consent green + a NEW post arrives -> repopulate -> reappears (resumed, no gap fill).
    consent["devA"] = "green"
    hub._readings.append(_reading("devA", clock.t))
    ev2 = await agen.__anext__()
    assert ev2.presence is True
    assert src.whos_home() == ["phoneA"]
    await agen.aclose()


# --- 7: coarse floor holds under every tier ----------------------------------------

async def test_7_coarse_floor_holds_every_tier():
    # green AND yellow both still vote present at conf 0.8, but a lone phone
    # (weight 0.5 x 0.8 = 0.4) < 0.5 threshold -> NOT occupied. Consent never RAISES this.
    for tier in ("green", "yellow"):
        clock = _Clock()
        consent = {"devA": tier}
        src = PhoneSensorSource(_FakeHub([_reading("devA")]),
                                get_consent=consent.get, now_fn=clock)
        agen = src.events()
        ev = await agen.__anext__()
        await agen.aclose()
        assert ev.presence is True and ev.confidence == 0.8, tier

        engine = FusionEngine(now_fn=clock)
        rs = engine.update(ev)
        assert rs.confidence == 0.4, tier          # 0.5 x 0.8 -- unchanged by consent
        assert rs.occupied is False, tier          # lone phone can never cross the floor

    # red -> the reading is skipped at the source (queue-residue re-check) -> no vote.
    clock = _Clock()
    consent = {"devA": "red"}
    src = PhoneSensorSource(_FakeHub([_reading("devA")]),
                            get_consent=consent.get, now_fn=clock)
    agen = src.events()
    ev = await agen.__anext__()
    await agen.aclose()
    assert ev.presence is False and ev.confidence == 0.0


# --- 8: accept-and-drop still rate-limits (red flood -> 429) ------------------------

def test_8_red_flood_still_rate_limits(ctx):
    peer, auth, _ = _pair(ctx.app)
    peer.post("/api/consent", json={"level": "red"}, headers=auth)
    # Rate-limit is checked BEFORE the consent drop, so a red flood still 429s -- a red
    # device cannot escape the limiter merely because its readings are dropped.
    ctx.app.state.telemetry_limiter = PerDeviceRateLimiter(
        capacity=3, refill_per_sec=0, clock=lambda: 0.0)
    codes = [peer.post("/api/telemetry", json=_sample_payload(), headers=auth).status_code
             for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]      # first 3 are accept-and-drop, then 429
    assert ctx.app.state.telemetry_hub.qsize() == 0  # none of them were ingested


# --- 9: decay not abrupt (green voting -> red -> confidence decays gradually) --------

async def test_9_red_decay_is_gradual_not_abrupt():
    clock = _Clock()
    engine = FusionEngine(now_fn=clock)            # default freshness 30 / stale 90
    ev = SensingEvent(room="casa", modality="phone", presence=True, motion=0.0,
                      breathing_bpm=None, heart_bpm=None, confidence=0.8,
                      ts=clock.t.isoformat(), targets=())
    fresh = engine.update(ev)
    assert fresh.confidence == 0.4                 # green phone voting present

    # The device goes RED: the app drops its posts, so NO new event reaches fusion (there is
    # NO synthetic presence=False injected). The last vote decays with the clock, gradually.
    clock.advance(60)                              # halfway into the 30..90 decay band
    mid = engine.state("casa")
    assert 0.0 < mid.confidence < 0.4              # decaying, not an abrupt flip
    assert abs(mid.confidence - 0.2) < 1e-9        # 0.5 x 0.8 x ((90-60)/60=0.5)
    assert mid.occupied is False

    clock.advance(31)                              # past STALE_S
    gone = engine.state("casa")
    assert gone.confidence == 0.0                  # fully decayed
    assert gone.occupied is False


# --- 10: ADR-0002 guard (no new table; a dropped red reading leaves storage unchanged) -

def test_10_adr0002_no_new_table_red_no_storage(ctx):
    peer, auth, device_id = _pair(ctx.app)

    # (a) consent is a COLUMN on `devices`, NOT a new table -- telemetry adds no store.
    conn = sqlite3.connect(ctx.db_path)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        cols = {r[1] for r in conn.execute("PRAGMA table_info(devices)")}
    finally:
        conn.close()
    assert "consent" in cols
    assert "consent" not in tables and "consents" not in tables

    # (b) a dropped red reading writes NOTHING to storage (row count unchanged).
    before = len(ctx.storage.recent(limit=10000))
    peer.post("/api/consent", json={"level": "red"}, headers=auth)
    peer.post("/api/telemetry", json=_sample_payload(), headers=auth)
    after = len(ctx.storage.recent(limit=10000))
    assert after == before == 0


# --- PLUS: a sensor-role token CAN POST /api/consent but reaches no read route -------

def test_plus_sensor_can_post_consent_but_stays_confined(ctx):
    peer, auth, device_id = _pair(ctx.app, "sensor")
    # allowlisted GDPR-withdrawal path: a sensor CAN set its OWN consent
    r = peer.post("/api/consent", json={"level": "yellow"}, headers=auth)
    assert r.status_code == 200 and r.json() == {"device_id": device_id, "level": "yellow"}
    # ...but confinement is intact: still 403 on every read route (opens no read surface)
    for route in ("/api/state", "/api/history", "/api/inventory", "/api/status",
                  "/api/devices"):
        assert peer.get(route, headers=auth).status_code == 403, route


# --- PLUS: consent via /api/consent affects only the caller's own device_id ---------

def test_plus_consent_is_self_scoped(ctx):
    a_peer, a_auth, a_id = _pair(ctx.app, "sensor")
    b_peer, b_auth, b_id = _pair(ctx.app, "sensor")
    assert a_id != b_id

    # A withdraws to red; B is untouched (still default green).
    assert a_peer.post("/api/consent", json={"level": "red"},
                       headers=a_auth).status_code == 200
    # A is dropped; B still ingests normally -> the change moved only A's row.
    assert a_peer.post("/api/telemetry", json=_sample_payload(),
                       headers=a_auth).json()["accepted"] is False
    rb = b_peer.post("/api/telemetry", json=_sample_payload(), headers=b_auth)
    assert rb.json() == {"accepted": True, "device_id": b_id}


# --- PLUS: /api/consent auth + validation contract ----------------------------------

def test_plus_consent_endpoint_auth_and_validation(ctx):
    peer, auth, device_id = _pair(ctx.app, "sensor")
    # loopback root (no device token) -> 401 from the handler
    central = TestClient(ctx.app)
    assert central.post("/api/consent", json={"level": "green"},
                        headers=CSRF).status_code == 401
    # well-typed but out-of-set level -> 422 (VALID_CONSENT check)
    assert peer.post("/api/consent", json={"level": "blue"}, headers=auth).status_code == 422
    # every valid tier accepted
    for level in sorted(VALID_CONSENT):
        r = peer.post("/api/consent", json={"level": level}, headers=auth)
        assert r.status_code == 200 and r.json() == {"device_id": device_id, "level": level}


# --- PLUS: DeviceStore consent migration is idempotent on an existing v1 db ----------

def test_plus_consent_migration_is_idempotent(tmp_path):
    # Build a pre-consent (schema v1) devices table WITHOUT the consent column.
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE devices (
            device_id    TEXT PRIMARY KEY,
            name         TEXT    NOT NULL,
            role         TEXT    NOT NULL,
            token_hash   TEXT    NOT NULL UNIQUE,
            created_ts   TEXT    NOT NULL,
            last_seen_ts TEXT,
            revoked      INTEGER NOT NULL DEFAULT 0
        );
    """)
    conn.execute(
        "INSERT INTO devices (device_id, name, role, token_hash, created_ts, revoked)"
        " VALUES ('d1', 'legacy', 'user', 'hash-1', '2026-01-01T00:00:00+00:00', 0)")
    conn.commit()
    conn.close()

    # Opening the store must ADD the column and backfill the prior row to 'green'.
    store = DeviceStore(path)
    assert store.get("d1").consent == "green"          # existing device backfilled
    assert store.set_consent("d1", "yellow") is True
    assert store.get_consent("d1") == "yellow"
    store.close()

    # Re-open: migration is a no-op (column already present) and the value persists.
    store2 = DeviceStore(path)
    assert store2.get_consent("d1") == "yellow"
    store2.close()
