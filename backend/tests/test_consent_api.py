"""Route tests for GET/POST /api/consent (device-scope participation
tri-color, 2026-07-11 mobile companion reconciliation) + the register-companion
enforcement it backs. Uses the same forged-LAN-peer technique as
test_companion_presence_api.py/test_multidevice_integration.py
(`TestClient(app, client=(host, port))` + monkeypatched `_local_ipv4`).
"""
import asyncio

from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.identity_store import ROOT_DEVICE_ID, IdentityStore
from wavr.sources.network import NetworkSource
from wavr.storage import Storage
from wavr.camera_store import CameraStore
from wavr.sources.simulated import SimulatedSource

CSRF = {"X-Wavr-Local": "1"}


def _fake_resolver(mapping: dict):
    async def resolve(ip):
        return mapping.get(ip)
    return resolve


def _md_app(tmp_path, monkeypatch, resolver_map=None, identity=None):
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    # DeviceStore always opens cfg.db_path -- isolate it per test (same
    # convention as test_companion_presence_api.py's _md_app) or devices would
    # leak into the real project wavr.db across test runs.
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "md.db"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    store = identity or IdentityStore(str(tmp_path / "id.db"))
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"),
        identity_store=store, companion_resolve_mac=_fake_resolver(resolver_map or {}))
    return app, store


