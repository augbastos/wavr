"""One phone, one live key to the home.

The first user paired the same phone four times over an afternoon -- a
reinstall here, a fresh code there -- and the Core finished the day holding
four live credentials:

    my phone    central   seen 16:31
    Phone       user      seen 16:32
    phone       central   seen 18:44
    phone       user      seen 20:53

One phone. Four rows, because the typed name drifted every time. Three of them
forgotten, and every one of them still a working key to that home. Re-pairing is not a rare event -- it happens on every
reinstall -- so this accumulates quietly for as long as somebody owns the
product.

The fix is an opaque, client-hashed `device_key`: the phone says "same phone as
last time", and the Core retires what that phone held before. These tests
pin the parts that make it true and, just as importantly, the parts that must
keep working for everything that sends no key at all -- a browser, an older
app, a script. Nothing may be revoked on a guess.

Two separate flows mint a companion credential (typed code, and approve-on-the
-Core). Both are covered here: fixing one would have left the duplicate the
user actually saw half-open, which is precisely the shape of defect that has
cost the most this week -- the checking half written, the producing half never
called.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wavr.api_devices import build_pair_router
from wavr.api_pair_requests import build_pair_request_router
from wavr.devices import DeviceStore
from wavr.pair_requests import PairApprovalManager
from wavr.pairing import PairingManager

CHAVE = "a" * 64          # what a SHA-256 digest from the phone looks like
OUTRA = "b" * 64


def _store(tmp_path) -> DeviceStore:
    return DeviceStore(str(tmp_path / "devices.db"))


def _vivos(store: DeviceStore) -> list:
    return [d for d in store.list() if not d.revoked]


# --------------------------------------------------------------------------- #
# The store: matching and retiring.
# --------------------------------------------------------------------------- #
def test_pairing_the_same_phone_again_kills_the_old_key(tmp_path):
    """The whole point: the credential from the earlier pairing stops working."""
    store = _store(tmp_path)
    pairing = PairingManager(store)

    _velho_id, velho_token = pairing.redeem(pairing.mint_code("user"), "my phone", device_key=CHAVE)
    assert store.verify(velho_token) is not None            # it worked, before

    _novo_id, novo_token = pairing.redeem(pairing.mint_code("user"), "my phone", device_key=CHAVE)

    assert store.verify(velho_token) is None, "the phone's previous key still opens the home"
    assert store.verify(novo_token) is not None, "the key the phone just received must work"


def test_the_old_row_is_retired_not_erased(tmp_path):
    """A pairing that happened is a fact. Revoked, never deleted: an audit trail
    that erases itself whenever a device re-pairs is not an audit trail."""
    store = _store(tmp_path)
    pairing = PairingManager(store)

    velho_id, _ = pairing.redeem(pairing.mint_code("user"), "my phone", device_key=CHAVE)
    pairing.redeem(pairing.mint_code("user"), "my phone", device_key=CHAVE)

    velho = store.get(velho_id)
    assert velho is not None, "the record of the earlier pairing was deleted"
    assert velho.revoked is True


def test_a_phone_that_says_nothing_keeps_what_it_had(tmp_path):
    """Every caller that predates this -- a browser, an older app, a script --
    must pair exactly as it always did. No key, no matching, no revocation."""
    store = _store(tmp_path)
    pairing = PairingManager(store)

    _a_id, token_a = pairing.redeem(pairing.mint_code("user"), "laptop")
    _b_id, token_b = pairing.redeem(pairing.mint_code("user"), "laptop")

    assert store.verify(token_a) is not None, "a credential was revoked on a guess"
    assert store.verify(token_b) is not None


def test_a_different_phone_is_left_alone(tmp_path):
    """Two people, two phones, two keys. Matching must be on the key, not on
    the name (both read `my phone`) and not on "the newest wins"."""
    store = _store(tmp_path)
    pairing = PairingManager(store)

    _meu_id, meu = pairing.redeem(pairing.mint_code("user"), "my phone", device_key=CHAVE)
    _seu_id, seu = pairing.redeem(pairing.mint_code("user"), "my phone", device_key=OUTRA)

    assert store.verify(meu) is not None, "somebody else's phone revoked this one"
    assert store.verify(seu) is not None


def test_a_key_less_row_is_stored_as_nothing_at_all(tmp_path):
    """Not an empty string: a column full of `''` would make every key-less
    device look like the same device to any future query that forgets to
    special-case it."""
    store = _store(tmp_path)
    store.add("laptop", "user")
    store.close()

    con = sqlite3.connect(str(tmp_path / "devices.db"))
    try:
        (valor,), = con.execute("SELECT device_key FROM devices").fetchall()
    finally:
        con.close()
    assert valor is None


def test_supersede_reports_what_it_retired(tmp_path):
    """It returns the ids so a caller can SAY what happened. A credential
    vanishing from somebody's device list is exactly the kind of thing that
    should be reported rather than inferred."""
    store = _store(tmp_path)
    um, _ = store.add("my phone", "user", device_key=CHAVE)
    dois, _ = store.add("my phone", "central", device_key=CHAVE)
    fica, _ = store.add("my phone", "user", device_key=CHAVE)
    outro, _ = store.add("tablet", "user", device_key=OUTRA)

    retirados = store.supersede_same_device(CHAVE, fica)

    assert set(retirados) == {um, dois}
    assert store.get(fica).revoked is False
    assert store.get(outro).revoked is False
    assert store.supersede_same_device(CHAVE, fica) == [], "re-running it must be a no-op"


def test_supersede_ignores_an_empty_key(tmp_path):
    """Called with nothing, it must touch nothing -- never fall through to
    "every row whose key happens to be empty".

    The row is planted with an empty key on purpose. `add()` stores None, so a
    version of this test that only used `add()` would pass with the guard
    deleted -- nothing would match either way -- and prove nothing. The guard
    exists for the day some other writer puts an empty string in that column."""
    store = _store(tmp_path)
    a, _ = store.add("laptop", "user")
    b, _ = store.add("desktop", "user")
    caminho = str(tmp_path / "devices.db")
    store.close()
    con = sqlite3.connect(caminho)
    try:
        con.execute("UPDATE devices SET device_key = '' WHERE device_id = ?", (a,))
        con.commit()
    finally:
        con.close()

    store = DeviceStore(caminho)
    assert store.supersede_same_device("", b) == []
    assert store.get(a).revoked is False, "an empty key matched a row and revoked it"


# --------------------------------------------------------------------------- #
# The two mint sites, end to end.
# --------------------------------------------------------------------------- #
def _cliente_codigo(tmp_path):
    store = _store(tmp_path)
    pairing = PairingManager(store)
    app = FastAPI()
    app.include_router(build_pair_router(store, pairing))
    return TestClient(app), store, pairing


def test_the_typed_code_endpoint_carries_the_key(tmp_path):
    """`POST /api/pair` -- the flow behind a typed 8-digit code."""
    client, store, pairing = _cliente_codigo(tmp_path)

    primeiro = client.post("/api/pair", json={
        "code": pairing.mint_code("user"), "device_name": "my phone", "device_key": CHAVE})
    assert primeiro.status_code == 200
    segundo = client.post("/api/pair", json={
        "code": pairing.mint_code("user"), "device_name": "my phone", "device_key": CHAVE})
    assert segundo.status_code == 200

    assert store.verify(primeiro.json()["token"]) is None
    assert store.verify(segundo.json()["token"]) is not None
    assert len(_vivos(store)) == 1


def test_the_typed_code_endpoint_still_works_without_a_key(tmp_path):
    """The field is optional on the wire. A client that omits it must not get
    a 422 -- that would break every phone already installed."""
    client, store, pairing = _cliente_codigo(tmp_path)
    r = client.post("/api/pair", json={
        "code": pairing.mint_code("user"), "device_name": "laptop"})
    assert r.status_code == 200, r.text
    assert store.verify(r.json()["token"]) is not None


def test_an_explicit_null_key_is_the_same_as_no_key(tmp_path):
    """Some clients serialize their optionals instead of dropping them, so the
    body arrives as `"device_key": null`. That has to pair, not 422."""
    client, store, pairing = _cliente_codigo(tmp_path)
    r = client.post("/api/pair", json={
        "code": pairing.mint_code("user"), "device_name": "laptop",
        "device_key": None})
    assert r.status_code == 200, r.text
    assert store.verify(r.json()["token"]) is not None


def _cliente_aprovacao(tmp_path):
    store = _store(tmp_path)
    approvals = PairApprovalManager(store)
    app = FastAPI()
    app.include_router(build_pair_request_router(approvals, lambda: "AA:BB"))
    return TestClient(app), store, approvals


def test_the_approval_flow_carries_the_key_too(tmp_path):
    """`POST /api/pair-request` + the operator approving on the Core is a
    SEPARATE mint site. Threading the key through only one of the two would
    leave the duplicate half-fixed."""
    client, store, approvals = _cliente_aprovacao(tmp_path)

    def parear() -> str:
        r = client.post("/api/pair-request", json={
            "requester_name": "my phone", "platform": "Android", "device_key": CHAVE})
        assert r.status_code == 200, r.text
        corpo = r.json()
        device_id = approvals.approve(corpo["request_id"], "user", corpo["compare_code"])
        assert device_id is not None
        return device_id

    velho = parear()
    novo = parear()

    assert store.get(velho).revoked is True
    assert store.get(novo).revoked is False
    assert len(_vivos(store)) == 1


def test_the_approval_flow_still_works_without_a_key(tmp_path):
    client, store, approvals = _cliente_aprovacao(tmp_path)
    r = client.post("/api/pair-request", json={"requester_name": "laptop"})
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert approvals.approve(corpo["request_id"], "user", corpo["compare_code"]) is not None
    assert len(_vivos(store)) == 1


def test_the_approval_screen_never_shows_the_key(tmp_path):
    """The operator's approve prompt has no use for it, and a value whose only
    job is to identify a phone should not travel anywhere it is not read."""
    _client, _store_, approvals = _cliente_aprovacao(tmp_path)
    approvals.create("my phone", platform="Android", device_key=CHAVE)
    pendentes = approvals.list_pending()
    assert pendentes
    for r in pendentes:
        assert CHAVE not in str(r)


def test_four_pairings_of_one_phone_leave_one_live_key(tmp_path):
    """The afternoon he actually had, replayed: same phone, different names,
    different roles, four times."""
    store = _store(tmp_path)
    pairing = PairingManager(store)
    for nome, papel in (("Alex", "central"), ("Phone", "user"),
                        ("my phone", "central"), ("my phone", "user")):
        pairing.redeem(pairing.mint_code(papel), nome, device_key=CHAVE)

    vivos = _vivos(store)
    assert len(vivos) == 1, [d.name for d in vivos]
    assert vivos[0].name == "my phone" and vivos[0].role == "user"
    assert len(store.list()) == 4, "the earlier pairings must still be on record"


# --------------------------------------------------------------------------- #
# A credential only retires credentials of its own kind.
#
# The first version of this feature matched on the device key alone. A guest
# invite is redeemed through the very same POST /api/pair, and the phone
# attaches its key without knowing which kind of code it is holding -- so
# handing a four-hour visitor code to the phone that runs the house retired the
# house credential, and four hours later that phone held nothing at all. The
# least-trusted ceremony in the product had gained power over the most
# privileged credential.
# --------------------------------------------------------------------------- #
def test_a_visitor_code_cannot_retire_the_house_credential(tmp_path):
    store = _store(tmp_path)
    pairing = PairingManager(store)

    dono_id, dono = pairing.redeem(pairing.mint_code("central"), "my phone", device_key=CHAVE)
    _hosp_id, hospede = pairing.redeem(pairing.mint_guest_code(4), "my phone", device_key=CHAVE)

    assert store.verify(dono) is not None, \
        "a four-hour visitor code retired the credential that runs the house"
    assert store.verify(hospede) is not None
    assert store.get(dono_id).revoked is False


def test_a_permanent_pairing_does_not_retire_a_live_visitor(tmp_path):
    """The rule reads both ways, and it must: a visit in progress is not
    somebody else's to end by re-pairing their own phone."""
    store = _store(tmp_path)
    pairing = PairingManager(store)

    hosp_id, hospede = pairing.redeem(pairing.mint_guest_code(4), "my phone", device_key=CHAVE)
    pairing.redeem(pairing.mint_code("user"), "my phone", device_key=CHAVE)

    assert store.verify(hospede) is not None
    assert store.get(hosp_id).revoked is False


