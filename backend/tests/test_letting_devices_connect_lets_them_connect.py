"""The switch has to mean what its own label says.

"Let other devices connect" turned on multidevice mode and local TLS, and left
the socket on the loopback interface, because which address to listen on was a
SECOND setting called "Listen on" whose two choices were `127.0.0.1` and
`0.0.0.0`.

Measured on the frozen Core that ships in the Windows installer, with the switch
flipped from the settings screen exactly as an operator would and the Core
restarted:

    the setup screen advertised : https://192.168.1.226:64778
    connecting to that address  : ConnectionRefusedError
    netstat                     : TCP 127.0.0.1:64778 ... LISTENING

So the pairing QR carried an address nothing was listening on. A phone scans a
QR without a human reading it first: it dials, nobody answers, and the Core
never learns anything happened, because nothing reached it. Both ends silent,
and the only way through was knowing that a second setting existed, that it was
called "Listen on", and that `0.0.0.0` is how you say "everyone".

Two rules, and they are separate:

  1. Turning the switch on binds every interface, unless somebody states an
     address on purpose. `WAVR_BIND` still wins, from an environment or from
     the (now advanced) setting, because that is a deliberate choice.
  2. An address is only advertised when the socket is actually on the network.
     Rule 1 makes that the normal case; rule 2 is what stops the QR lying in
     the case rule 1 does not cover -- a deliberate loopback bind, or a
     launcher that states its own address on the command line where the config
     never sees it (`backend/Dockerfile` does exactly that).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from wavr import lan_reachability
from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.config import _bind_host, load_config
from wavr.sources.simulated import SimulatedSource
from wavr.storage import Storage

LOCAL = {"X-Wavr-Local": "1"}
LAN_IP = "192.168.1.1"          # fixed, never a real household address


@pytest.fixture(autouse=True)
def _forget_the_launcher():
    """Each case states its own bind, or states that nothing did."""
    lan_reachability.note_bound_host("")
    yield
    lan_reachability.note_bound_host("")


# -- Rule 1: the switch binds the network ------------------------------------

@pytest.mark.parametrize("multidevice,stated,expected", [
    (None, None, "127.0.0.1"),
    ("1", None, "0.0.0.0"),
    ("1", "127.0.0.1", "127.0.0.1"),
    (None, "0.0.0.0", "0.0.0.0"),
])
def test_the_switch_decides_the_interface(monkeypatch, multidevice, stated,
                                          expected):
    """Off stays loopback. On opens the network. A stated address always wins.

    The third row is the one worth keeping: somebody who deliberately wants the
    encrypted connection WITHOUT the LAN must still be able to have it, and
    saying so must not be overridden by a default trying to be helpful.
    """
    monkeypatch.delenv("WAVR_MULTIDEVICE", raising=False)
    monkeypatch.delenv("WAVR_BIND", raising=False)
    if multidevice:
        monkeypatch.setenv("WAVR_MULTIDEVICE", multidevice)
    if stated:
        monkeypatch.setenv("WAVR_BIND", stated)
    assert _bind_host() == expected


def test_the_config_carries_it(monkeypatch, tmp_path):
    """Not just the helper: the value `serve.py` actually hands uvicorn."""
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.delenv("WAVR_BIND", raising=False)
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "b.db"))
    assert load_config().bind_host == "0.0.0.0", (
        "the switch is on and the Core would still listen only to itself")


# -- Rule 2: only advertise an address that answers --------------------------

def _core(tmp_path, monkeypatch, *, multidevice, bind=None):
    monkeypatch.delenv("WAVR_BIND", raising=False)
    monkeypatch.delenv("WAVR_MULTIDEVICE", raising=False)
    if multidevice:
        monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    if bind:
        monkeypatch.setenv("WAVR_BIND", bind)
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "adv.db"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: LAN_IP)
    return create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))


def test_a_lan_bound_core_advertises_the_lan(tmp_path, monkeypatch):
    app = _core(tmp_path, monkeypatch, multidevice=True)
    lan_reachability.note_bound_host("0.0.0.0")
    with TestClient(app, base_url="https://testserver") as c:
        status = c.get("/api/setup/status", headers=LOCAL).json()
        assert status["lan_url"] == f"https://{LAN_IP}:8000", status["lan_url"]
        code = c.post("/api/pair-code", headers=LOCAL,
                      json={"role": "user"}).json()
        assert code["lan_url"] == f"https://{LAN_IP}:8000", code["lan_url"]


def test_a_loopback_bound_core_does_not_advertise_the_lan(tmp_path, monkeypatch):
    """The measured failure, as a test.

    Multidevice on, TLS on, and the operator has deliberately kept the socket
    on this machine. The LAN address is not reachable and must not be offered
    -- least of all inside a QR code, which is acted on by a phone with nobody
    reading it.
    """
    app = _core(tmp_path, monkeypatch, multidevice=True, bind="127.0.0.1")
    lan_reachability.note_bound_host("127.0.0.1")
    with TestClient(app, base_url="https://testserver") as c:
        status = c.get("/api/setup/status", headers=LOCAL).json()
        assert LAN_IP not in status["lan_url"], (
            f"the setup screen offers an address nothing is listening on: "
            f"{status['lan_url']}")
        code = c.post("/api/pair-code", headers=LOCAL,
                      json={"role": "user"}).json()
        assert LAN_IP not in code["lan_url"], (
            f"the pairing QR carries an address nothing is listening on: "
            f"{code['lan_url']}")
        guest = c.post("/api/guest/invite", headers=LOCAL,
                       json={"hours": 4}).json()
        assert LAN_IP not in guest["lan_url"], guest["lan_url"]


def test_the_launcher_outranks_the_configuration(tmp_path, monkeypatch):
    """`backend/Dockerfile` states its host on the command line, where
    `load_config()` cannot see it. Whoever opened the socket is right."""
    app = _core(tmp_path, monkeypatch, multidevice=True)   # config says 0.0.0.0
    lan_reachability.note_bound_host("127.0.0.1")          # the socket says no
    with TestClient(app, base_url="https://testserver") as c:
        status = c.get("/api/setup/status", headers=LOCAL).json()
        assert LAN_IP not in status["lan_url"], (
            "the configuration was believed over the launcher that actually "
            f"opened the socket: {status['lan_url']}")


def test_serves_the_lan_reads_the_launcher_first():
    """The unit underneath both, including the case nobody reported in."""
    lan_reachability.note_bound_host("")
    assert lan_reachability.serves_the_lan("0.0.0.0") is True
    assert lan_reachability.serves_the_lan("127.0.0.1") is False
    assert lan_reachability.serves_the_lan("") is False

    lan_reachability.note_bound_host("0.0.0.0")
    assert lan_reachability.serves_the_lan("127.0.0.1") is True
    lan_reachability.note_bound_host("127.0.0.1")
    assert lan_reachability.serves_the_lan("0.0.0.0") is False


# -- The firewall, which is the other half of "can the phone get through" ----

def test_the_firewall_check_never_raises_and_never_over_claims():
    """It runs a subprocess on somebody's machine during setup. The one thing
    it may never do is take the Core down with it, and the second is call an
    unknown state fine."""
    r = lan_reachability.check(program="wavr-core.exe", port=8000, force=True)
    assert r.state in (lan_reachability.BLOCKED, lan_reachability.ALLOWED,
                       lan_reachability.UNKNOWN)
    assert r.reason, "a verdict with no reason cannot be acted on"
    assert isinstance(r.to_dict()["rules"], list)


def test_no_matching_rule_is_unknown_and_never_allowed(monkeypatch):
    """The reassurance this must refuse.

    No rule mentioning Wavr does not mean the way is clear: Windows prompts on
    the next bind, and where a policy suppresses the prompt the default inbound
    action refuses. Reporting ALLOWED there would tell somebody their phone can
    connect at the exact moment it cannot.
    """
    monkeypatch.setattr(lan_reachability, "_is_windows", lambda: True)
    monkeypatch.setattr(
        lan_reachability, "_netsh_rules",
        lambda: "Rule Name: Something Else\n"
                "----------------------------------\n"
                "Enabled: Yes\nDirection: In\nAction: Allow\n"
                "Program: C:\\other\\thing.exe\nLocalPort: 445\n")
    r = lan_reachability.check(program="wavr-core.exe", port=8000, force=True)
    assert r.state == lan_reachability.UNKNOWN, r
    assert r.checked is True


def test_a_block_rule_is_reported_as_blocked(monkeypatch):
    """The "clicked Cancel on the Windows dialog once" state, which nothing
    else in the product can see: the packets never arrive to be counted."""
    monkeypatch.setattr(lan_reachability, "_is_windows", lambda: True)
    monkeypatch.setattr(
        lan_reachability, "_netsh_rules",
        lambda: "Rule Name: wavr-core.exe\n"
                "----------------------------------\n"
                "Enabled: Yes\nDirection: In\nAction: Block\n"
                "Program: C:\\Program Files\\Wavr\\wavr-core.exe\n")
    r = lan_reachability.check(program=r"C:\Program Files\Wavr\wavr-core.exe",
                               port=8000, force=True)
    assert r.state == lan_reachability.BLOCKED, r
    assert r.rules, "it must name the rule, or nobody can remove it"


def test_a_block_beats_an_allow(monkeypatch):
    """Because that is what Windows itself does. Reporting ALLOWED next to a
    block would be the product disagreeing with the machine it runs on."""
    monkeypatch.setattr(lan_reachability, "_is_windows", lambda: True)
    monkeypatch.setattr(
        lan_reachability, "_netsh_rules",
        lambda: "Rule Name: wavr allow\n"
                "----------------------------------\n"
                "Enabled: Yes\nDirection: In\nAction: Allow\n"
                "Program: C:\\wavr\\wavr-core.exe\n"
                "----------------------------------\n"
                "Rule Name: wavr block\n"
                "Enabled: Yes\nDirection: In\nAction: Block\n"
                "Program: C:\\wavr\\wavr-core.exe\n")
    r = lan_reachability.check(program=r"C:\wavr\wavr-core.exe", port=8000,
                               force=True)
    assert r.state == lan_reachability.BLOCKED, r


def test_a_port_is_matched_exactly(monkeypatch):
    """`8000` must not match a rule about `18000`. A substring match here would
    invent a verdict from somebody else's rule."""
    monkeypatch.setattr(lan_reachability, "_is_windows", lambda: True)
    monkeypatch.setattr(
        lan_reachability, "_netsh_rules",
        lambda: "Rule Name: something on 18000\n"
                "----------------------------------\n"
                "Enabled: Yes\nDirection: In\nAction: Block\n"
                "LocalPort: 18000\n")
    r = lan_reachability.check(program="", port=8000, force=True)
    assert r.state == lan_reachability.UNKNOWN, r
