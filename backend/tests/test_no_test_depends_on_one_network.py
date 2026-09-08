"""A test that only passes on the author's Wi-Fi is not a test.

`in_subnet(peer, local)` asks whether a caller shares a /24 with THIS MACHINE,
and the Core reads its own address from `_local_ipv4()`. So a test that builds
a peer at a hardcoded `192.168.1.50` is asking a question whose answer depends
on the network the test happens to be running on:

    developer box on 192.168.1.x   ->  in subnet   ->  pairs   ->  green
    CI runner on 10.x / 172.x      ->  off subnet  ->  403     ->  red

`test_experience_grants.py` did exactly that. It passed locally every time and
failed on the first CI run that got far enough to execute it, as
`KeyError: 'device_id'` — the pairing had been refused and nobody was looking
at the status code. It cost a full CI cycle to find, and the fix was one line
that other test files already had.

So the rule is enforced rather than remembered: a test that hands `TestClient`
a LAN peer address must also pin what the Core thinks its own address is.
Pinning is what makes the question answerable anywhere.

Loopback peers are exempt — they are the one case `authorize` resolves without
asking about subnets at all.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

AQUI = Path(__file__).resolve().parent

# `TestClient(app, client=("192.168.1.50", 12345))` and friends.
PAR_LAN = re.compile(
    r"""client\s*=\s*\(\s*["'](\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})["']""")

# And the shape that got past the first version of this check: the peer as a
# module constant, `LAN = ("192.168.1.50", 4444)`, used as `client=LAN`.
#
# That hole mattered. `test_node_enrollment_e2e.py` is written exactly that
# way, it was the ONE file in the six this check named that was already failing
# on CI for this reason, and the only reason it appeared at all was an
# unrelated inline `8.8.8.8` further down. A guard that finds the real defect
# by accident is a guard that will miss the next one.
CONSTANTE_LAN = re.compile(
    r"""^\s*[A-Z_][A-Z0-9_]*\s*=\s*\(\s*["'](\d{1,3}(?:\.\d{1,3}){3})["']""",
    re.M)

LOOPBACK = re.compile(r"^(127\.|0\.0\.0\.0$)")

# Any of these means the file has decided what the Core's own address is.
FIXA_O_IP = (
    "_local_ipv4",          # the app's own resolver, monkeypatched
    "host_subnet",          # passed explicitly to auth helpers
    "in_subnet",            # the file is testing the predicate itself
)


def _codigo(fonte: str) -> str:
    """What the file DOES, with what it says about itself removed.

    Proved necessary by a mutation, and that is what mutations are for.
    Deleting the pin from `test_node_enrollment_e2e` left this check GREEN,
    because the comment explaining the pin still contained the word
    `in_subnet` — so the guard was satisfied by prose describing a line that
    was no longer there. `test_localisation` learned the same lesson about
    catalogue entries kept alive by a comment, and answers it the same way.

    Parsed rather than tokenised, and the distinction matters here: the pin is
    written `monkeypatch.setattr("wavr.app._local_ipv4", …)`, so the evidence
    IS a string literal and dropping every string would break the check
    outright. What has to go is the subset of strings that are prose about the
    code — docstrings — plus the comments, which `ast` never sees at all.

    A file that will not parse is returned whole. Refusing to judge it would be
    the quieter failure, and this file exists because quiet failures are the
    problem.
    """
    import ast
    try:
        arvore = ast.parse(fonte)
    except SyntaxError:
        return fonte

    docstrings = set()
    for no in ast.walk(arvore):
        if isinstance(no, (ast.Module, ast.ClassDef, ast.FunctionDef,
                           ast.AsyncFunctionDef)):
            corpo = getattr(no, "body", None)
            if (corpo and isinstance(corpo[0], ast.Expr)
                    and isinstance(corpo[0].value, ast.Constant)
                    and isinstance(corpo[0].value.value, str)):
                docstrings.add(id(corpo[0].value))

    pedacos = []
    for no in ast.walk(arvore):
        if isinstance(no, ast.Name):
            pedacos.append(no.id)
        elif isinstance(no, ast.Attribute):
            pedacos.append(no.attr)
        elif isinstance(no, ast.Constant) and isinstance(no.value, str):
            if id(no) not in docstrings:
                pedacos.append(no.value)
    return " ".join(pedacos)


def _arquivos():
    for p in sorted(AQUI.glob("test_*.py")):
        if p.name == Path(__file__).name:
            continue
        yield p


def test_a_lan_peer_is_always_paired_with_a_pinned_local_address():
    culpados = []
    for p in _arquivos():
        fonte = io.open(p, encoding="utf-8", errors="replace").read()
        achados = [m.group(1) for m in PAR_LAN.finditer(fonte)]
        # A constant only counts when the file actually hands it to a client;
        # a bare address in a fixture's config is not a peer.
        if re.search(r"client\s*=\s*[A-Z_][A-Z0-9_]*\b", fonte):
            achados += [m.group(1) for m in CONSTANTE_LAN.finditer(fonte)]
        pares = [a for a in achados if not LOOPBACK.match(a)]
        if not pares:
            continue
        if any(marca in _codigo(fonte) for marca in FIXA_O_IP):
            continue
        culpados.append(f"{p.name}: peer {sorted(set(pares))[0]} with nothing "
                        f"pinning the Core's own address")

    assert not culpados, (
        "these tests ask whether a peer is on the same network as the machine "
        "running them, which makes them true on some networks and false on "
        "others:\n  " + "\n  ".join(culpados)
        + "\n\nPin it, the way test_app.py does:\n"
          '    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")')
