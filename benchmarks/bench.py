"""Reproducible performance baseline for a Wavr Core.

    python benchmarks/bench.py --python <venv>/Scripts/python.exe --label baseline
    python benchmarks/bench.py --only imports,startup --repeat 7
    python benchmarks/bench.py --lan          # also sweep the real local /24

Writes `benchmarks/results/<label>.json` and prints a summary. Dev-only: it needs
`psutil` in the interpreter running THIS script (`pip install -e backend[bench]`),
never in the Core being measured.

What it measures, and how
-------------------------
imports   `python -X importtime -c "import wavr.app"` in a fresh process. Importing
          `wavr.app` also runs `create_app()` (the module builds `app` at import),
          so the `wavr.app` self-time includes app construction; the per-package
          totals are the imports themselves.
startup   Launch -> interpreter ready -> `wavr.app` imported and built -> first 200
          from /healthz -> first 200 from /api/runtime. Fresh database vs the
          database the previous run left behind.
idle      After the Core settles: CPU seconds per minute (process only), memory,
          threads, handles, child processes spawned, SQLite rows written, and
          requests made to a fake Home Assistant. One scenario per process.
pipeline  `benchmarks/pipeline.py`: event -> hub latency, fusion, SQLite, payloads.
footprint Installed size of the interpreter's site-packages, by distribution.

What it does NOT claim
----------------------
One machine's numbers describe that machine. Scenarios marked LAN touch the
local /24 exactly as a Core configured that way would; everything else fakes only
the hardware edge (`_core.py`). Cold-cache (post-reboot) starts are not measured:
every repeat after the first runs with the OS file cache warm.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import platform
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import psutil

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "benchmarks" / "_core.py"
RESULTS = REPO / "benchmarks" / "results"

# name -> (fakes for _core.py, extra env, setup steps, needs_lan)
SCENARIOS: dict[str, tuple[str, dict, tuple, bool]] = {
    "base": ("", {}, (), False),
    "network": ("network", {"WAVR_NET_MACS": "aa:bb:cc:00:00:01"}, (), False),
    "inventory": ("inventory", {"WAVR_NET_INVENTORY": "1"}, (), False),
    "ble": ("ble", {"WAVR_BLE_KNOWN": "aa:bb:cc:00:00:02=alex"}, (), False),
    "ha": ("", {}, ("ha",), False),
    "sim": ("", {}, ("sim",), False),
    "multi": ("network,ble,inventory",
              {"WAVR_NET_MACS": "aa:bb:cc:00:00:01", "WAVR_NET_INVENTORY": "1",
               "WAVR_BLE_KNOWN": "aa:bb:cc:00:00:02=alex"}, ("ha", "sim"), False),
    "network-lan": ("", {"WAVR_NET_MACS": "aa:bb:cc:00:00:01"}, (), True),
    "network+inventory-lan": ("", {"WAVR_NET_MACS": "aa:bb:cc:00:00:01",
                                   "WAVR_NET_INVENTORY": "1"}, (), True),
}

HA_ENTITIES = [f"binary_sensor.bench_{i}" for i in range(4)]


# -- fake Home Assistant ------------------------------------------------------

class _FakeHA(http.server.ThreadingHTTPServer):
    requests = 0


class _HAHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.server.requests += 1
        now = datetime.now(timezone.utc).isoformat()
        if self.path.startswith("/api/states/"):
            eid = self.path.rsplit("/", 1)[1]
            body = {"entity_id": eid, "state": "on", "last_changed": now,
                    "last_updated": now,
                    "attributes": {"device_class": "occupancy",
                                   "friendly_name": eid}}
        elif self.path == "/api/states":
            body = [{"entity_id": e, "state": "on", "last_changed": now,
                     "attributes": {"device_class": "occupancy"}} for e in HA_ENTITIES]
        else:
            body = {"message": "API running."}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):  # silence
        pass


def _start_fake_ha() -> _FakeHA:
    srv = _FakeHA(("127.0.0.1", 0), _HAHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# -- helpers --------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(port: int, method: str, path: str, body=None, timeout=5.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 method=method,
                                 headers={"X-Wavr-Local": "1",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (loopback)
        return r.status, r.read()


def _core_env(workdir: Path, port: int, extra: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("WAVR_")}
    env.update({
        "WAVR_PORT": str(port),
        "WAVR_DB": str(workdir / "wavr.db"),
        "WAVR_HOUSE_MAP": str(workdir / "house.json"),
        "WAVR_FRONTEND": str(REPO / "frontend"),
        # An ancestor .env must not configure the Core under measurement.
        "PYTHON_DOTENV_DISABLED": "1",
        "WAVR_BENCH_MARKS": str(workdir / "marks.json"),
        "PYTHONDONTWRITEBYTECODE": "",
    })
    env.update(extra)
    return env


def _db_rows(db: Path) -> int:
    if not db.exists():
        return 0
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        total = 0
        for (name,) in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"):
            total += con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        return total
    finally:
        con.close()


class Core:
    """One Core process under measurement."""

    def __init__(self, python: str, workdir: Path, fakes: str, extra_env: dict):
        self.workdir = workdir
        self.port = _free_port()
        self.env = _core_env(workdir, self.port, extra_env)
        self.python = python
        self.fakes = fakes
        self.stub = None
        self.proc: psutil.Process | None = None
        self.t0 = 0.0
        self.marks: dict = {}

    def start(self, timeout: float = 120.0) -> dict:
        self.t0 = time.time()
        self.stub = subprocess.Popen(
            [self.python, str(CORE), "--fake", self.fakes], cwd=self.workdir,
            env=self.env, stdout=subprocess.DEVNULL,
            stderr=open(self.workdir / "core.log", "w"))
        t_health = t_runtime = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.stub.poll() is not None:
                raise RuntimeError(f"Core exited: see {self.workdir / 'core.log'}")
            try:
                if t_health is None and _http(self.port, "GET", "/healthz", timeout=1)[0] == 200:
                    t_health = time.time()
                if t_health is not None and _http(self.port, "GET", "/api/runtime", timeout=2)[0] == 200:
                    t_runtime = time.time()
                    break
            except OSError:
                pass
            time.sleep(0.02)
        if t_runtime is None:
            raise RuntimeError("Core never answered /api/runtime")
        self.marks = json.loads((self.workdir / "marks.json").read_text())
        self.proc = psutil.Process(self.marks["pid"])
        m = self.marks
        return {
            "interpreter_s": round(m["t_interp_ready"] - self.t0, 3),
            "import_and_create_app_s": round(m["import_and_create_app_s"], 3),
            "serve_to_healthz_s": round(t_health - m["t_app_built"], 3),
            "healthz_s": round(t_health - self.t0, 3),
            "runtime_s": round(t_runtime - self.t0, 3),
        }

    def stop(self) -> None:
        with_suppress = (psutil.NoSuchProcess, psutil.AccessDenied)
        if self.proc is not None:
            try:
                for ch in self.proc.children(recursive=True):
                    ch.kill()
                self.proc.terminate()
                self.proc.wait(10)
            except with_suppress:
                pass
            except psutil.TimeoutExpired:
                self.proc.kill()
        if self.stub is not None:
            try:
                self.stub.wait(10)
            except subprocess.TimeoutExpired:
                self.stub.kill()


# -- sections -------------------------------------------------------------------

def run_imports(python: str, repeat: int) -> dict:
    work = Path(tempfile.mkdtemp(prefix="wavr-imp-"))
    env = _core_env(work, 0, {})
    runs = []
    for i in range(repeat + 1):
        r = subprocess.run([python, "-X", "importtime", "-c", "import wavr.app"],
                           cwd=work, env=env, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr[-2000:])
        rows = []
        for line in r.stderr.splitlines():
            if not line.startswith("import time:") or "|" not in line:
                continue
            parts = [p.strip() for p in line[len("import time:"):].split("|")]
            try:
                rows.append((int(parts[0]), int(parts[1]), parts[2]))
            except ValueError:
                continue
        if i == 0:
            continue                   # first run writes .pyc; discard
        by_pkg: dict[str, int] = {}
        for self_us, _cum, name in rows:
            top = name.strip().split(".")[0]
            by_pkg[top] = by_pkg.get(top, 0) + self_us
        total = next((cum for s, cum, n in rows if n.strip() == "wavr.app"), None)
        app_self = next((s for s, cum, n in rows if n.strip() == "wavr.app"), None)
        runs.append({"total_us": total, "wavr_app_self_us": app_self,
                     "by_package_us": by_pkg,
                     "modules": sorted({n.strip() for _s, _c, n in rows})})
    med = lambda k: int(statistics.median(r[k] for r in runs))  # noqa: E731
    pkgs: dict[str, list[int]] = {}
    for r in runs:
        for k, v in r["by_package_us"].items():
            pkgs.setdefault(k, []).append(v)
    top = sorted(((k, int(statistics.median(v))) for k, v in pkgs.items()),
                 key=lambda kv: -kv[1])[:20]
    heavy = ("numpy", "cv2", "torch", "ultralytics", "paho", "bleak", "serial",
             "zeroconf", "cryptography", "google", "mcp", "pydantic", "onnxruntime")
    loaded = set(runs[-1]["modules"])
    return {
        "repeat": len(runs),
        "total_ms_median": round(med("total_us") / 1000, 1),
        "total_ms_all": [round(r["total_us"] / 1000, 1) for r in runs],
        "wavr_app_self_ms_median": round(med("wavr_app_self_us") / 1000, 1),
        "top_packages_self_ms": {k: round(v / 1000, 1) for k, v in top},
        "heavy_packages_imported": sorted(h for h in heavy if h in loaded),
        "module_count": len(loaded),
    }


def run_startup(python: str, repeat: int) -> dict:
    fresh, existing = [], []
    for i in range(repeat + 1):
        work = Path(tempfile.mkdtemp(prefix="wavr-start-"))
        core = Core(python, work, "", {})
        try:
            r = core.start()
        finally:
            core.stop()
        if i == 0:
            continue
        fresh.append(r)
        core2 = Core(python, work, "", {})    # same dir -> existing DB
        try:
            existing.append(core2.start())
        finally:
            core2.stop()

    def agg(rows):
        return {k: {"median": round(statistics.median(r[k] for r in rows), 3),
                    "min": round(min(r[k] for r in rows), 3),
                    "max": round(max(r[k] for r in rows), 3)}
                for k in rows[0]}
    return {"repeat": len(fresh), "fresh_db": agg(fresh), "existing_db": agg(existing)}


def _setup(core: Core, steps: tuple, ha: _FakeHA | None) -> None:
    if "sim" in steps:
        _http(core.port, "POST", "/api/sources/sim/toggle", {"enabled": True})
    if "ha" in steps:
        rooms = ["kitchen", "living", "office", "bedroom"]
        for eid, room in zip(HA_ENTITIES, rooms):
            _http(core.port, "PUT", f"/api/ha/presence/{eid}",
                  {"room": room, "modality": "pir"})


def run_idle(python: str, name: str, settle: float, window: float) -> dict:
    fakes, extra, steps, _lan = SCENARIOS[name]
    ha = None
    if "ha" in steps:
        ha = _start_fake_ha()
        extra = {**extra, "WAVR_HA_URL": f"http://127.0.0.1:{ha.server_address[1]}",
                 "WAVR_HA_TOKEN": "bench-not-a-secret"}
    work = Path(tempfile.mkdtemp(prefix=f"wavr-idle-{name}-"))
    core = Core(python, work, fakes, extra)
    try:
        core.start()
        _setup(core, steps, ha)
        time.sleep(settle)
        p = core.proc
        db = work / "wavr.db"
        rows0 = _db_rows(db)
        ha0 = ha.requests if ha else 0
        psutil.cpu_percent(None)
        cpu0 = p.cpu_times()
        children: dict[int, float] = {}
        t0 = time.time()
        while time.time() - t0 < window:
            try:
                for ch in p.children(recursive=True):
                    try:
                        ct = ch.cpu_times()
                        children[ch.pid] = max(children.get(ch.pid, 0.0), ct.user + ct.system)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        children.setdefault(ch.pid, 0.0)
            except psutil.NoSuchProcess:
                break
            time.sleep(0.05)
        elapsed = time.time() - t0
        cpu1 = p.cpu_times()
        sys_cpu = psutil.cpu_percent(None)
        mem = p.memory_full_info()
        cpu_s = (cpu1.user + cpu1.system) - (cpu0.user + cpu0.system)
        out = {
            "window_s": round(elapsed, 1),
            "cpu_s_per_min": round(cpu_s / elapsed * 60, 3),
            "cpu_pct_one_core": round(cpu_s / elapsed * 100, 2),
            "rss_mb": round(mem.rss / 2**20, 1),
            "uss_mb": round(getattr(mem, "uss", 0) / 2**20, 1),
            "private_mb": round(getattr(mem, "private", 0) / 2**20, 1) or None,
            "threads": p.num_threads(),
            "handles_or_fds": (p.num_handles() if os.name == "nt" else p.num_fds()),
            "children_spawned_per_min": round(len(children) / elapsed * 60, 1),
            "children_cpu_s_per_min_lower_bound": round(sum(children.values()) / elapsed * 60, 3),
            "db_rows_written_per_min": round((_db_rows(db) - rows0) / elapsed * 60, 1),
            "ha_requests_per_min": round((ha.requests - ha0) / elapsed * 60, 1) if ha else None,
            "system_cpu_pct_during_window": sys_cpu,
        }
        return out
    finally:
        core.stop()
        if ha:
            ha.shutdown()


def run_pipeline(python: str, events: int) -> dict:
    r = subprocess.run([python, str(REPO / "benchmarks" / "pipeline.py"),
                        "--events", str(events)], capture_output=True, text=True,
                       env={k: v for k, v in os.environ.items() if not k.startswith("WAVR_")})
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-3000:])
    return json.loads(r.stdout.strip().splitlines()[-1])


def run_footprint(python: str) -> dict:
    r = subprocess.run([python, str(REPO / "benchmarks" / "footprint.py")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-3000:])
    return json.loads(r.stdout)


def environment(python: str) -> dict:
    """What a reader needs to reproduce the run, and nothing that describes the
    machine it ran on: results are committed, and a hardware inventory in a
    public repository is not ours to publish. Compare runs from ONE machine."""
    ver = subprocess.run([python, "-c", "import sys,platform;print(sys.version.split()[0]);"
                          "print(platform.machine())"], capture_output=True, text=True)
    sha = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "-C", str(REPO), "status", "--porcelain",
                                 "--", "backend"], capture_output=True,
                                text=True).stdout.strip())
    return {
        "when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git": sha + ("+dirty-backend" if dirty else ""),
        "os_family": platform.system(),
        "core_python": ver.stdout.split()[0] if ver.stdout else None,
        "core_env": Path(python).resolve().parents[1].name,
        "scope": "one development machine; compare runs from the same machine only",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--label", default="run")
    ap.add_argument("--only", default="imports,startup,idle,pipeline,footprint")
    ap.add_argument("--scenarios", default="base,network,inventory,ble,ha,sim,multi")
    ap.add_argument("--lan", action="store_true",
                    help="also run scenarios that sweep the real local /24")
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--settle", type=float, default=20.0)
    ap.add_argument("--window", type=float, default=60.0)
    ap.add_argument("--events", type=int, default=1000)
    args = ap.parse_args()
    only = set(args.only.split(","))
    res: dict = {"label": args.label, "environment": environment(args.python)}
    if "imports" in only:
        res["imports"] = run_imports(args.python, args.repeat)
        print("imports", json.dumps(res["imports"]["total_ms_median"]), flush=True)
    if "startup" in only:
        res["startup"] = run_startup(args.python, args.repeat)
        print("startup", json.dumps(res["startup"]["fresh_db"]["runtime_s"]), flush=True)
    if "idle" in only:
        names = [s for s in args.scenarios.split(",") if s]
        if args.lan:
            names += [n for n, sc in SCENARIOS.items() if sc[3] and n not in names]
        res["idle"] = {}
        for n in names:
            res["idle"][n] = run_idle(args.python, n, args.settle, args.window)
            print("idle", n, json.dumps(res["idle"][n]), flush=True)
    if "pipeline" in only:
        res["pipeline"] = run_pipeline(args.python, args.events)
        print("pipeline done", flush=True)
    if "footprint" in only:
        res["footprint"] = run_footprint(args.python)
        print("footprint", res["footprint"].get("total_mb"), flush=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{args.label}.json"
    out.write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
