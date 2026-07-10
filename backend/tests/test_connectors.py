"""Connectors & Services egress surface (project_wavr_connectors_vision).

Covers the ConnectorStore round-trip and the three routes, proving the
non-negotiable guardrails: DEFAULT-OFF, REVOCABLE, monotone (a registry row can
NEVER enable egress beyond the env flag), SINGLE-SURFACE central+CSRF gating, and
byte-identical behaviour with an empty registry.
"""
import pytest
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.storage import Storage
from wavr.connector_store import ConnectorStore

CSRF = {"X-Wavr-Local": "1"}


class _FakeNarrator:
    def narrate(self, state, history):
        return "casa ocupada"


def _client(tmp_path, monkeypatch, store=None, narrator=None):
    # Pin every store to a tmp db so no wavr.db is written in cwd (public-repo PII trap).
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "w.db"))
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    store = store or ConnectorStore(":memory:")
    app = create_app(sources=[], storage=Storage(":memory:"),
                     connector_store=store, narrator=narrator)
    return TestClient(app, headers=CSRF), store


# --------------------------------------------------------------------------- #
# ConnectorStore: persistence round-trip + the two overlay predicates.
# --------------------------------------------------------------------------- #
def test_store_round_trip_and_predicates():
    s = ConnectorStore(":memory:")
    # absent id => neither suppressed nor enabled (empty registry == default-off)
    assert s.is_suppressed("narrator") is False
    assert s.is_enabled("gen-x") is False
    assert s.get("gen-x") is None

    row = s.upsert("gen-x", "generic", "My API", scope="outbound: example.com")
    assert row["enabled"] == 0                       # DEFAULT-OFF on insert
    assert s.is_enabled("gen-x") is False
    assert [r["id"] for r in s.list()] == ["gen-x"]

    assert s.set_enabled("gen-x", True) is True
    assert s.is_enabled("gen-x") is True
    assert s.is_suppressed("gen-x") is False         # enabled=1 is not a kill-switch

    assert s.set_enabled("gen-x", False) is True
    assert s.is_suppressed("gen-x") is True          # enabled=0 row == suppressed
    assert s.set_enabled("missing", True) is False   # unknown id

    assert s.delete("gen-x") is True
    assert s.delete("gen-x") is False
    assert s.is_suppressed("gen-x") is False          # gone => back to default


def test_store_override_and_effective_active():
    # override(): absent => None (env decides); enabled=1 => "on"; enabled=0 => "off".
    s = ConnectorStore(":memory:")
    assert s.override("narrator") is None
    # effective_active with no row is byte-identical to the env flag passed in.
    assert s.effective_active("narrator", True) is True
    assert s.effective_active("narrator", False) is False
    # A deliberate enable WINS over an env flag that is off.
    s.upsert("narrator", "builtin", "LLM Narrator")
    s.set_enabled("narrator", True)
    assert s.override("narrator") == "on"
    assert s.effective_active("narrator", False) is True     # override forces on
    # A deliberate disable WINS over an env flag that is on (kill-switch).
    s.set_enabled("narrator", False)
    assert s.override("narrator") == "off"
    assert s.effective_active("narrator", True) is False     # override forces off


def test_store_upsert_preserves_enabled():
    # An upsert refreshes metadata but must NEVER silently flip the kill-switch bit.
    s = ConnectorStore(":memory:")
    s.upsert("narrator", "builtin", "LLM Narrator")
    s.set_enabled("narrator", False)                  # suppressed
    s.upsert("narrator", "builtin", "LLM Narrator", scope="outbound-cloud: gemini")
    assert s.is_suppressed("narrator") is True        # still suppressed after re-upsert
    assert s.get("narrator")["scope"] == "outbound-cloud: gemini"


def test_store_persists_across_instances(tmp_path):
    p = str(tmp_path / "c.db")
    ConnectorStore(p).upsert("gen-x", "generic", "My API")
    assert ConnectorStore(p).get("gen-x") is not None


