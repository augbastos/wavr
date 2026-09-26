"""Check a live LD2450 UART using the Core's canonical framer and parser."""
from __future__ import annotations

import argparse
import sys
import time


def verdict(code: int, summary: str, *evidence: str) -> int:
    print(f"{('PASS', 'FAIL', 'CANNOT TEST')[code]}: {summary}")
    for line in evidence:
        print(line)
    return code


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", help="serial device, for example /dev/ttyUSB0")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--min-frames", type=int, default=3)
    args = ap.parse_args()
    if not args.port:
        return verdict(2, "no serial device supplied", "Supply --port /dev/ttyUSB0.")
    if args.seconds <= 0 or args.min_frames < 1:
        return verdict(2, "invalid test duration or frame threshold")
    try:
        import serial  # pyserial is the optional mmwave extra.
    except ImportError:
        return verdict(2, "pyserial is unavailable", "Install the optional mmwave extra on the test host.")
    try:
        from wavr.sources.mmwave import parse_ld2450_frame, take_ld2450_frame
    except ImportError:
        return verdict(2, "Wavr backend parser is unavailable", "Set PYTHONPATH to this checkout's backend directory.")

    try:
        port = serial.Serial(args.port, 256000, timeout=0.2)
    except (OSError, serial.SerialException) as exc:
        return verdict(2, "serial port cannot be opened", f"port={args.port}", f"reason={type(exc).__name__}")

    bytes_read = valid = malformed = occupied = 0
    buffer = b""
    try:
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            chunk = port.read(256)
            bytes_read += len(chunk)
            buffer += chunk
            while True:
                frame, buffer = take_ld2450_frame(buffer)
                if frame is None:
                    break
                if not frame.endswith(b"\x55\xcc"):
                    malformed += 1
                    continue
                targets = parse_ld2450_frame(frame)
                valid += 1
                occupied += bool(targets)
    except (OSError, serial.SerialException) as exc:
        return verdict(1, "serial read failed", f"reason={type(exc).__name__}", f"bytes={bytes_read}")
    finally:
        port.close()

    evidence = (f"port={args.port} baud=256000 duration_s={args.seconds:g}",
                f"bytes={bytes_read} valid_frames={valid} malformed_frames={malformed} occupied_frames={occupied} minimum={args.min_frames}")
    if bytes_read == 0:
        return verdict(1, "no bytes; check wiring, power and baud", *evidence)
    if valid == 0:
        return verdict(1, "bytes arrived but no valid LD2450 frames; check baud and protocol", *evidence)
    if valid < args.min_frames:
        return verdict(1, "too few valid frames", *evidence)
    if occupied == 0:
        return verdict(0, "valid frames; zero targets is normal when nobody is in view", *evidence)
    return verdict(0, "valid frames and target readings", *evidence)


if __name__ == "__main__":
    sys.exit(main())
