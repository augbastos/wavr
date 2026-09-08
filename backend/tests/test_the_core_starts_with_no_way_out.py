"""The README says no cloud is required for anything Wavr does. Prove it.

That sentence is the strongest claim in this repository, and until now nothing
produced it. `test_offline_launch.py` cuts the network at the BROWSER and
reloads a page whose service worker has already been installed — a real test of
the shell's precache, and no test at all of the Core: the server it talks to is
running on the same machine with a working internet connection the whole time.

So a dependency on something outside the house — a DNS lookup at import time, a
version check on start, a font or a CDN in a served page, a token exchange —
would pass every check here and fail on the first installation in a garage with
no uplink.

## How the network is actually cut

Not by trusting a flag. `socket.socket` is replaced for the child process, at
the bottom, before anything imports: a connect to a loopback or LAN address is
allowed through (that is the product's own Space, and the test client has to
reach it), and a connect to anything else raises `OSError` the same way an
unplugged cable does. `getaddrinfo` refuses any name that is not localhost,
which is what a house with no uplink does to `api.example.com`.

That is a stronger cut than pulling the Wi-Fi: it also removes the DNS cache
and any captive portal that would answer.

What is deliberately NOT tested here: an optional integration that a household
has switched on. A cloud narrator is supposed to need the internet. The claim
is about what Wavr does BEFORE anybody switches anything on.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Run inside the child, before `wavr` is imported. Anything that reaches past
# the local Space raises, so a hidden dependency surfaces as a traceback rather
# than as a slow start nobody notices.
CORTA_A_REDE = textwrap.dedent('''
    # stdlib first, unpatched. `ssl` does `from socket import socket` and then
    # `class SSLSocket(socket)`, so replacing the class before ssl is imported
    # builds the whole TLS stack on top of the test double — which failed with
    # a TypeError that had nothing to do with the network. Importing them here
    # costs nothing and is not what this test is measuring: the question is
    # whether WAVR reaches outside, and Wavr is imported after the cut.
    import asyncio, ipaddress, socket, ssl, sys

    _real_socket = socket.socket
    _real_getaddrinfo = socket.getaddrinfo

    def _local(host):
        if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "", None):
            return True
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        return ip.is_loopback or ip.is_private or ip.is_link_local

    class Tesoura(_real_socket):
        def connect(self, address):
            host = address[0] if isinstance(address, tuple) else address
            if not _local(host):
                raise OSError(101, "Network is unreachable (test: no uplink)")
            return super().connect(address)

        def connect_ex(self, address):
            host = address[0] if isinstance(address, tuple) else address
            if not _local(host):
                return 101
            return super().connect_ex(address)

    def _getaddrinfo(host, *a, **k):
        if not _local(host):
            raise socket.gaierror(-2, f"Name or service not known: {host!r}")
        return _real_getaddrinfo(host, *a, **k)

    # `create_connection` looks `socket` up in the module's own globals, which
    # is this same dict, so one assignment covers both callers.
    socket.socket = Tesoura
    socket.getaddrinfo = _getaddrinfo

    import uvicorn
    uvicorn.run("wavr.app:app", host="127.0.0.1", port=int(sys.argv[1]),
                log_level="warning")
''')


@pytest.fixture(scope="module")
def core_sem_saida(tmp_path_factory):
    """A Core started in a process that cannot reach anything but the LAN."""
    dir_ = tmp_path_factory.mktemp("sem-saida")
    porta = _free_port()
    script = dir_ / "sem_uplink.py"
    script.write_text(CORTA_A_REDE, encoding="utf-8")

    env = {
        **os.environ,
        "WAVR_DB": str(dir_ / "wavr.db"),
        "WAVR_HOUSE_MAP": str(dir_ / "house.json"),
        "WAVR_LOCAL_TOKEN": "",
        "PYTHONPATH": str(BACKEND),
        # No cloud credentials of any kind reach the child. If something needs
        # one to START, that is the finding.
        "OPENAI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
    }
    proc = subprocess.Popen(
        [sys.executable, str(script), str(porta)],
        cwd=str(BACKEND), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace")

    base = f"http://127.0.0.1:{porta}"
    limite = time.monotonic() + 90
    while time.monotonic() < limite:
        if proc.poll() is not None:
            saida = proc.stdout.read() if proc.stdout else ""
            pytest.fail(
                "the Core exited instead of starting with no route off this "
                f"machine:\n{saida[-4000:]}")
        try:
            with socket.create_connection(("127.0.0.1", porta), timeout=0.5):
                break
        except OSError:
            time.sleep(0.25)
    else:
        proc.kill()
        saida = proc.stdout.read() if proc.stdout else ""
        pytest.fail(
            "the Core never came up with the uplink cut. Something in the "
            f"start path is waiting on the internet:\n{saida[-4000:]}")

    try:
        yield base
    finally:
        proc.kill()
        proc.wait(timeout=15)


def _get(base: str, caminho: str, cabecalhos: dict | None = None):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(base + caminho,
                                 headers=cabecalhos or {"X-Wavr-Local": "1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_the_scissors_actually_cut():
    """The control, and it runs first.

    Every assertion in this file rests on the child process genuinely having no
    way out. If the patch above stopped biting — a rename in `socket`, an
    import order that captures the real class first, a typo in `_local` — every
    test here would keep passing while proving nothing at all, which is the
    quietest way a guarantee dies.

    So the same scissors are applied in a throwaway child and asked to refuse
    two things a Core with an uplink could do: resolve a public name, and open
    a socket to a public address. Loopback must still work, or the cut would be
    proving the wrong thing by strangling the product.
    """
    # `CORTA_A_REDE` is already dedented, so the tail is dedented on its own —
    # dedenting the concatenation finds no common indent and leaves the tail
    # exactly as indented as it was written.
    prova = CORTA_A_REDE.split("import uvicorn")[0] + textwrap.dedent("""
    import json
    r = {}
    try:
        socket.getaddrinfo("example.invalid", 80); r["dns"] = "resolved"
    except Exception as e:
        r["dns"] = type(e).__name__
    try:
        s = socket.socket(); s.settimeout(3); s.connect(("93.184.216.34", 80))
        r["publico"] = "connected"
    except OSError as e:
        r["publico"] = f"OSError:{e.args[0]}"
    try:
        srv = _real_socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
        c = socket.socket(); c.settimeout(3); c.connect(srv.getsockname())
        r["loopback"] = "connected"; c.close(); srv.close()
    except Exception as e:
        r["loopback"] = type(e).__name__
    print(json.dumps(r))
    """)
    script = Path(os.environ.get("TEMP", ".")) / "wavr_scissors_probe.py"
    script.write_text(prova, encoding="utf-8")
    try:
        out = subprocess.run([sys.executable, str(script)], capture_output=True,
                             text=True, timeout=60)
        linha = [l for l in out.stdout.splitlines() if l.startswith("{")]
        assert linha, (f"the probe printed nothing:\n{out.stdout}\n"
                       f"{out.stderr}")
        r = json.loads(linha[-1])
    finally:
        script.unlink(missing_ok=True)

    assert r["dns"] == "gaierror", (
        f"a public name still resolves inside the cut child ({r['dns']!r}); "
        f"the offline tests below are not testing offline")
    assert r["publico"].startswith("OSError"), (
        f"a public address is still reachable inside the cut child "
        f"({r['publico']!r}); the offline tests below prove nothing")
    assert r["loopback"] == "connected", (
        f"loopback is blocked too ({r['loopback']!r}), so the child cannot "
        f"serve its own dashboard and the tests below would fail for the "
        f"wrong reason")


def test_the_core_comes_up_with_no_route_off_this_machine(core_sem_saida):
    """Starting is the claim. If the fixture yielded, it started."""
    status, _ = _get(core_sem_saida, "/healthz")
    assert status == 200, f"/healthz answered {status} with no uplink"


def test_it_serves_the_dashboard_rather_than_a_page_that_needs_the_internet(
        core_sem_saida):
    """A 200 on `/` is not enough: the page has to be the product.

    Checked for the shell's own markers, and separately for anything the page
    would have to FETCH from outside to render — a CDN script, a webfont, an
    analytics beacon. A dashboard that serves fine and then blanks because
    fonts.googleapis.com never answers is not a local-first product; it is a
    local-first server in front of a page that is not.
    """
    status, corpo = _get(core_sem_saida, "/")
    assert status == 200, f"the dashboard answered {status} with no uplink"
    texto = corpo.decode("utf-8", "replace")
    assert "<title>" in texto and "js/i18n.js" in texto, (
        "`/` answered 200 but did not serve the dashboard")

    import re
    fora = sorted(set(re.findall(
        r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)["\']', texto)))
    # A link a person may click is not a load. Only things the BROWSER fetches
    # to render the page count, and those are src= or a stylesheet href.
    carregados = [u for u in fora
                  if re.search(r'src\s*=\s*["\']' + re.escape(u), texto)
                  or re.search(r'rel=["\']stylesheet["\'][^>]*'
                               + re.escape(u), texto)]
    assert not carregados, (
        "the dashboard loads these from outside the house, so it does not "
        f"render on a Space with no uplink:\n  " + "\n  ".join(carregados))


def test_nothing_off_the_machine_is_switched_on_by_default(core_sem_saida):
    """The other half of "no cloud is required": nothing is on to begin with.

    `_egress_now` is the one producer the tray, the privacy screen and the
    trust screen all read (see `test_egress_is_visible.py`). On a Core that has
    just been created it has to be empty — an integration that ships enabled
    would make the claim false on the first boot, before anybody chose
    anything.
    """
    status, corpo = _get(core_sem_saida, "/api/runtime")
    assert status == 200, f"/api/runtime answered {status}"
    body = json.loads(corpo)
    saidas = [f for f in (body.get("findings") or [])
              if f.get("kind") == "egress" or "egress" in str(f.get("id", ""))]
    assert not saidas, (
        f"a fresh Core reports something already leaving the network: {saidas}")


def test_the_space_answers_about_itself_without_asking_anybody(core_sem_saida):
    """Presence is the product, and it must not need a lookup.

    A Core with no sources still has a Space, a map and a runtime state, and
    every one of those answers has to be composed here. This is the difference
    between "works offline" as a feature and as the default.
    """
    for caminho in ("/api/status", "/api/rooms", "/api/house"):
        status, corpo = _get(core_sem_saida, caminho)
        assert status in (200, 404), (
            f"{caminho} answered {status} with no uplink — a local read should "
            f"never depend on the internet")
        if status == 200:
            json.loads(corpo)          # and it is real JSON, not an error page