def test_two_visits_from_one_phone_still_leave_one(tmp_path):
    """Narrowing the rule must not switch the feature off for guests."""
    store = _store(tmp_path)
    pairing = PairingManager(store)

    velho_id, velho = pairing.redeem(pairing.mint_guest_code(4), "my phone", device_key=CHAVE)
    _novo_id, novo = pairing.redeem(pairing.mint_guest_code(4), "my phone", device_key=CHAVE)

    assert store.verify(velho) is None, "the earlier visit is still a live key"
    assert store.verify(novo) is not None
    assert store.get(velho_id).revoked is True


# --------------------------------------------------------------------------- #
# Retiring a credential cleans up after itself, and says so.
# --------------------------------------------------------------------------- #
def test_retiring_runs_the_same_cleanup_that_revoking_runs(tmp_path):
    """`DELETE /api/devices/{id}` forgets the device's experience grants. A
    credential retired by a re-pair used to skip that and keep showing up as
    holding access to an experience: gone from one screen, present on another."""
    store = _store(tmp_path)
    esquecidos: list[str] = []
    store.set_revoke_hook(esquecidos.append)
    pairing = PairingManager(store)

    velho_id, _ = pairing.redeem(pairing.mint_code("user"), "my phone", device_key=CHAVE)
    pairing.redeem(pairing.mint_code("user"), "my phone", device_key=CHAVE)

    assert esquecidos == [velho_id]


