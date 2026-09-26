"""What the native runtime costs, beside the Python it can stand in for.

    python benchmarks/native_footprint.py --wavr build/native/wavr[.exe] [--seconds 30]

Measures, on this machine:
  * binary size (as built, and stripped when a `strip` is on PATH);
  * start-up: `wavr version` vs `python -c pass`, and `wavr status` vs
    `python -m wavr.status` against a local stub that answers like a healthy
    Core (a closed port would measure the OS's connect timeout instead);
  * the Node role running: peak RSS and CPU-seconds of `wavr node run` over a
    window, replaying LD2450 frames to a local sink that answers like a Core.

Needs psutil (the `bench` extra). Prints one JSON object. Like every result in
this directory, it describes one machine and is compared only with itself.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import psutil

REPO = Path(__file__).resolve().parents[1]


def _median_wall(cmd: list[str], n: int = 15, env: dict | None = None) -> float:
    times = []
    for _ in range(n):
        t = time.perf_counter()
        r = subprocess.run(cmd, capture_output=True, env=env)
        if r.returncode != 0:          # a failing command is not a start-up time
            raise SystemExit(f"{cmd} exited {r.returncode}: {r.stderr[-300:]!r}")
        times.append(time.perf_counter() - t)
    return round(statistics.median(times) * 1000, 1)


class _Sink(http.server.BaseHTTPRequestHandler):
    """Answers the Node Protocol like an active Core, over plain HTTP on
    loopback (the native client allows unverified transport only there)."""

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        body = (b'{"command":"ok","state":"active"}' if self.path.endswith("heartbeat")
                else b'{"accepted":true}')
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        body = (b'{"total":0}' if "attention" in self.path
                else b'{"state":"ok","headline":"Wavr is fine","sections":[]}')
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _sink() -> http.server.ThreadingHTTPServer:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Sink)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _node_run(wavr: str, seconds: int) -> dict:
    srv = _sink()
    tmp = Path(tempfile.mkdtemp(prefix="wavr-native-fp-"))
    state = tmp / "node.json"
    state.write_text(json.dumps({"url": f"http://127.0.0.1:{srv.server_port}",
                                 "node_id": "fp", "token": "fp", "pin": "", "state": "active",
                                 "seq": 0, "press_count": 0}))
    cases = json.loads((REPO / "conformance" / "ld2450_framing.json").read_text())["cases"]
    frames = tmp / "frames.txt"
    frames.write_text("\n".join(f for c in cases for f in c["frames_hex"]) + "\n")
    p = psutil.Popen([wavr, "node", "run", "--sensor", f"replay:{frames}",
                      "--state", str(state), "--seconds", str(seconds)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    peak = 0
    cpu = 0.0
    while p.poll() is None:
        try:
            peak = max(peak, p.memory_info().rss)
            t = p.cpu_times()
            cpu = t.user + t.system
        except psutil.NoSuchProcess:
            break
        time.sleep(0.1)
    err = p.stderr.read().decode(errors="replace").strip()
    srv.shutdown()
    return {"window_s": seconds, "exit": p.returncode, "peak_rss_mb": round(peak / 2**20, 2),
            "cpu_s": round(cpu, 3), "cpu_pct_of_one_core": round(100 * cpu / seconds, 2),
            "stderr": err[:300]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wavr", required=True)
    ap.add_argument("--seconds", type=int, default=30)
    a = ap.parse_args()
    wavr = os.path.abspath(a.wavr)
    out: dict = {"binary_bytes": os.path.getsize(wavr)}
    strip = shutil.which("strip")
    if strip:
        s = Path(tempfile.mkdtemp()) / Path(wavr).name
        subprocess.run([strip, "-o", str(s), wavr], check=True)
        out["binary_stripped_bytes"] = os.path.getsize(s)
    env = {k: v for k, v in os.environ.items() if not k.startswith("WAVR_")}
    env.update(PYTHONPATH=str(REPO / "backend"), PYTHON_DOTENV_DISABLED="1")
    srv = _sink()
    live = f"http://127.0.0.1:{srv.server_port}"
    out["startup_ms"] = {
        "native_version": _median_wall([wavr, "version"]),
        "python_bare": _median_wall([sys.executable, "-c", "pass"], env=env),
        "native_status": _median_wall([wavr, "status", "--url", live]),
        "python_status": _median_wall(
            [sys.executable, "-m", "wavr.status", "--url", live], env=env),
    }
    srv.shutdown()
    out["node_run"] = _node_run(wavr, a.seconds)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
