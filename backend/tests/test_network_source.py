from wavr.sources.network import parse_arp_table

WINDOWS_ARP = """
Interface: 192.168.0.10 --- 0x5
  Internet Address      Physical Address      Type
  192.168.0.1           AA-BB-CC-DD-EE-FF     dynamic
  192.168.0.23          11-22-33-44-55-66     dynamic
  192.168.0.255         ff-ff-ff-ff-ff-ff     static
"""

def test_parse_arp_table_extracts_normalized_macs():
    macs = parse_arp_table(WINDOWS_ARP)
    assert "aa:bb:cc:dd:ee:ff" in macs
    assert "11:22:33:44:55:66" in macs
    assert "ff:ff:ff:ff:ff:ff" in macs
    assert len(macs) == 3

def test_parse_arp_table_handles_colon_form_and_empty():
    assert parse_arp_table("host 0a:1b:2c:3d:4e:5f ok") == {"0a:1b:2c:3d:4e:5f"}
    assert parse_arp_table("") == set()
    assert parse_arp_table("no macs here 12-34") == set()


import asyncio
import pytest
from wavr.sources.network import NetworkSource

KNOWN = {"aa:bb:cc:dd:ee:ff"}

async def _first_n(source, n):
    out = []
    agen = source.events()
    try:
        async for ev in agen:
            out.append(ev)
            if len(out) == n:
                break
    finally:
        await agen.aclose()
    return out

async def test_network_source_present_when_known_mac_seen():
    async def scan():
        return {"aa:bb:cc:dd:ee:ff", "00:00:00:00:00:01"}
    src = NetworkSource(KNOWN, scan=scan, interval=0)
    [ev] = await _first_n(src, 1)
    assert ev.room == "casa"
    assert ev.modality == "network"
    assert ev.presence is True
    assert ev.confidence == 0.8
    assert ev.breathing_bpm is None and ev.heart_bpm is None
    assert ev.motion == 0.0
    assert ev.ts.endswith("+00:00")

async def test_network_source_absent_when_no_known_mac():
    async def scan():
        return {"00:00:00:00:00:02"}
    src = NetworkSource(KNOWN, scan=scan, interval=0)
    [ev] = await _first_n(src, 1)
    assert ev.presence is False
    assert ev.confidence == 0.0

async def test_network_source_grace_holds_presence_across_misses():
    seq = [ {"aa:bb:cc:dd:ee:ff"}, set(), set(), set() ]  # seen, miss, miss, miss
    calls = {"i": 0}
    async def scan():
        i = calls["i"]; calls["i"] += 1
        return seq[min(i, len(seq) - 1)]
    src = NetworkSource(KNOWN, scan=scan, interval=0, grace=2)
    evs = await _first_n(src, 4)
    # tick0 seen -> present; tick1 miss#1 -> still present (grace); tick2 miss#2 -> still present; tick3 miss#3 -> absent
    assert [e.presence for e in evs] == [True, True, True, False]

async def test_arp_scan_parses_real_command_output(monkeypatch):
    from wavr.sources import network
    async def fake_run(*args):
        if args[0] == "ping":
            return ""
        return "iface\n  192.168.0.1  AA-BB-CC-DD-EE-FF  dynamic\n"
    monkeypatch.setattr(network, "_run", fake_run)
    monkeypatch.setattr(network, "_local_ipv4", lambda: None)  # skip the ping sweep
    macs = await network.arp_scan()
    assert "aa:bb:cc:dd:ee:ff" in macs