# --------------------------------------------------------------------------- #
# GET /api/connectors + /catalog: lists the built-ins with live env-derived state.
# --------------------------------------------------------------------------- #
def test_empty_registry_lists_builtins_all_inactive(tmp_path, monkeypatch):
    c, _s = _client(tmp_path, monkeypatch)
    body = c.get("/api/connectors").json()
    by_id = {x["id"]: x for x in body["connectors"]}
    assert set(by_id) == {"narrator", "ha-import", "ha-control", "mcp-read", "mcp-http"}  # no generics
    # DEFAULT-OFF: nothing is active with a bare env + empty registry.
    assert all(x["active"] is False for x in by_id.values())
    # Empty store => no override anywhere and nothing "enabled but pending" => byte-identical.
    assert all(x["override"] is None for x in by_id.values())
    assert all(x["needs"] is None for x in by_id.values())
    assert by_id["narrator"]["available"] is False          # no provider configured
    assert by_id["narrator"]["enforcement"] == "registry-overlay"
    assert by_id["ha-control"]["enforcement"] == "env"
    # catalog is the built-ins only, decoupled from generics
    cat = c.get("/api/connectors/catalog").json()["catalog"]
    assert {x["id"] for x in cat} == {"narrator", "ha-import", "ha-control", "mcp-read", "mcp-http"}


def test_empty_registry_status_badge_zero_and_byte_identical_narrate(tmp_path, monkeypatch):
    c, s = _client(tmp_path, monkeypatch)
    assert c.get("/api/status").json()["features"]["connectors_active"] == 0
    # Byte-identical: no narrator configured + empty registry => same 503 as before.
    r = c.post("/api/narrate")
    assert r.status_code == 503 and "not configured" in r.json()["detail"]
    assert s.is_suppressed("narrator") is False             # nothing was written


def test_empty_registry_ha_import_byte_identical(tmp_path, monkeypatch):
    # Default WAVR_HA_IMPORT=1 but HA not configured => unchanged 400, no suppression.
    c, s = _client(tmp_path, monkeypatch)
    r = c.post("/api/ha/import", json={"dry_run": True})
    assert r.status_code == 400                             # "not configured"
    assert s.is_suppressed("ha-import") is False


# --------------------------------------------------------------------------- #
# Generic connector: the registry IS the full gate (default-off, revocable).
# --------------------------------------------------------------------------- #
def test_generic_enable_flips_and_revokes(tmp_path, monkeypatch):
    s = ConnectorStore(":memory:")
    s.upsert("gen-x", "generic", "My API", scope="outbound: example.com")
    c, _ = _client(tmp_path, monkeypatch, store=s)
    # default-off
    got = {x["id"]: x for x in c.get("/api/connectors").json()["connectors"]}
    assert got["gen-x"]["active"] is False
    # enable => is_enabled true, badge counts it
    r = c.post("/api/connectors/gen-x/enable", json={"enabled": True})
    assert r.status_code == 200 and r.json()["connector"]["active"] is True
    assert s.is_enabled("gen-x") is True
    assert c.get("/api/status").json()["features"]["connectors_active"] == 1
    # revoke => immediately off
    r = c.post("/api/connectors/gen-x/enable", json={"enabled": False})
    assert r.json()["connector"]["active"] is False
    assert s.is_enabled("gen-x") is False


def test_unknown_connector_404(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    assert c.post("/api/connectors/nope/enable", json={"enabled": True}).status_code == 404


# --------------------------------------------------------------------------- #
# Built-in enforcement='env' (ha-control, mcp-read): the toggle is informational.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cid,flag", [("ha-control", "WAVR_MCP_CONTROL"),
                                      ("mcp-read", "separate MCP server")])
def test_env_enforced_builtin_enable_409(tmp_path, monkeypatch, cid, flag):
    c, s = _client(tmp_path, monkeypatch)
    r = c.post(f"/api/connectors/{cid}/enable", json={"enabled": True})
    assert r.status_code == 409
    # nothing written to the registry -- an env-gate stays the single source of truth
    assert s.get(cid) is None


