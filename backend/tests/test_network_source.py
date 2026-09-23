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

async def test_network_source_skips_scan_when_no_known_macs():
    called = {"v": False}
    async def scan():
        called["v"] = True
        return set()
    src = NetworkSource(set(), scan=scan, interval=0)
    [ev] = await _first_n(src, 1)
    assert called["v"] is False
    assert ev.presence is False
    assert ev.confidence == 0.0

async def test_network_source_scans_when_known_macs_present():
    called = {"v": False}
    async def scan():
        called["v"] = True
        return {"aa:bb:cc:dd:ee:ff"}
    src = NetworkSource(KNOWN, scan=scan, interval=0)
    [ev] = await _first_n(src, 1)
    assert called["v"] is True
    assert ev.presence is True

async def test_network_identity_attached_when_enabled():
    # A labelled MAC that is seen gets named (rssi is None — ARP has no signal).
    async def scan():
        return {"aa:bb:cc:dd:ee:ff", "00:00:00:00:00:01"}
    src = NetworkSource(KNOWN, scan=scan, interval=0, emit_identity=True,
                        known={"AA-BB-CC-DD-EE-FF": "alice"})
    [ev] = await _first_n(src, 1)
    assert ev.presence is True
    assert [i.to_dict() for i in ev.identities] == [
        {"person": "alice", "source": "network", "rssi": None}
    ]


async def test_network_identity_absent_when_flag_off():
    async def scan():
        return {"aa:bb:cc:dd:ee:ff"}
    src = NetworkSource(KNOWN, scan=scan, interval=0,
                        known={"aa:bb:cc:dd:ee:ff": "alice"})
    [ev] = await _first_n(src, 1)
    assert ev.presence is True and ev.confidence == 0.8
    assert ev.identities == ()


async def test_network_identity_house_level_only():
    async def scan():
        return {"aa:bb:cc:dd:ee:ff"}
    src = NetworkSource(KNOWN, scan=scan, interval=0, emit_identity=True,
                        known={"aa:bb:cc:dd:ee:ff": "alice"})
    [ev] = await _first_n(src, 1)
    assert ev.room == "casa"


async def test_detail_provider_emits_label_for_optin_mac_with_global_flag_off():
    # Per-device opt-in (consent #2): global emit_identity is OFF, but the MAC is
    # in the live detail_provider allowlist -> its label still emits.
    async def scan():
        return {"aa:bb:cc:dd:ee:ff"}
    src = NetworkSource(KNOWN, scan=scan, interval=0,
                        known={"aa:bb:cc:dd:ee:ff": "alice"},
                        detail_provider=lambda: {"aa:bb:cc:dd:ee:ff"})
    [ev] = await _first_n(src, 1)
    assert ev.presence is True
    assert [i.to_dict() for i in ev.identities] == [
        {"person": "alice", "source": "network", "rssi": None}
    ]


async def test_detail_provider_never_labels_a_non_optin_mac():
    # A second present, labelled MAC NOT in the allowlist stays unlabelled --
    # opting one device in must never widen to every present device.
    known_two = {"aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"}
    async def scan():
        return known_two
    src = NetworkSource(known_two, scan=scan, interval=0,
                        known={"aa:bb:cc:dd:ee:ff": "alice",
                               "11:22:33:44:55:66": "housemate"},
                        detail_provider=lambda: {"aa:bb:cc:dd:ee:ff"})
    [ev] = await _first_n(src, 1)
    people = {i.person for i in ev.identities}
    assert people == {"alice"}


async def test_detail_provider_none_is_byte_identical_to_before():
    # Default (no detail_provider) with the global flag off: no identities at all --
    # unchanged from before the per-device seam existed.
    async def scan():
        return {"aa:bb:cc:dd:ee:ff"}
    src = NetworkSource(KNOWN, scan=scan, interval=0,
                        known={"aa:bb:cc:dd:ee:ff": "alice"})
    [ev] = await _first_n(src, 1)
    assert ev.identities == ()


async def test_arp_scan_parses_real_command_output(monkeypatch):
    from wavr.sources import network
    async def fake_run(*args):
        if args[0] == "ping":
            return ""
        return "iface\n  192.168.0.1  AA-BB-CC-DD-EE-FF  dynamic\n"
    monkeypatch.setattr(network, "_run", fake_run)
    monkeypatch.setattr(network, "_local_ipv4", lambda: None)  # skip the warm-up sweep
    macs = await network.arp_scan()
    assert "aa:bb:cc:dd:ee:ff" in macs


# -- The shared sweep ------------------------------------------------------------

def _counting_sweep(monkeypatch):
    """The real `arp_table_text`, with the edges counted instead of performed."""
    from wavr.sources import network
    calls = {"warm": 0, "arp": 0}

    def warm(ip):
        calls["warm"] += 1
        return 253

    async def fake_run(*args):
        assert args == ("arp", "-a"), "no ping subprocess, ever"
        calls["arp"] += 1
        await asyncio.sleep(0.01)
        return "  192.168.0.7  aa-bb-cc-dd-ee-ff  dynamic\n"

    monkeypatch.setattr(network, "_local_ipv4", lambda: "192.168.0.10")
    monkeypatch.setattr(network, "_warm_arp_cache", warm)
    monkeypatch.setattr(network, "_run", fake_run)
    monkeypatch.setattr(network, "_ARP_SETTLE_S", 0.0)
    return network, calls


async def test_the_presence_source_and_the_inventory_share_one_sweep(monkeypatch):
    # They each ran their own 254-process sweep, on their own timers. Both at
    # once -- or one within seconds of the other -- is now one sweep.
    from wavr import netinventory
    network, calls = _counting_sweep(monkeypatch)
    macs, text = await asyncio.gather(network.arp_scan(), netinventory._arp_output())
    assert calls == {"warm": 1, "arp": 1}
    assert "aa:bb:cc:dd:ee:ff" in macs and "aa-bb-cc-dd-ee-ff" in text
    await network.arp_scan()                       # well inside the reuse window
    assert calls == {"warm": 1, "arp": 1}


async def test_a_sweep_older_than_the_window_is_repeated(monkeypatch):
    network, calls = _counting_sweep(monkeypatch)
    await network.arp_table_text()
    await network.arp_table_text(max_age_s=0.0)    # the caller wants it fresh
    assert calls == {"warm": 2, "arp": 2}


def test_the_warm_up_is_datagrams_to_the_own_subnet_not_processes():
    # Loopback, so nothing leaves the machine: one empty datagram per host of the
    # /24, the sender itself excluded, and no child process at all.
    from wavr.sources import network
    assert network._warm_arp_cache("127.0.0.5") == 253