def test_a_cleanup_that_fails_does_not_fail_the_pairing(tmp_path):
    """And the retirement is still reported.

    Both callers wrap this in their own try/except, so "the pairing survives"
    is true with or without the store's own guard -- an earlier version of this
    test asserted only that and could not tell the two apart. The difference
    that matters is the report: with the guard the ids come back and the caller
    can say which credential was retired; without it the exception escapes and
    a revocation that DID happen goes unrecorded."""
    store = _store(tmp_path)

    def explode(_device_id):
        raise RuntimeError("the grants store is busy")

    store.set_revoke_hook(explode)
    pairing = PairingManager(store)

    velho_id, _ = pairing.redeem(pairing.mint_code("user"), "my phone", device_key=CHAVE)
    resultado = pairing.redeem(pairing.mint_code("user"), "my phone", device_key=CHAVE)

    assert resultado is not None
    assert store.verify(resultado[1]) is not None
    assert len(_vivos(store)) == 1
    # The store itself must survive the hook and still report what it did.
    outro, _ = store.add("tablet", "user", device_key=OUTRA)
    mais, _ = store.add("tablet", "user", device_key=OUTRA)
    assert store.supersede_same_device(OUTRA, mais) == [outro], \
        "a failing cleanup hook swallowed the report of what was retired"
    assert store.get(velho_id).revoked is True