# --------------------------------------------------------------------------- #
# Built-in registry-overlay kill-switch (narrator): REVOCABLE, immediate, no restart.
# --------------------------------------------------------------------------- #
def test_narrator_kill_switch_revokes_and_restores(tmp_path, monkeypatch):
    c, s = _client(tmp_path, monkeypatch, narrator=_FakeNarrator())
    assert c.post("/api/narrate").status_code == 200          # configured => works
    # revoke via the Connectors screen -> next call is refused, no restart
    r = c.post("/api/connectors/narrator/enable", json={"enabled": False})
    assert r.status_code == 200 and s.is_suppressed("narrator") is True
    r = c.post("/api/narrate")
    assert r.status_code == 503 and "revoked" in r.json()["detail"]
    # restore -> works again immediately
    c.post("/api/connectors/narrator/enable", json={"enabled": True})
    assert s.is_suppressed("narrator") is False
    assert c.post("/api/narrate").status_code == 200


def test_override_enable_without_provider_stays_inert_and_honest(tmp_path, monkeypatch):
    # The override is a REAL enable now, BUT an enabled-yet-unconfigured connector must
    # still egress NOTHING. Enable narrator while WAVR_NARRATE_ENABLED is unset AND no
    # provider is configured: the gate flips on (override "on") but the card stays
    # inactive, honestly reports needs="config", and POST /api/narrate stays 503 with a
    # message telling the admin what is missing (never a silent egress).
    c, s = _client(tmp_path, monkeypatch)          # no injected narrator, env off, no key
    c.post("/api/connectors/narrator/enable", json={"enabled": True})
    got = {x["id"]: x for x in c.get("/api/connectors").json()["connectors"]}
    assert got["narrator"]["override"] == "on"      # deliberate admin enable, persisted
    assert got["narrator"]["active"] is False        # not actually live (no provider)
    assert got["narrator"]["needs"] == "config"      # honest: needs a provider key
    r = c.post("/api/narrate")
    assert r.status_code == 503                       # no egress
    assert "enabled in Connectors" in r.json()["detail"]
    assert "provider" in r.json()["detail"]


def test_override_enable_persists_across_restart(tmp_path, monkeypatch):
    # The admin's enable is stored ON THE BOX and survives a process restart.
    p = str(tmp_path / "conn.db")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "w.db"))
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    app = create_app(sources=[], storage=Storage(":memory:"),
                     connector_store=ConnectorStore(p))
    with TestClient(app, headers=CSRF) as c:
        assert c.post("/api/connectors/narrator/enable",
                      json={"enabled": True}).status_code == 200
    # A fresh store off the same file (a "restart") still sees the override.
    assert ConnectorStore(p).override("narrator") == "on"


def test_narrator_override_activates_after_restart_with_provider(tmp_path, monkeypatch):
    # Restart honesty: an override "on" set BEFORE app start, with a configured provider
    # (local Ollama needs no key), builds the provider client at startup so the feature is
    # actually LIVE -- active True, needs None. This is the "needs a hub restart" path
    # paying off after the restart.
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "w.db"))
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    monkeypatch.setenv("WAVR_NARRATE_PROVIDER", "ollama")   # configured, no key needed
    s = ConnectorStore(":memory:")
    s.upsert("narrator", "builtin", "LLM Narrator")
    s.set_enabled("narrator", True)                          # override "on" pre-restart
    c, _ = _client(tmp_path, monkeypatch, store=s)           # WAVR_NARRATE_ENABLED unset
    got = {x["id"]: x for x in c.get("/api/connectors").json()["connectors"]}
    assert got["narrator"]["override"] == "on"
    assert got["narrator"]["active"] is True                 # provider client built at startup
    assert got["narrator"]["needs"] is None
    assert got["narrator"]["scope"] == "local, zero egress"  # ollama == on-box


def test_ha_import_override_enables_gate_when_env_off(tmp_path, monkeypatch):
    # WAVR_HA_IMPORT=0 (env off) + no override => the chokepoint 403s "disabled". Enabling
    # the override flips the GATE on: the same call now passes the gate and only fails on
    # the missing HA creds (400 "not configured") -- proof the override enabled beyond env,
    # while still egressing nothing until HA is actually configured.
    monkeypatch.setenv("WAVR_HA_IMPORT", "0")
    c, s = _client(tmp_path, monkeypatch)
    r = c.post("/api/ha/import", json={"dry_run": True})
    assert r.status_code == 403 and "disabled" in r.json()["detail"]
    c.post("/api/connectors/ha-import/enable", json={"enabled": True})
    got = {x["id"]: x for x in c.get("/api/connectors").json()["connectors"]}
    assert got["ha-import"]["override"] == "on"
    assert got["ha-import"]["needs"] == "config"     # HA creds still missing => not live
    r = c.post("/api/ha/import", json={"dry_run": True})
    assert r.status_code == 400 and "not configured" in r.json()["detail"]


