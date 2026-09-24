"""Generate conformance/*.json from the canonical Python implementation.

    python scripts/gen_conformance.py            # rewrite the fixtures
    python scripts/gen_conformance.py --check    # exit 1 if any fixture is stale

Wavr has one set of semantics and, since native/ exists, more than one
implementation of some of them. These fixtures are how that stays true: every
expected value below is COMPUTED by the Python code that owns the behaviour, and
every other implementation (native/tests) must reproduce it exactly. Nothing here
is typed in by hand except the inputs -- and one table, heartbeat.json, which
encodes docs/NODE_PROTOCOL's client rules because the Core is the server side of
that exchange and has no client to run; it says so in its own `source` field.

backend/tests/test_conformance_fixtures_are_current.py runs `--check`, so a change
to the Python semantics that is not regenerated here fails the suite.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "conformance"
sys.path.insert(0, str(REPO / "backend"))
os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")

from wavr import capabilities as cap  # noqa: E402
from wavr import client_view as cv  # noqa: E402
from wavr import status as st  # noqa: E402
from wavr.runtime_status import unreachable  # noqa: E402
from wavr.sources.mmwave import parse_ld2450_frame, take_ld2450_frame  # noqa: E402


def _status() -> dict:
    def finding(key, text, **kw):
        return {"key": key, "text": text, **kw}

    runtimes = {
        "healthy": {"state": "healthy", "space": "My Home", "role": "",
                    "uptime_s": 93784, "last_state_age_s": 4.2,
                    "findings": [finding("state", "Rooms updated 4 seconds ago."),
                                 finding("sensors", "3 sensors reporting.")]},
        "starting": {"state": "starting", "space": "", "uptime_s": 12,
                     "last_state_age_s": None, "findings": []},
        "degraded": {"state": "degraded", "space": "Flat 2", "role": "primary",
                     "uptime_s": 7260, "last_state_age_s": 65,
                     "findings": [finding("state", "The kitchen radar is not reporting.",
                                          state="degraded"),
                                  finding("nodes", "1 of 2 nodes reporting.")]},
        "attention": {"state": "attention", "space": "Casa", "uptime_s": 59,
                      "last_state_age_s": 3,
                      "findings": [finding("state", "A camera needs a password.",
                                           state="attention")]},
        "unavailable": {"state": "unavailable", "space": "", "uptime_s": 4000,
                        "last_state_age_s": 3700,
                        "findings": [finding("state", "Nothing produced for an hour.",
                                             state="unavailable")]},
        "paused": {"state": "paused", "space": "Office", "uptime_s": 360000,
                   "last_state_age_s": 0.4, "findings": []},
        "unknown_state": {"state": "reticulating", "uptime_s": None, "findings": []},
    }
    attentions = {
        "clean": {"headline": "Nothing needs your attention", "total": 0},
        "one": {"headline": "1 thing needs you", "total": 1},
        "could_not_check": {"headline": "Nothing needs your attention", "total": 0,
                            "could_not_check": ["alerts"]},
        "unreadable": None,
    }
    cases = []
    for rk, runtime in runtimes.items():
        for ak, attention in attentions.items():
            cases.append({
                "name": f"{rk}/{ak}",
                "runtime": runtime,
                "attention": attention,
                "exit_code": st.exit_code(runtime, attention),
                "text_ascii": st.render(runtime, attention, st._MARK_ASCII),
                "text_unicode": st.render(runtime, attention, st._MARK),
            })
    return {
        "source": "backend/wavr/status.py (exit_code, render)",
        "exit_codes": {"ok": st.OK, "attention": st.ATTENTION,
                       "unreachable": st.UNREACHABLE},
        "unreachable_runtime": unreachable().to_dict(),
        "cases": cases,
    }


def _tiers() -> dict:
    cases = []
    for plat in ("linux", "windows", "android", "macos", "esp32", "unknown"):
        for ram in (None, 128, 449, 450, 512, 900, 1799, 1800, 4096, 6999, 7000, 16384):
            for cpus in (None, 1, 2, 3, 4, 8):
                tier = cap._compute_tier(ram, cpus, plat)
                cases.append({"platform": plat, "ram_mb": ram, "cpu_count": cpus,
                              "compute_tier": tier,
                              "can_be_core": cap._can_be_core(plat, ram, tier)})
    return {"source": "backend/wavr/capabilities.py (_compute_tier, _can_be_core)",
            "min_core_ram_mb": cap.MIN_CORE_RAM_MB, "cases": cases}


def _vocabulary() -> dict:
    return {"source": "backend/wavr/capabilities.py",
            "capability_keys": list(cap.CAPABILITY_KEYS),
            "platforms": list(cap.PLATFORMS),
            "device_functions": sorted(cap.DEVICE_FUNCTIONS),
            "compute_tiers": list(cap.COMPUTE_TIERS)}


def _frame(slots) -> bytes:
    body = b""
    for slot in slots:
        body += slot if slot is not None else b"\x00" * 8
    return b"\xaa\xff\x03\x00" + body + b"\x55\xcc"


def _slot(x_mm, y_mm, v_cms) -> bytes:
    def sm(v):   # LD2450 sign-magnitude: bit 15 set means positive
        return (0x8000 | v) if v >= 0 else (-v)
    import struct
    return struct.pack("<HHHH", sm(x_mm), sm(y_mm), sm(v_cms), 360)


def _ld2450() -> dict:
    one = _frame([_slot(-450, 1250, 0), None, None])
    two = _frame([_slot(120, 3000, -35), _slot(1500, 800, 60), None])
    empty = _frame([None, None, None])
    streams = {
        "one_clean_frame": one,
        "two_frames_back_to_back": one + two,
        "noise_before_header": b"\x12\x34\xaa\xff\x00" + two,
        "split_header_needs_more": b"\x00\xaa\xff",
        "partial_frame_needs_more": one[:17],
        "frame_then_partial": empty + two[:10],
        "long_noise_is_trimmed": bytes(range(256)) * 17,
        "noise_then_frame_after_trim": bytes([7]) * 5000 + one,
    }
    cases = []
    for name, data in streams.items():
        frames, buf = [], data
        while True:
            frame, buf = take_ld2450_frame(buf)
            if frame is None:
                break
            frames.append(frame)
        cases.append({
            "name": name, "stream_hex": data.hex(),
            "frames_hex": [f.hex() for f in frames],
            "leftover_hex": buf.hex(),
            # What the Core will make of each forwarded frame, for an end-to-end
            # check against a real Core -- the native node never parses.
            "targets": [[{"id": t.id, "x": t.x, "y": t.y, "velocity": t.velocity}
                         for t in parse_ld2450_frame(f)] for f in frames],
        })
    return {"source": "backend/wavr/sources/mmwave.py (take_ld2450_frame, "
                      "parse_ld2450_frame)",
            "max_buffer": 4096, "keep_on_overflow": 64, "cases": cases}


def _heartbeat() -> dict:
    # The client half of firmware/NODE_PROTOCOL.md, "Data plane" -> heartbeat,
    # and the kill-switch state machine. Hand-written from the spec on purpose:
    # the Core only ever sends these, so there is no Python client to generate
    # them from. Every row cites the sentence it encodes.
    rows = [
        ("network_error", None, None, "keep", "no HTTP response at all: keep state, retry next tick"),
        ("ok", 200, {"command": "ok", "state": "active"}, "active", "ok = sense normally"),
        ("run_legacy", 200, {"command": "run"}, "active", "legacy synonym of ok"),
        ("sleep", 200, {"command": "sleep", "state": "disabled"}, "disabled", "operator disabled: stop the sensor, slow heartbeat"),
        ("revoked_body", 200, {"command": "revoked"}, "revoked", "token dead: factory-reset and re-enroll"),
        ("unauthorized", 401, {"detail": "missing node bearer token"}, "revoked", "a 401 on heartbeat is identical to revoked"),
        ("forbidden", 403, {"detail": "invalid or revoked node token"}, "revoked", "a 403 on heartbeat is identical to revoked"),
        ("server_error", 500, {"detail": "boom"}, "keep", "not a kill signal; retry next tick"),
        ("unknown_command", 200, {"command": "dance"}, "keep", "an unknown command changes nothing"),
        ("garbage_body", 200, "not json", "keep", "an unparseable 200 changes nothing"),
    ]
    return {"source": "firmware/NODE_PROTOCOL.md (spec table, hand-written: the "
                      "Core has no node client to generate this from)",
            "cases": [{"name": n, "http_status": s, "body": b, "next_state": ns, "rule": why}
                      for n, s, b, ns, why in rows]}


def _loopback() -> dict:
    # Who counts as THIS machine decides where certificate checks may be skipped
    # and where plain HTTP may carry the local token. The hostile rows are the
    # point: names that merely start like a loopback address.
    urls = ["http://127.0.0.1:8000", "https://127.0.0.5:8000", "https://127.255.255.254",
            "https://localhost:8000", "https://LOCALHOST", "https://[::1]:8000",
            "https://127.0.0.1.attacker.example:8000", "https://127.evil.example",
            "https://localhost.attacker.example", "https://127.0.0.1@attacker.example",
            "https://192.168.1.57:8000", "https://10.0.0.9", "https://0.0.0.0:8000",
            "https://128.0.0.1", "https://[::2]:8000", "https://127.1",
            "https://[::ffff:127.0.0.1]:8000", "https://[::ffff:10.0.0.1]:8000",
            "https://core.example.com"]
    return {"source": "backend/wavr/status.py (is_loopback)",
            "cases": [{"url": u, "loopback": st.is_loopback(u)} for u in urls]}


def _client_view() -> dict:
    # The native client snapshot: well-formed answers, and every way an answer
    # can arrive garbled. JSON has no NaN, so non-finite numbers are covered by
    # the Python unit test instead.
    healthy = {"state": "healthy", "headline": "Wavr is running", "space": "Home",
               "role": "core", "uptime_s": 3600, "last_state_age_s": 4.5,
               "findings": [{"key": "sensors", "state": "healthy", "text": "3 sensors",
                             "text_template": "{n} sensors", "text_args": {"n": 3},
                             "detail": None}]}
    inbox = {"total": 1, "blocking": 0, "degraded": 1, "info": 0,
             "headline": "1 thing needs your attention", "could_not_check": [],
             "items": [{"key": "cam-url", "band": "degraded", "title": "Camera needs a URL",
                        "detail": "Kitchen camera", "where": "kitchen", "action": "open",
                        "since": "2026-09-24T10:00:00+00:00", "count": 1,
                        "evidence": {"x": 1}}]}
    empty_inbox = {"total": 0, "blocking": 0, "degraded": 0, "info": 0,
                   "headline": "Nothing needs your attention", "could_not_check": [],
                   "items": []}
    room = {"occupied": True, "confidence": 0.87, "person_count": 1,
            "precision_level": "position", "explanation": "mmwave: presence",
            "ts": "2026-09-24T10:00:00+00:00", "vitals": {"hr": 60},
            "targets": [{"x": 1, "y": 2}], "identities": ["alice"],
            "sources": [{"modality": "mmwave", "sensor_id": "a", "presence": True,
                         "confidence": 0.9, "age_s": 3, "health": "fresh", "count": 1,
                         "reliability": 0.5}]}
    watched = {**room, "targets": [], "identities": [], "vitals": {}, "watch": True,
               "unrecognized": True}
    garbled_room = {"occupied": "yes", "confidence": "0.9", "person_count": 1.5,
                    "precision_level": 3, "explanation": None, "ts": 12,
                    "watch": "true", "sources": [7, {"modality": 1, "presence": 1,
                                                     "confidence": True, "age_s": "3",
                                                     "health": None, "count": False}]}
    rows = [
        ("healthy_empty_inbox", healthy, empty_inbox, {"office": room, "bath": {**room, "occupied": False}}, True, None),
        ("attention_item", healthy, inbox, {"office": room}, True, None),
        ("watch_on", healthy, empty_inbox, {"office": watched}, True, None),
        ("inbox_could_not_check", healthy, {**empty_inbox, "could_not_check": ["pairings", 7]}, {}, True, None),
        ("inbox_wrong_shape", healthy, [], {}, True, None),
        ("inbox_unreadable", healthy, None, {}, True, None),
        ("degraded_runtime", {**healthy, "state": "degraded"}, empty_inbox, {}, True, None),
        ("unavailable_runtime", {**healthy, "state": "unavailable"}, None, None, True, None),
        ("runtime_wrong_shape", ["healthy"], empty_inbox, {}, True, None),
        ("garbled_fields", {**healthy, "uptime_s": "long", "findings": [1, {"key": 2}]},
         {**inbox, "total": "1", "items": ["x", {"count": True}]},
         {"office": garbled_room, "bad": 5}, True, None),
        ("state_wrong_shape", healthy, empty_inbox, ["office"], True, None),
        ("core_not_answering", None, None, None, False, "cannot connect to 127.0.0.1:8000"),
    ]
    return {"source": "backend/wavr/client_view.py (snapshot)",
            "cases": [{"name": n, "runtime": r, "attention": a, "state": s_,
                       "reachable": ok, "error": err,
                       "snapshot": cv.snapshot(r, a, s_, reachable=ok, error=err)}
                      for n, r, a, s_, ok, err in rows]}


FIXTURES = {
    "status.json": _status,
    "compute_tier.json": _tiers,
    "vocabulary.json": _vocabulary,
    "ld2450_framing.json": _ld2450,
    "heartbeat.json": _heartbeat,
    "loopback.json": _loopback,
    "client_view.json": _client_view,
}


def render_all() -> dict[str, str]:
    return {name: json.dumps(fn(), indent=1, ensure_ascii=False, sort_keys=True) + "\n"
            for name, fn in FIXTURES.items()}


def main(argv=None) -> int:
    check = "--check" in (argv or sys.argv[1:])
    stale = []
    for name, text in render_all().items():
        path = OUT / name
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != text:
            stale.append(name)
            if not check:
                OUT.mkdir(exist_ok=True)
                path.write_text(text, encoding="utf-8", newline="\n")
    if check and stale:
        print("stale conformance fixtures (run scripts/gen_conformance.py): "
              + ", ".join(stale))
        return 1
    print(("up to date" if not stale else "wrote " + ", ".join(stale)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
