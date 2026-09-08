"""When the phone finds no Core, it must not blame the network.

## What the screen used to say, and what was true

    We couldn't find a Wavr hub on this Wi-Fi yet.
    Make sure your hub is on and this phone is on the same network.

Both of those were already true. Measured, on the first user's machines, at the
moment he was reading that sentence:

* The Core was up and advertising: `Wavr._wavr._tcp.local. -> 192.168.1.226:8000`,
  `role=desktop`, visible from the laptop in under a second.
* His phone was on the same Wi-Fi — `wlan0`, `192.168.1.141/24`, MULTICAST flag
  set — and reachable; `adb` paired to it over that address.
* The firewall allowed the Core inbound on TCP and UDP, scoped to the profile
  the machine was actually on.

## What was actually happening

Finding a Core is a multicast query to 224.0.0.251. His phone had a VPN up, and
the tunnel carried a route for `224.0.0.0/3` — every multicast address there is
— while deliberately leaving `192.168.0.0/16` outside it. So:

* unicast to the Core: out over Wi-Fi, works. Typing the address works, pairing
  works, the dashboard works.
* the discovery query: into the tunnel, never reaches the Wi-Fi.

Confirmed by observation rather than by reading routes. A brand-new
`_wavrtest._tcp` service was advertised from the laptop and the phone was asked,
through its own ZeroConf plugin over a forwarded DevTools socket, to browse for
both it and `_wavr._tcp`:

    servico de teste : NADA
    servico do Wavr  : NADA

It sees no mDNS service of any kind on that network, while the laptop sees them
instantly. Nothing about Wavr's advertisement is involved.

## What is pinned

The screen cannot detect the routing — no app can read a VPN's routing table —
so it must not claim the VPN is the cause. What it must do is stop asserting the
two things that were already true, and name the likeliest reason when a VPN is
in fact up. `networkFacts` reports that much and no more.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
from tests.mobile_tree import mobile_dir   # noqa: E402 -- shared lookup

_MOBILE = mobile_dir()
SHIM_CANDIDATES = [
    (_MOBILE / "src" / "wavr-mobile-shim.js") if _MOBILE else Path("nao-existe"),
    RAIZ / "mobile" / "src" / "wavr-mobile-shim.js",
]
PLUGIN_CANDIDATES = [
    (_MOBILE / "plugins" / "wavr-net" / "WavrNetPlugin.kt") if _MOBILE
    else Path("nao-existe"),
    RAIZ / "mobile" / "plugins" / "wavr-net" / "WavrNetPlugin.kt",
]


def _primeiro(caminhos: list[Path], oque: str) -> Path:
    for p in caminhos:
        if p.is_file():
            return p
    pytest.skip(f"{oque} is not in this checkout")


@pytest.fixture(scope="module")
def shim() -> str:
    fonte = io.open(_primeiro(SHIM_CANDIDATES, "the mobile shim"),
                    encoding="utf-8", newline="").read()
    # This file's own comments quote the retired sentence to explain it.
    return re.sub(r"^\s*//.*$", "",
                  re.sub(r"/\*.*?\*/", " ", fonte, flags=re.S), flags=re.M)


@pytest.fixture(scope="module")
def plugin() -> str:
    return io.open(_primeiro(PLUGIN_CANDIDATES, "the WavrNet plugin"),
                   encoding="utf-8", newline="").read()


def test_the_screen_no_longer_asserts_what_was_already_true(shim):
    assert "Make sure your hub is on and this phone is on" not in shim, (
        "the discovery screen again tells the person to check that the hub is "
        "on and the phone is on the same network. Both were true when this was "
        "measured; the sentence sent him to inspect the working half.")


def test_the_screen_says_a_reachable_hub_can_still_be_unfindable(shim):
    assert "still not be found this way" in shim, (
        "the failure text no longer distinguishes 'not reachable' from 'not "
        "findable'. They are different states and only one of them was true.")


def test_the_vpn_line_exists_and_is_conditional(shim):
    """Named when a VPN is up. Never asserted as the cause, never shown when
    there is no VPN."""
    assert "networkFacts" in shim, (
        "the discovery screen no longer asks what the network looks like, so "
        "it cannot mention a VPN and is back to guessing")
    trecho = shim[shim.index("networkFacts"):]
    trecho = trecho[:2000]
    assert "vpnActive" in trecho, "the VPN answer is fetched but not read"
    assert re.search(r"if\s*\(\s*!f\s*\|\|\s*!f\.known\s*\|\|\s*!f\.vpnActive\s*\)", trecho), (
        "the VPN line is no longer gated on a VPN actually being up, or on the "
        "answer being known — an unknown network must say nothing")
    assert "usually capture" in trecho or "likely" in trecho.lower(), (
        "the VPN line states the cause instead of naming a likely one. The app "
        "cannot read a VPN's routing table and must not pretend to.")


def test_the_plugin_reports_the_network_without_claiming_a_cause(plugin):
    assert "fun networkFacts" in plugin, "networkFacts is gone from the plugin"
    corpo = plugin[plugin.index("fun networkFacts"):]
    corpo = corpo[:corpo.index("\n    private fun") if "\n    private fun" in corpo
                  else len(corpo)]
    assert "TRANSPORT_VPN" in corpo, "it no longer detects a VPN at all"
    assert '"known"' in corpo, (
        "there is no way for the caller to tell 'no VPN' from 'could not "
        "tell', and those must produce different screens")
    assert "catch" in corpo, (
        "a failure to read the network now fails the screen that asked; "
        "unknown has to be a survivable answer")