def test_the_retirement_is_reported(tmp_path, caplog):
    """The store returns the ids so a caller can SAY what happened. Both
    callers used to throw the list away -- a stated guarantee with no
    producer, written inside the fix for exactly that."""
    import logging

    store = _store(tmp_path)
    pairing = PairingManager(store)
    velho_id, _ = pairing.redeem(pairing.mint_code("user"), "my phone", device_key=CHAVE)
    with caplog.at_level(logging.INFO):
        pairing.redeem(pairing.mint_code("user"), "my phone", device_key=CHAVE)

    assert velho_id in caplog.text, "nothing said which credential was retired"


def test_nothing_is_said_when_nothing_was_retired(tmp_path, caplog):
    """An empty retirement is worth nothing in the log."""
    import logging

    store = _store(tmp_path)
    pairing = PairingManager(store)
    with caplog.at_level(logging.INFO):
        pairing.redeem(pairing.mint_code("user"), "my phone", device_key=CHAVE)

    assert "retired" not in caplog.text


def test_a_store_that_cannot_retire_says_so(tmp_path, caplog):
    """The quietest outcome must not be the one that silently restores the
    original bug. The failure path at least logged a warning; the
    `if callable(...)` guard next to it logged nothing at all."""
    import logging

    class _LojaAntiga:
        def add(self, name, role, **kw):
            return ("dev-1", "token-1")

    pairing = PairingManager(_LojaAntiga())
    with caplog.at_level(logging.WARNING):
        assert pairing.redeem(pairing.mint_code("user"), "my phone",
                              device_key=CHAVE) == ("dev-1", "token-1")

    assert "duplicates will accumulate" in caplog.text