def _pair(app, role):
    central = TestClient(app)
    code = central.post("/api/pair-code", json={"role": role}, headers=CSRF).json()["code"]
    peer = TestClient(app, client=("192.168.1.50", 12345))
    token = peer.post("/api/pair", json={"code": code, "device_name": "phone"}).json()["token"]
    return peer, {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Root: has no Device row -- its lever is /api/system/toggle, so /api/consent
# 409s rather than fabricating a value. Tested so the loopback dashboard (which
# never calls this route today) can't accidentally crash if it ever did.
# --------------------------------------------------------------------------- #
def test_root_get_consent_409(tmp_path, monkeypatch):
    app, _store = _md_app(tmp_path, monkeypatch)
    with TestClient(app, headers=CSRF) as c:
        r = c.get("/api/consent")
        assert r.status_code == 409
        assert "system/toggle" in r.json()["detail"]


def test_root_post_consent_409(tmp_path, monkeypatch):
    app, _store = _md_app(tmp_path, monkeypatch)
    with TestClient(app, headers=CSRF) as c:
        r = c.post("/api/consent", json={"level": "red"})
        assert r.status_code == 409


# --------------------------------------------------------------------------- #
# A paired LAN companion: self-resolved, no body device_id anywhere.
# --------------------------------------------------------------------------- #
def test_default_consent_is_green(tmp_path, monkeypatch):
    app, _store = _md_app(tmp_path, monkeypatch)
    peer, auth = _pair(app, "user")
    r = peer.get("/api/consent", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["level"] == "green"
    assert "device_id" in body


def test_set_and_get_consent_roundtrip(tmp_path, monkeypatch):
    app, _store = _md_app(tmp_path, monkeypatch)
    peer, auth = _pair(app, "user")
    r = peer.post("/api/consent", json={"level": "yellow"}, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["level"] == "yellow" and body["device_id"]
    r2 = peer.get("/api/consent", headers=auth)
    assert r2.json()["level"] == "yellow"


def test_invalid_consent_level_422(tmp_path, monkeypatch):
    app, _store = _md_app(tmp_path, monkeypatch)
    peer, auth = _pair(app, "user")
    r = peer.post("/api/consent", json={"level": "blue"}, headers=auth)
    assert r.status_code == 422


def test_lan_peer_needs_no_csrf_header(tmp_path, monkeypatch):
    # An authenticated LAN peer proved possession of a bearer token already --
    # unlike root, it must NOT also need X-Wavr-Local.
    app, _store = _md_app(tmp_path, monkeypatch)
    peer, auth = _pair(app, "user")
    r = peer.post("/api/consent", json={"level": "red"}, headers=auth)
    assert r.status_code == 200


def test_unauthenticated_lan_peer_forbidden(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "md.db"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    app = create_app(sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
                     storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))
    peer = TestClient(app, client=("192.168.1.50", 12345))
    r = peer.get("/api/consent")
    assert r.status_code == 403


def test_revoked_device_cannot_read_consent(tmp_path, monkeypatch):
    app, _store = _md_app(tmp_path, monkeypatch)
    peer, auth = _pair(app, "user")
    central = TestClient(app)
    devs = central.get("/api/devices", headers=CSRF).json()["devices"]
    central.delete(f"/api/devices/{devs[0]['device_id']}", headers=CSRF)
    r = peer.get("/api/consent", headers=auth)
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Enforcement at register_companion: red drops, yellow anonymizes, green full.
# --------------------------------------------------------------------------- #
def test_red_consent_drops_registration_server_side(tmp_path, monkeypatch):
    app, store = _md_app(tmp_path, monkeypatch,
                         resolver_map={"192.168.1.50": "11:22:33:44:55:66"})
    peer, auth = _pair(app, "user")
    peer.post("/api/consent", json={"level": "red"}, headers=auth)
    r = peer.post("/api/presence/register-companion", json={"label": "housemate"},
                  headers=auth)
    assert r.status_code == 200
    assert r.json() == {"mac_registered": False, "reason": "consent-withdrawn"}
    # Never even reaches identity_store -- a patched client that keeps sending
    # after withdrawal still can't get a row written.
    assert store.as_net_map() == {}


def test_yellow_consent_registers_without_name_label(tmp_path, monkeypatch):
    app, store = _md_app(tmp_path, monkeypatch,
                         resolver_map={"192.168.1.50": "11:22:33:44:55:66"})
    peer, auth = _pair(app, "user")
    peer.post("/api/consent", json={"level": "yellow"}, headers=auth)
    r = peer.post("/api/presence/register-companion", json={"label": "housemate"},
                  headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["mac_registered"] is True
    assert body["label"] is None
    assert body["mac_prefix"] == "11:22:33"
    # An ANONYMOUS identity row: present so the MAC is counted, never named.
    #
    # This assertion used to read `store.get(...) is None` -- "yellow writes no row
    # at all". That was asserting the DEFECT, not the contract: an unwritten MAC is
    # not in the known set, and only a known MAC counts toward presence, so yellow
    # delivered nothing whatsoever and was indistinguishable from red. The contract
    # is "counted as home, without a name", which needs a row that carries no name.
    assert store.as_net_map() == {}, "must never be named"
    row = store.get("11:22:33:44:55:66")
    assert row is not None, "must exist, or it cannot be counted as present"
    assert row["person"] == "", "must carry no name at rest, not just at read time"
    assert "housemate" not in str(row), "the declined label must not be stored anywhere"


def test_green_consent_registers_full_named_presence(tmp_path, monkeypatch):
    app, store = _md_app(tmp_path, monkeypatch,
                         resolver_map={"192.168.1.50": "11:22:33:44:55:66"})
    peer, auth = _pair(app, "user")
    # green is the default -- no explicit POST /api/consent needed.
    r = peer.post("/api/presence/register-companion", json={"label": "housemate"},
                  headers=auth)
    assert r.status_code == 200
    assert r.json()["mac_registered"] is True
    assert r.json()["label"] == "housemate"
    assert store.as_net_map() == {"11:22:33:44:55:66": "housemate"}


def test_root_register_companion_unaffected_by_consent_column(tmp_path, monkeypatch):
    # Web-mode/loopback byte-identical regression guard: root has no Device
    # row, so it must always behave as "green" -- unchanged from before this
    # feature existed.
    app, store = _md_app(tmp_path, monkeypatch,
                         resolver_map={"testclient": "aa:bb:cc:dd:ee:ff"})
    with TestClient(app, headers=CSRF) as c:
        r = c.post("/api/presence/register-companion", json={"label": "alice"})
        assert r.status_code == 200
        assert r.json() == {"mac_registered": True, "label": "alice",
                            "mac_prefix": "aa:bb:cc"}
    assert store.as_net_map() == {"aa:bb:cc:dd:ee:ff": "alice"}


# --------------------------------------------------------------------------- #
# GET /api/devices/me -- read-back of the caller's own role.
# --------------------------------------------------------------------------- #
def test_devices_me_root(tmp_path, monkeypatch):
    app, _store = _md_app(tmp_path, monkeypatch)
    with TestClient(app, headers=CSRF) as c:
        r = c.get("/api/devices/me")
        assert r.status_code == 200
        assert r.json()["role"] == "root"


def test_devices_me_user_peer(tmp_path, monkeypatch):
    app, _store = _md_app(tmp_path, monkeypatch)
    peer, auth = _pair(app, "user")
    r = peer.get("/api/devices/me", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "user"
    assert body["device_id"]


# --------------------------------------------------------------------------- #
# What each level actually DELIVERS (2026-07-16). The tests above only prove the
# register-companion route's write-time branch; these prove the LIVE behaviour
# the three colours promise, which is a different thing and is what the owner
# actually specified:
#
#   red    -> nothing            (no presence, no name -- ever)
#   yellow -> limited presence   (counted as home, WITHOUT a name)
#   green  -> the maximum        (counted as home, WITH a name)
#
# `app.state.net_known_provider` is the seam NetworkSource re-reads every scan
# cycle (same test-seam convention as app.state.refuse_once / camera_health):
# {mac: person} feeds presence AND labels, and a None value means "counts toward
# presence, carries no name" -- the shape yellow needs and had no way to express.
# --------------------------------------------------------------------------- #
MAC = "11:22:33:44:55:66"


def _registered_peer(tmp_path, monkeypatch, level=None):
    """Pair a companion, optionally set its consent, then let it self-register --
    exactly the order the mobile shim uses (applyConsentLocal -> applyPresence)."""
    app, store = _md_app(tmp_path, monkeypatch, resolver_map={"192.168.1.50": MAC})
    peer, auth = _pair(app, "user")
    if level is not None:
        peer.post("/api/consent", json={"level": level}, headers=auth)
    peer.post("/api/presence/register-companion", json={"label": "housemate"},
              headers=auth)
    return app, store, peer, auth


def test_green_delivers_named_presence(tmp_path, monkeypatch):
    app, _store, _peer, _auth = _registered_peer(tmp_path, monkeypatch)  # green default
    assert app.state.net_known_provider() == {MAC: "housemate"}


def test_yellow_delivers_presence_without_a_name(tmp_path, monkeypatch):
    """Yellow's PROMISE (mobile onboarding copy): "counted as home, without a
    name". Before this, yellow wrote NO row at all -> the mac never reached the
    known-provider -> `known & seen` (network.py:219) could not match it -> it
    contributed exactly NOTHING, i.e. it was byte-identical to red."""
    app, store, _peer, _auth = _registered_peer(tmp_path, monkeypatch, "yellow")
    prov = app.state.net_known_provider()
    assert MAC in prov, "yellow must be COUNTED toward presence"
    assert prov[MAC] is None, "yellow must never carry a name"
    assert store.as_net_map() == {}, "the named map must not expose a yellow device"


def test_red_delivers_nothing(tmp_path, monkeypatch):
    app, store, _peer, _auth = _registered_peer(tmp_path, monkeypatch, "red")
    assert app.state.net_known_provider() == {}
    assert store.as_net_map() == {}


def test_withdrawing_to_red_after_green_stops_presence_server_side(tmp_path, monkeypatch):
    """The withdrawal case the consent axis exists for. The shim DELETEs its own
    row on red (shim applyPresence: "register on green/yellow, DELETE on red"),
    but that is a CLIENT promise -- an offline, crashed, or patched client never
    sends it. The hub must stop counting the device on its own."""
    app, store, peer, auth = _registered_peer(tmp_path, monkeypatch)  # green
    assert app.state.net_known_provider() == {MAC: "housemate"}
    peer.post("/api/consent", json={"level": "red"}, headers=auth)
    # NOTE: no DELETE /api/presence/register-companion -- withdrawal must hold
    # without the client's cooperation.
    assert app.state.net_known_provider() == {}, "a withdrawn device must not be presence"
    assert store.as_net_map() == {}, "a withdrawn device must not stay named"


def test_downgrading_to_yellow_after_green_drops_the_name_keeps_presence(tmp_path, monkeypatch):
    app, store, peer, auth = _registered_peer(tmp_path, monkeypatch)  # green
    peer.post("/api/consent", json={"level": "yellow"}, headers=auth)
    prov = app.state.net_known_provider()
    assert prov == {MAC: None}, "downgrade keeps presence, drops the name"
    assert store.as_net_map() == {}


def test_yellow_is_counted_by_the_REAL_network_source_without_an_identity(tmp_path, monkeypatch):
    """End-to-end through the actual source, not the provider seam. Everything
    above asserts what the provider HANDS OVER; this asserts what NetworkSource
    DOES with it -- that a yellow MAC survives the `known & seen` intersection into
    a present event, and that no Identity (PII) rides along."""
    app, _store, _peer, _auth = _registered_peer(tmp_path, monkeypatch, "yellow")

    async def _scan():
        return {MAC}

    src = NetworkSource(known_macs=set(), scan=_scan, interval=0,
                        known_provider=app.state.net_known_provider,
                        emit_identity=True)   # ON: prove the SOURCE withholds it
    ev = asyncio.run(anext(src.events()))
    assert ev.presence is True, "a yellow device must make the house read occupied"
    assert ev.identities == (), "a yellow device must never emit an Identity"


def test_green_is_named_by_the_REAL_network_source(tmp_path, monkeypatch):
    """The control for the test above: same path, same scan, green -> named."""
    app, _store, _peer, _auth = _registered_peer(tmp_path, monkeypatch)

    async def _scan():
        return {MAC}

    src = NetworkSource(known_macs=set(), scan=_scan, interval=0,
                        known_provider=app.state.net_known_provider,
                        emit_identity=True)
    ev = asyncio.run(anext(src.events()))
    assert ev.presence is True
    assert [i.person for i in ev.identities] == ["housemate"]


def test_legacy_row_with_no_device_id_link_fails_closed(tmp_path, monkeypatch):
    """THE UPGRADE PATH. `ALTER TABLE ADD COLUMN device_id` cannot backfill a link
    that was never recorded, so every companion row written before this column
    existed reads back NULL. Such a row has no consent level to look up -- and if
    that resolves to "green" it is pinned green FOREVER, because POST /api/consent
    can never reach it and register-companion early-returns on red, so the owner
    would have to GRANT consent in order to withdraw it. It fails closed instead."""
    app, store, peer, auth = _registered_peer(tmp_path, monkeypatch)  # green + linked
    assert app.state.net_known_provider() == {MAC: "housemate"}
    # Exactly the pre-migration state: the row is there, the link is not.
    store._conn.execute("UPDATE identity_devices SET device_id = NULL WHERE address = ?",
                        (MAC,))
    store._conn.commit()
    assert app.state.net_known_provider() == {}, "an unlinkable row must not be trusted"
    # ...and it self-heals: the shim re-registers on every boot/attach/resume, which
    # re-links the row via _write's COALESCE. No manual repair, no lost device.
    peer.post("/api/presence/register-companion", json={"label": "housemate"},
              headers=auth)
    assert app.state.net_known_provider() == {MAC: "housemate"}


def test_root_own_registration_is_not_mistaken_for_a_legacy_row(tmp_path, monkeypatch):
    """Root holds no Device row by design (its lever is /api/system/toggle), so its
    own registration would write a NULL link and fail closed with the legacy rows --
    silencing the operator's own box. It carries the reserved sentinel instead."""
    # "testclient" is TestClient's own source host -- the loopback/root path here.
    app, store = _md_app(tmp_path, monkeypatch, resolver_map={"testclient": MAC})
    with TestClient(app, headers=CSRF) as c:
        r = c.post("/api/presence/register-companion", json={"label": "operator"})
        assert r.json()["mac_registered"] is True
    # The link is internal (not part of the route contract), so read it directly.
    linked = store._conn.execute(
        "SELECT device_id FROM identity_devices WHERE address = ?", (MAC,)).fetchone()
    assert linked["device_id"] == ROOT_DEVICE_ID, "root must be linked, not left NULL"
    assert app.state.net_known_provider() == {MAC: "operator"}, \
        "the operator's own box must keep counting"


def test_disabling_multidevice_does_not_resurrect_a_withdrawn_name(tmp_path, monkeypatch):
    """Identity rows OUTLIVE the multidevice flag. With no DeviceStore there is
    nothing to ask for a level, so a companion row cannot be confirmed -- and a
    feature flag must never become a consent override."""
    app, store, peer, auth = _registered_peer(tmp_path, monkeypatch)  # green
    peer.post("/api/consent", json={"level": "red"}, headers=auth)
    assert app.state.net_known_provider() == {}
    monkeypatch.delenv("WAVR_MULTIDEVICE", raising=False)
    app_off = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"),
        identity_store=store)
    assert app_off.state.net_known_provider() == {}, \
        "turning multidevice off must not re-name a device that withdrew"


def test_yellow_corroborates_known_presence_anonymously(tmp_path, monkeypatch):
    """Yellow must appear on the surface that EXPLAINS the house reading -- as an
    unnamed corroborator. Composing that list over the green-only map made a
    yellow-only household read "likely home" with zero corroborators."""
    app, _store, _peer, _auth = _registered_peer(tmp_path, monkeypatch, "yellow")
    central = TestClient(app)
    body = central.get("/api/identity/known-presence", headers=CSRF).json()
    assert len(body["corroborators"]) == 1, "yellow must be listed"
    entry = body["corroborators"][0]
    assert entry["person"] is None, "...and must be listed WITHOUT a name"
    assert entry["mac_prefix"] == "11:22:33"


def test_stepping_down_to_yellow_withdraws_the_details_opt_in(tmp_path, monkeypatch):
    """Consent #2 (details) rides ON TOP of the tri-color, it does not outrank it.
    Opting into details while green then stepping down to yellow asks to be counted
    without a name -- surfacing first/last-seen and device type would be the exact
    opposite of the "minimal data" that step means."""
    app, store, peer, auth = _registered_peer(tmp_path, monkeypatch)  # green
    store.set_details(MAC, True)
    assert store.detailed_net_addresses() == {MAC}
    peer.post("/api/consent", json={"level": "yellow"}, headers=auth)
    assert store.detailed_net_addresses() == set(), \
        "the narrower grant must not survive the wider one being withdrawn"


def test_withdrawn_device_health_does_not_claim_it_is_registered(tmp_path, monkeypatch):
    """A withdrawal screen must never be the last place the withdrawal is believed.
    my_presence_registered answers "is my device contributing presence right now?"
    -- so it has to read the consent-gated view, not a raw row lookup."""
    app, _store, peer, auth = _registered_peer(tmp_path, monkeypatch)  # green
    assert peer.get("/api/companion/health", headers=auth).json()["my_presence_registered"] is True
    peer.post("/api/consent", json={"level": "red"}, headers=auth)
    assert peer.get("/api/companion/health", headers=auth).json()["my_presence_registered"] is False
    # yellow IS contributing (counted, never named) -- True is the honest answer.
    peer.post("/api/consent", json={"level": "yellow"}, headers=auth)
    peer.post("/api/presence/register-companion", json={"label": "housemate"}, headers=auth)
    assert peer.get("/api/companion/health", headers=auth).json()["my_presence_registered"] is True


def test_anonymous_row_is_flagged_for_the_admin_ui(tmp_path, monkeypatch):
    """Every consumer of a row predates ANONYMOUS and assumes person is a non-empty
    string, so an empty one renders as a real name ("* " with nothing after it).
    The flag is explicit rather than left for each caller to infer."""
    app, store, _peer, _auth = _registered_peer(tmp_path, monkeypatch, "yellow")
    row = store.get(MAC)
    assert row["anonymous"] is True
    assert row["person"] == ""
    central = TestClient(app)
    listed = central.get("/api/identity/devices", headers=CSRF).json()
    entry = [d for d in listed["devices"] if d["address"] == MAC][0]
    assert entry["anonymous"] is True


def test_green_row_is_not_flagged_anonymous(tmp_path, monkeypatch):
    _app, store, _peer, _auth = _registered_peer(tmp_path, monkeypatch)
    assert store.get(MAC)["anonymous"] is False


def test_red_device_is_not_named_in_known_presence(tmp_path, monkeypatch):
    """Second read surface: /api/identity/known-presence composes over
    as_net_map() (api_identity.py:116). A withdrawn device must not be listed
    there by name either -- the guarantee is per-LEVEL, not per-route."""
    app, _store, peer, auth = _registered_peer(tmp_path, monkeypatch)  # green
    central = TestClient(app)
    named = [c["person"] for c in
             central.get("/api/identity/known-presence", headers=CSRF).json()["corroborators"]]
    assert named == ["housemate"]
    peer.post("/api/consent", json={"level": "red"}, headers=auth)
    body = central.get("/api/identity/known-presence", headers=CSRF).json()
    assert body["corroborators"] == []
