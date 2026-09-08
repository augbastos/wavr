"""The name the Core broadcasts must be a name the phone accepts.

## The failure

Turning on "Let other devices connect" makes the Core advertise itself on the
LAN over mDNS, as `_wavr._tcp`, with a TXT record. The phone app's first screen
after install browses that exact service type and offers what it finds.

The Core advertised `role=desktop`. The phone accepted only `role=core`:

    if(String(txt.role || "").toLowerCase() !== "core") return null;

So the phone received the advertisement of the Core sitting on the same Wi-Fi
and threw it away, then said "We couldn't find a Wavr hub on this Wi-Fi yet.
Make sure your hub is on and this phone is on the same network." Both of those
were already true. The first user turned his VPN off to rule that out, which it
was not.

`role` says WHICH KIND OF MACHINE hosts the Core — `core` for the Android
Core-launcher, `desktop` for `wavr/app.py`. Both are Cores. Filtering on it as
though it meant "is this a Core" excluded the host kind almost everybody
installs.

## Why the check reads two files

One decision, two implementations, in two languages, in two repositories — the
shape this codebase keeps finding at the bottom of its worst days. They cannot
share code, so instead they are compared: the string the backend actually
passes to `advertise_self` must appear in the list the phone actually accepts.

Neither side is trusted to describe itself. Both are read.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
APP = RAIZ / "backend" / "wavr" / "app.py"
from tests.mobile_tree import mobile_dir   # noqa: E402 -- shared lookup

_MOBILE = mobile_dir()
LIB_CANDIDATES = [
    (_MOBILE / "src" / "wavr-lib.js") if _MOBILE else Path("nao-existe"),
    RAIZ / "mobile" / "src" / "wavr-lib.js",
]


def _sem_comentarios_js(fonte: str) -> str:
    """The comment above CORE_ROLES lists every role by name, including the one
    the bug excluded. Reading it would satisfy this check with prose."""
    fonte = re.sub(r"/\*.*?\*/", " ", fonte, flags=re.S)
    return re.sub(r"^\s*//.*$", "", fonte, flags=re.M)


def _papeis_que_o_core_anuncia() -> set[str]:
    """Every `role=` the backend hands to `advertise_self`, from the call sites."""
    fonte = io.open(APP, encoding="utf-8", newline="").read()
    fonte = re.sub(r"^\s*#.*$", "", fonte, flags=re.M)
    chamadas = re.findall(r"advertise_self\((.*?)\)", fonte, flags=re.S)
    assert chamadas, "nothing calls advertise_self any more — has mDNS moved?"
    papeis = set()
    for c in chamadas:
        m = re.search(r"""role\s*=\s*["']([^"']+)["']""", c)
        # No explicit role means the default in `mdns_peers.advertise_self`.
        papeis.add(m.group(1) if m else _papel_padrao())
    return papeis


def _papel_padrao() -> str:
    fonte = io.open(RAIZ / "backend" / "wavr" / "mdns_peers.py",
                    encoding="utf-8", newline="").read()
    m = re.search(r"""def advertise_self\([^)]*?role:\s*str\s*=\s*["']([^"']+)["']""",
                  fonte, flags=re.S)
    assert m, "advertise_self no longer has a default role to read"
    return m.group(1)


def _papeis_que_o_celular_aceita() -> set[str]:
    for p in LIB_CANDIDATES:
        if p.is_file():
            fonte = _sem_comentarios_js(io.open(p, encoding="utf-8", newline="").read())
            break
    else:
        pytest.skip("the mobile lib is not in this checkout")
    m = re.search(r"CORE_ROLES\s*=\s*\[(.*?)\]", fonte, flags=re.S)
    assert m, (
        "the phone no longer has a CORE_ROLES list. If the role check was "
        "removed entirely that may be fine, but this contract check cannot "
        "see it any more — read parseCoreService and decide deliberately.")
    return {s.strip().strip("\"'").lower() for s in m.group(1).split(",") if s.strip()}


def test_the_phone_accepts_every_role_the_core_broadcasts():
    anunciados = _papeis_que_o_core_anuncia()
    aceitos = _papeis_que_o_celular_aceita()
    orfaos = sorted(anunciados - aceitos)
    assert not orfaos, (
        f"the Core advertises role={orfaos} and the phone accepts {sorted(aceitos)}. "
        f"A device broadcasting a role the app discards is discovered and then "
        f"thrown away, and the person is told nothing was found — which is what "
        f"happened with role=desktop.")


def test_the_desktop_core_is_one_of_them():
    """The specific one that was missing, named, so a future list that drops it
    fails here rather than on somebody's phone."""
    assert "desktop" in _papeis_que_o_celular_aceita(), (
        "the phone no longer accepts role=desktop — the Core that runs on a "
        "computer, which is the one nearly every install has")


def test_the_guard_still_refuses_something():
    """Accepting everything would also pass the test above, and would offer to
    connect to whatever else ever appears on this service type."""
    aceitos = _papeis_que_o_celular_aceita()
    assert aceitos, "the accepted-roles list is empty"
    assert "printer" not in aceitos and len(aceitos) < 6, (
        f"the role check has been widened until it stops being a check: "
        f"{sorted(aceitos)}")
