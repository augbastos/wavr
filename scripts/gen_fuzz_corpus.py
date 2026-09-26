"""Seed corpora for native/fuzz, built from the conformance answer key.

    python scripts/gen_fuzz_corpus.py OUT_DIR

Writes OUT_DIR/<target>/<n>.bin for every target in native/fuzz/targets.cpp.
Seeds are real, valid inputs (the fixtures Python generated) so mutation
starts from the shapes the runtime actually meets; malformed variants are the
driver's job. Generated, not committed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIX = REPO / "conformance"


def _load(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _j(v) -> bytes:
    return b"" if v is None else json.dumps(v).encode()


def _http(body: bytes, chunked: bool = False) -> bytes:
    if chunked:
        mid = len(body) // 2
        parts = [body[:mid], body[mid:]]
        enc = b"".join(b"%x\r\n%s\r\n" % (len(p), p) for p in parts if p) + b"0\r\n\r\n"
        return b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + enc
    return b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body)


def seeds() -> dict[str, list[bytes]]:
    cv = _load("client_view.json")["cases"]
    st = _load("status.json")["cases"]
    out: dict[str, list[bytes]] = {}
    out["fuzz_snapshot"] = [
        b"\0".join([_j(c["runtime"]), _j(c["attention"]), _j(c["state"]),
                    b"\x01" if c["reachable"] else b"\x00",
                    (c["error"] or "").encode()]) for c in cv]
    out["fuzz_status"] = [_j(c["runtime"]) + b"\0" + _j(c["attention"]) for c in st]
    out["fuzz_http_response"] = [_http(_j(c["runtime"]), chunked=i % 2 == 1)
                                 for i, c in enumerate(st)]
    out["fuzz_framer"] = [bytes.fromhex(c["stream_hex"])
                          for c in _load("ld2450_framing.json")["cases"]]
    out["fuzz_url"] = [c["url"].encode() for c in _load("loopback.json")["cases"]]
    hb = []
    for c in _load("heartbeat.json")["cases"]:
        code = -1 if c["http_status"] is None else c["http_status"]
        body = c["body"] if isinstance(c["body"], str) else _j(c["body"]).decode()
        hb.append((code + 1).to_bytes(2, "big") + body.encode())
    out["fuzz_heartbeat"] = hb
    out["fuzz_node_state"] = [
        _j({"url": "https://192.168.1.5:8000", "node_id": "n1", "token": "t", "pin": "AB:CD",
            "state": "active", "seq": 17, "press_count": 2}),
        _j({"url": "https://h", "token": "t", "state": "disabled"}),
        _j({"url": "", "token": ""}),
    ]
    return out


def main() -> None:
    root = Path(sys.argv[1])
    for target, items in seeds().items():
        d = root / target
        d.mkdir(parents=True, exist_ok=True)
        for i, b in enumerate(items):
            (d / f"{i:03d}.bin").write_bytes(b)
    print(f"wrote seeds for {len(seeds())} targets to {root}")


if __name__ == "__main__":
    main()