# --------------------------------------------------------------------------- #
# Failing to tidy up must never cost somebody their home.
# --------------------------------------------------------------------------- #
class _LojaQueNaoAposenta:
    """A store that mints fine and blows up on the retirement step."""

    def __init__(self, real: DeviceStore):
        self._real = real
        self.tentou = False

    def add(self, *a, **kw):
        return self._real.add(*a, **kw)

    def supersede_same_device(self, *_a, **_kw):
        self.tentou = True
        raise sqlite3.OperationalError("database is locked")


def test_a_failure_to_retire_still_hands_over_a_working_key(tmp_path):
    """Revoking first and then failing to mint would lock somebody out of their
    own home. So the order is mint, then retire -- and a failure to retire is a
    stale row, which is untidy, not a lockout."""
    real = _store(tmp_path)
    loja = _LojaQueNaoAposenta(real)
    pairing = PairingManager(loja)

    resultado = pairing.redeem(pairing.mint_code("user"), "my phone", device_key=CHAVE)

    assert loja.tentou, "it never even tried to retire the old credentials"
    assert resultado is not None, "the pairing failed over a tidiness problem"
    assert real.verify(resultado[1]) is not None


def test_a_store_that_never_heard_of_this_still_pairs(tmp_path):
    """Older stores and test doubles have no `supersede_same_device` and an
    `add()` that predates the kwarg. Neither may raise."""

    class _LojaAntiga:
        def __init__(self):
            self.chamadas = []

        def add(self, name, role, expires_at=None):
            self.chamadas.append((name, role))
            return ("dev-1", "token-1")

    loja = _LojaAntiga()
    pairing = PairingManager(loja)
    assert pairing.redeem(pairing.mint_code("user"), "my phone") == ("dev-1", "token-1")
    assert loja.chamadas == [("my phone", "user")]


# --------------------------------------------------------------------------- #
# The producing half: does the phone actually SEND one?
#
# Every test above would pass with a Core that is perfectly ready for a key no
# client ever sends. That is the defect shape that has cost the most this week,
# so it gets its own check, across the language boundary.
# --------------------------------------------------------------------------- #
RAIZ = Path(__file__).resolve().parents[2]
CONEXAO = RAIZ / "frontend" / "js" / "core-connection.js"


from tests.mobile_tree import mobile_dir as _mobile   # onde o app do celular esta


_BASE = _mobile()
_SHIM = _BASE / "src" / "wavr-mobile-shim.js"
_PLUGIN = _BASE / "plugins" / "wavr-net" / "WavrNetPlugin.kt"

# No skip: `mobile/` is part of this repository. If it is missing the checkout
# is broken, and `mobile_dir()` says so instead of letting the run look green.
def _precisa_do_mobile(f):
    return f


