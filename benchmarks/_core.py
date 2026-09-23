"""Start a real Wavr Core for measurement, with hardware edges faked on request.

Only the transport at the very edge is replaced -- the ARP sweep, the BLE radio
scan -- so everything Wavr itself does per cycle (the source loop, fusion,
storage, the hub, the supervisor) runs for real, and the numbers describe Wavr
rather than a mock of it.

    python benchmarks/_core.py --fake network,ble,inventory

`bench.py` drives this; it is also runnable by hand. It writes its own PID and
two wall-clock marks to the file named by WAVR_BENCH_MARKS, because on Windows a
venv's `python.exe` is a launcher stub whose child is the real interpreter, and
measuring the stub measures nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time

_T_START = time.time()

# A plausible household: 30 devices on one /24, one of them the "known" phone.
FAKE_KNOWN_MAC = "aa:bb:cc:00:00:01"
FAKE_BLE_ADDR = "aa:bb:cc:00:00:02"


def _fake_arp_text(n: int = 30) -> str:
    lines = ["Interface: 192.168.50.10 --- 0x9",
             "  Internet Address      Physical Address      Type"]
    for i in range(1, n + 1):
        mac = FAKE_KNOWN_MAC if i == 1 else f"02-00-00-00-00-{i:02x}"
        lines.append(f"  192.168.50.{i + 1:<14} {mac.replace(':', '-'):<21} dynamic")
    return "\n".join(lines) + "\n"


def _patch(fakes: set[str]) -> None:
    if "network" in fakes:
        import wavr.sources.network as net

        async def fake_arp_scan() -> set[str]:
            await asyncio.sleep(0.05)
            return {FAKE_KNOWN_MAC, "02:00:00:00:00:02"}

        net.arp_scan = fake_arp_scan
    if "inventory" in fakes:
        import wavr.netinventory as inv
        text = _fake_arp_text()

        async def fake_arp_output() -> str:
            await asyncio.sleep(0.05)
            return text

        inv._arp_output = fake_arp_output
    if "ble" in fakes:
        import wavr.sources.ble as ble

        async def fake_bleak_scan(duration: float = 5.0) -> dict[str, int]:
            # A real scan holds the radio for its whole window; keep the timing.
            await asyncio.sleep(duration)
            return {FAKE_BLE_ADDR: -60, "11:22:33:44:55:66": -71}

        ble.bleak_scan = fake_bleak_scan


def _mark(**kv) -> None:
    path = os.environ.get("WAVR_BENCH_MARKS")
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as fh:
            marks = json.load(fh)
    except (OSError, ValueError):
        marks = {}
    marks.update(kv)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(marks, fh)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", default="")
    args = ap.parse_args()
    _mark(pid=os.getpid(), t_interp_ready=_T_START)
    _patch({f for f in args.fake.split(",") if f})
    t0 = time.time()
    from wavr.serve import main as serve_main   # imports wavr.app -> create_app()
    _mark(t_app_built=time.time(), import_and_create_app_s=time.time() - t0)
    serve_main()


if __name__ == "__main__":
    main()
