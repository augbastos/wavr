"""Interleaved cold-start / latency / RSS A/B of the torch and ORT paths.

    python onnx_ab.py <workdir> [rounds]

Reuses child.py and ort_path.py written by onnx_spike.py. Alternates the two
paths round by round (as benchmarks/ab_*.py do) so machine drift lands on both;
reports medians plus every run's audit events and sampled connections.
"""
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

import psutil

WORK = Path(sys.argv[1])
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 7
MODELS = {"torch": WORK / "yolov8n.pt", "ort": WORK / "yolov8n.onnx"}


def one(mode):
    p = psutil.Popen([sys.executable, str(WORK / "child.py"), mode, str(MODELS[mode]),
                      str(WORK / "sample.jpg"), str(WORK / "ort_path.py")],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    conns, peak, seen = set(), 0, set()
    while p.poll() is None:
        # The whole tree: a venv's python.exe on Windows is a launcher stub,
        # and the interpreter doing the work is its child.
        try:
            procs = [p, *p.children(recursive=True)]
        except psutil.Error:
            procs = [p]
        for q in procs:
            try:
                seen.add(q.pid)
                peak = max(peak, q.memory_info().rss)
                for c in q.net_connections(kind="inet"):
                    conns.add(f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else f"listen:{c.laddr.port}")
            except psutil.Error:
                pass
        time.sleep(0.02)
    out, err = p.communicate()
    r = json.loads(out.strip().splitlines()[-1])
    r["peak_rss_mb"] = round(peak / 2**20, 1)     # the largest process of the tree
    r["processes_sampled"] = len(seen)
    r["connections"] = sorted(conns)
    return r


res = {"torch": [], "ort": []}
for i in range(ROUNDS):
    for mode in (("torch", "ort") if i % 2 == 0 else ("ort", "torch")):
        res[mode].append(one(mode))

out = {"rounds": ROUNDS}
for mode, runs in res.items():
    out[mode] = {
        "cold_start_s": round(statistics.median(r["cold_s"] for r in runs), 3),
        "latency_p50_ms": round(statistics.median(r["p50_ms"] for r in runs), 1),
        "peak_rss_mb": round(statistics.median(r["peak_rss_mb"] for r in runs), 1),
        "rss_after_warmup_mb": round(statistics.median(r["rss_mb"] for r in runs), 1),
        "threads": statistics.median(r["threads"] for r in runs),
        "processes_sampled": max(r["processes_sampled"] for r in runs),
        "audit_events": sorted({e[0] for r in runs for e in r["audit_events"]}),
        "connections": sorted({c for r in runs for c in r["connections"]}),
    }
print(json.dumps(out, indent=1))
