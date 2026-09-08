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

LOOPBACK = re.compile(r"^(127\.|0\.0\.0\.0$)")

# Any of these means the file has decided what the Core's own address is.
FIXA_O_IP = (
    "_local_ipv4",          # the app's own resolver, monkeypatched
    "host_subnet",          # passed explicitly to auth helpers
    "in_subnet",            # the file is testing the predicate itself
)


def _arquivos():
    for p in sorted(AQUI.glob("test_*.py")):
        if p.name == Path(__file__).name:
            continue
        yield p


def test_a_lan_peer_is_always_paired_with_a_pinned_local_address():
    culpados = []
    for p in _arquivos():
        fonte = io.open(p, encoding="utf-8", errors="replace").read()
        pares = [m.group(1) for m in PAR_LAN.finditer(fonte)
                 if not LOOPBACK.match(m.group(1))]
        if not pares:
            continue
        if any(marca in fonte for marca in FIXA_O_IP):
            continue
        culpados.append(f"{p.name}: peer {sorted(set(pares))[0]} with nothing "
                        f"pinning the Core's own address")

    assert not culpados, (
        "these tests ask whether a peer is on the same network as the machine "
        "running them, which makes them true on some networks and false on "
        "others:\n  " + "\n  ".join(culpados)
        + "\n\nPin it, the way test_app.py does:\n"
          '    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")')
