"""The LIVE known-provider seam: a registration/opt-out changes what the source
returns on the NEXT scan cycle, with no reconstruction (= no server restart)."""
from wavr.sources.ble import BLESource
from wavr.sources.network import NetworkSource


async def test_ble_provider_reread_live():
    state = {"map": {}}

    def provider():
        return dict(state["map"])

    async def scan():
        return {"aa:bb:cc:dd:ee:ff": -50}

    src = BLESource({}, scan=scan, interval=0, rssi_min=-80, grace=0,
                    known_provider=provider)
    agen = src.events()
    try:
        ev1 = await agen.__anext__()          # registry empty -> no scan -> absent
        assert ev1.presence is False
        state["map"] = {"aa:bb:cc:dd:ee:ff": "alice"}   # <-- register, no restart
        ev2 = await agen.__anext__()          # next cycle re-reads -> present
        assert ev2.presence is True
        state["map"] = {}                     # <-- opt-out
        ev3 = await agen.__anext__()          # stops being a signal next cycle
        assert ev3.presence is False
    finally:
        await agen.aclose()


async def test_ble_without_provider_is_unchanged():
    async def scan():
        return {"aa:bb:cc:dd:ee:ff": -50}
    src = BLESource({"aa:bb:cc:dd:ee:ff": "alice"}, scan=scan,
                    interval=0, rssi_min=-80)
    agen = src.events()
    try:
        ev = await agen.__anext__()
        assert ev.presence is True            # frozen env map still works
    finally:
        await agen.aclose()


async def test_network_provider_reread_live_with_identity():
    state = {"map": {}}

    def provider():
        return dict(state["map"])

    async def scan():
        return {"aa:bb:cc:dd:ee:ff"}

    src = NetworkSource(set(), scan=scan, interval=0, grace=0,
                        emit_identity=True, known_provider=provider)
    agen = src.events()
    try:
        ev1 = await agen.__anext__()          # empty registry -> absent
        assert ev1.presence is False and ev1.identities == ()
        state["map"] = {"aa:bb:cc:dd:ee:ff": "alice"}   # register network device
        ev2 = await agen.__anext__()
        assert ev2.presence is True
        assert [i.to_dict() for i in ev2.identities] == [
            {"person": "alice", "source": "network", "rssi": None}
        ]
    finally:
        await agen.aclose()