def test_ha_import_override_off_revokes_when_env_on(tmp_path, monkeypatch):
    # Kill-switch still works: default WAVR_HA_IMPORT=1 (env on), an override "off" revokes
    # it immediately, no restart.
    c, s = _client(tmp_path, monkeypatch)
    c.post("/api/connectors/ha-import/enable", json={"enabled": False})
    assert s.override("ha-import") == "off"
    r = c.post("/api/ha/import", json={"dry_run": True})
    assert r.status_code == 403 and "revoked" in r.json()["detail"]


def test_mcp_http_override_enable_needs_restart_when_unavailable(tmp_path, monkeypatch):
    # Without multidevice the in-app MCP-HTTP mount is not wired (available False).
    # Enabling it persists the override but the card is HONEST: active False, needs
    # "restart" -- nothing is exposed until the hub restarts with multidevice on.
    c, s = _client(tmp_path, monkeypatch)
    r = c.post("/api/connectors/mcp-http/enable", json={"enabled": True})
    assert r.status_code == 200
    d = r.json()["connector"]
    assert d["override"] == "on" and d["available"] is False
    assert d["active"] is False and d["needs"] == "restart"


# --------------------------------------------------------------------------- #
# XSS-safe: a hostile label is stored+returned VERBATIM as data (frontend uses
# textContent, never innerHTML) and config_json carries no secret.
# --------------------------------------------------------------------------- #
def test_hostile_label_stored_verbatim_as_data(tmp_path, monkeypatch):
    s = ConnectorStore(":memory:")
    evil = '<img src=x onerror=alert(1)>'
    s.upsert("gen-x", "generic", evil, scope="outbound: example.com",
             config_json='{"env_ref": "WAVR_EXAMPLE_KEY"}')
    c, _ = _client(tmp_path, monkeypatch, store=s)
    got = {x["id"]: x for x in c.get("/api/connectors").json()["connectors"]}["gen-x"]
    assert got["label"] == evil                      # verbatim data, not interpreted
    # config_json holds an env NAME reference, never a secret value
    assert "WAVR_EXAMPLE_KEY" in s.get("gen-x")["config_json"]


# --------------------------------------------------------------------------- #
# SINGLE-SURFACE gating: central+CSRF, same as /api/cameras + identity.
# --------------------------------------------------------------------------- #
def test_post_requires_csrf_header(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "w.db"))
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    s = ConnectorStore(":memory:")
    s.upsert("gen-x", "generic", "My API")
    app = create_app(sources=[], storage=Storage(":memory:"), connector_store=s)
    with TestClient(app) as c:                       # no X-Wavr-Local header
        assert c.post("/api/connectors/gen-x/enable", json={"enabled": True}).status_code == 403


def test_multidevice_user_is_forbidden(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "md.db"))
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    s = ConnectorStore(":memory:")
    s.upsert("gen-x", "generic", "My API")
    app = create_app(sources=[], storage=Storage(":memory:"), connector_store=s)
    # loopback root mints a 'user' pairing code; a forged in-subnet peer redeems it
    central = TestClient(app)
    code = central.post("/api/pair-code", json={"role": "user"}, headers=CSRF).json()["code"]
    peer = TestClient(app, client=("192.168.1.50", 12345))
    auth = {"Authorization": f"Bearer {peer.post('/api/pair', json={'code': code, 'device_name': 'phone'}).json()['token']}"}
    # a 'user' is refused on BOTH the read list and the enable toggle (central-gated)
    assert peer.get("/api/connectors", headers=auth).status_code == 403
    assert peer.post("/api/connectors/gen-x/enable", json={"enabled": True},
                     headers=auth).status_code == 403
    # loopback root passes
    assert central.get("/api/connectors", headers=CSRF).status_code == 200