def _sem_comentarios(js: str) -> str:
    """Strip comments first. A check that finds what it is looking for inside
    the comment warning about it has caught me three times this week."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", linha) for linha in js.splitlines())


@_precisa_do_mobile
def test_the_phone_asks_native_for_its_key():
    kotlin = _PLUGIN.read_text(encoding="utf-8", errors="replace")
    assert re.search(r"fun\s+deviceKey\s*\(", kotlin), \
        "the native plugin has no deviceKey() for the shim to call"
    # Not the bare word: the constant holding the known-broken value is called
    # ANDROID_ID_QUEBRADO, and matching on that would pass with the real lookup
    # deleted. Ask for the lookup itself.
    assert "Settings.Secure.ANDROID_ID" in kotlin, \
        "deviceKey() must survive a reinstall -- that is the case it exists for"

    shim = _sem_comentarios(_SHIM.read_text(encoding="utf-8", errors="replace"))
    assert "WavrNet.deviceKey" in shim, "the shim never calls deviceKey()"


def test_the_typed_code_form_sends_the_key_too():
    """The third producer, and the one that matters most.

    The shim offers "Enter an 8-digit code instead" from three screens, and
    every one of those offers appears right after something else went wrong:
    the approval was denied, the approval timed out, discovery found nothing.
    So the path a person reaches by re-trying was the one still leaving another
    live key behind. It is not in the shim -- it is the dashboard's own pairing
    form -- which is why a check that read only the shim passed.

    This file always exists, so this test never skips."""
    fonte = _sem_comentarios(CONEXAO.read_text(encoding="utf-8", errors="replace"))
    pedacos = fonte.split('"/api/pair"')
    assert len(pedacos) >= 2, "the dashboard no longer posts to /api/pair"
    vizinhanca = pedacos[0][-900:]
    # Not the words. `if(false) corpo.device_key = chave` keeps every word and
    # sends nothing; so does `chave = null`. Both of those were run against an
    # earlier version of this test and both passed it.
    atribui = re.search(r"if\s*\(\s*(\w+)\s*\)\s*\w+\.device_key\s*=\s*\1\s*;",
                        vizinhanca)
    assert atribui, "the typed-code body never sets device_key from the phone's key"
    nome = atribui.group(1)
    assert re.search(nome + r"\s*=\s*await\s+window\.WAVR_MOBILE\.deviceKey\s*\(",
                     vizinhanca), \
        "the value it sends does not come from asking the phone"


@_precisa_do_mobile
def test_the_shim_publishes_the_key_to_the_dashboard():
    """The dashboard cannot reach a native plugin; the shim has to hand it
    over. Without this the test above can only ever fail."""
    shim = _sem_comentarios(_SHIM.read_text(encoding="utf-8", errors="replace"))
    assert re.search(r"deviceKey\s*:\s*chaveDoAparelho", shim), \
        "WAVR_MOBILE does not publish deviceKey, so the dashboard cannot ask"


@_precisa_do_mobile
def test_both_pairing_calls_send_the_key():
    """Both, not one. The app has two ways to pair and the user hit both.

    Read around each POST rather than counting occurrences: a file that
    mentions `device_key` twice in one flow and never in the other would
    satisfy a count and still ship the bug."""
    shim = _sem_comentarios(_SHIM.read_text(encoding="utf-8", errors="replace"))
    # Not "the words device_key appear near the call" -- that survived a
    # mutation to `if(false)`. The field must be assigned FROM the value the
    # native side handed over, inside the block that builds this body.
    atribui = re.compile(r"if\s*\(\s*(\w+)\s*\)\s*\w+\.device_key\s*=\s*\1\s*;")
    for rota in ('"/api/pair"', '"/api/pair-request"'):
        pedacos = shim.split(rota)
        assert len(pedacos) == 2, f"expected exactly one POST to {rota}"
        # The body is built right around the call: the object is assembled just
        # above it and the fetch options just below.
        vizinhanca = pedacos[0][-600:] + pedacos[1][:600]
        assert atribui.search(vizinhanca), \
            f"the body posted to {rota} never sets device_key from the phone's key"
