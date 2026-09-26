"""Chaos soak for the native Node: a fake Core that misbehaves on a schedule.

    python native/tests/soak_node.py --wavr build/native/wavr[.exe] [--minutes 10]

The fake Core speaks the Node Protocol over TLS on loopback and cycles through
phases: normal, garbage bodies, HTTP 500, stale-sequence 409s, stalls longer
than the node's timeout, the Core gone, a DIFFERENT certificate (pin mismatch),
disable (423 + "sleep"), and finally revocation. Timers are accelerated.

Checked, not just survived:
  * no request -- so no bearer token -- ever reaches a server whose
    certificate is not the pinned one;
  * no telemetry is accepted-attempted while the node knows it is disabled
    (one in-flight grace);
  * the node keeps running through every outage, and exits 3 with its state
    erased when revoked;
  * resident memory, handles/fds and threads do not grow: the last quarter of
    the run is compared with the first (after warm-up).

Needs `cryptography` and `psutil` (dev + bench extras). Prints one JSON report.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.server
import json
import os
import socket
import ssl
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import psutil

REPO = Path(__file__).resolve().parents[2]
PHASES = [("normal", 20), ("garbage", 5), ("http500", 5), ("stale409", 5), ("stall", 10),
          ("down", 10), ("wrongcert", 10), ("normal", 15), ("disabled", 10), ("normal", 10)]


def make_cert(tmp: Path, name: str) -> tuple[str, str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=30)).sign(key, hashes.SHA256()))
    cert_p, key_p = tmp / f"{name}.crt", tmp / f"{name}.key"
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_p.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                        serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    der = cert.public_bytes(serialization.Encoding.DER)
    fp = hashlib.sha256(der).hexdigest().upper()
    return str(cert_p), str(key_p), ":".join(fp[i:i + 2] for i in range(0, 64, 2))


class FakeCore:
    def __init__(self, port: int, certs: dict):
        self.port, self.certs = port, certs
        self.phase = "normal"
        self.counts: dict[str, int] = {}
        self.violations: list[str] = []
        self.node_disabled_since: float | None = None
        self.lock = threading.Lock()
        self.srv = None
        self.cert = None

    def count(self, k):
        with self.lock:
            self.counts[k] = self.counts.get(k, 0) + 1

    def start(self, cert: str):
        core = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length", 0) or 0)
                self.rfile.read(min(n, 1 << 20))
                kind = self.path.rsplit("/", 1)[-1]
                core.count(f"{core.phase}:{kind}")
                if core.cert != "good":
                    core.violations.append(f"request {self.path} reached a server with the wrong certificate")
                if kind == "telemetry" and core.node_disabled_since and \
                        time.monotonic() - core.node_disabled_since > 1.5:
                    core.violations.append("telemetry sent while the node knew it was disabled")
                ph = core.phase
                if ph == "stall":
                    time.sleep(8)
                if ph == "garbage":
                    return self.reply(200, b"\x00\xff{not json" * 3)
                if ph == "http500":
                    return self.reply(500, b"oops")
                if ph == "stale409" and kind == "telemetry":
                    return self.reply(409, b'{"detail":"stale"}')
                if ph == "disabled":
                    if kind == "telemetry":
                        return self.reply(423, b'{"detail":"node disabled"}')
                    if kind == "heartbeat":
                        if core.node_disabled_since is None:
                            core.node_disabled_since = time.monotonic()
                        return self.reply(200, b'{"command":"sleep","state":"disabled"}')
                if ph == "revoked":
                    return self.reply(403, b'{"detail":"invalid or revoked node token"}')
                core.node_disabled_since = None
                if kind == "heartbeat":
                    return self.reply(200, b'{"command":"ok","state":"active"}')
                return self.reply(200, b'{"accepted":true}')

            def reply(self, code, body):
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

        class S(http.server.ThreadingHTTPServer):
            daemon_threads = True
            # Not on Windows: there SO_REUSEADDR lets a second listener share the
            # port, and a restarted fake Core kept losing connections to the old,
            # wrong-certificate one (the node rightly refused it). Found the hard way.
            allow_reuse_address = os.name != "nt"

        srv = S(("127.0.0.1", self.port), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*self.certs[cert][:2])
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        self.srv, self.cert = srv, cert
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        # The harness proves its own setup: the certificate on the wire is the one intended.
        pem = ssl.get_server_certificate(("127.0.0.1", self.port), timeout=5)
        fp = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()
        served = ":".join(fp[i:i + 2] for i in range(0, 64, 2))
        assert served == self.certs[cert][2], (
            f"fake Core serves the wrong certificate after starting with {cert!r}")

    def stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None
            self.cert = None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sample(p: psutil.Process) -> dict | None:
    try:
        with p.oneshot():
            handles = p.num_handles() if hasattr(p, "num_handles") else p.num_fds()
            t = p.cpu_times()
            return {"t": time.monotonic(), "rss": p.memory_info().rss, "handles": handles,
                    "threads": p.num_threads(), "cpu": t.user + t.system}
    except psutil.Error:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wavr", required=True)
    ap.add_argument("--minutes", type=float, default=10)
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="wavr-soak-"))
    certs = {"good": make_cert(tmp, "good"), "bad": make_cert(tmp, "bad")}
    port = free_port()
    core = FakeCore(port, certs)
    core.start("good")
    state = tmp / "node.json"
    state.write_text(json.dumps({"url": f"https://127.0.0.1:{port}", "node_id": "soak",
                                 "token": "soak-credential", "pin": certs["good"][2],
                                 "state": "active", "seq": 0, "press_count": 0}))
    frames = tmp / "frames.txt"
    cases = json.loads((REPO / "conformance" / "ld2450_framing.json").read_text())["cases"]
    frames.write_text("\n".join(f for c in cases for f in c["frames_hex"]) + "\n")
    total_s = a.minutes * 60
    proc = psutil.Popen([os.path.abspath(a.wavr), "node", "run", "--sensor", f"replay:{frames}",
                         "--state", str(state), "--telemetry-ms", "100", "--heartbeat-ms", "500",
                         "--disabled-heartbeat-ms", "500", "--seconds", str(total_s + 60)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    samples, start, phase_log = [], time.monotonic(), []
    # The node's stderr, timestamped as it arrives: a node that goes quiet is
    # a node that is stuck, and the timeline says where.
    err_lines: list[tuple[float, str]] = []

    def pump():
        for raw in proc.stderr:
            err_lines.append((time.monotonic() - start, raw.decode(errors="replace").rstrip()))

    threading.Thread(target=pump, daemon=True).start()
    cycle = 0
    try:
        while time.monotonic() - start < total_s and proc.poll() is None:
            for name, secs in PHASES:
                if time.monotonic() - start >= total_s or proc.poll() is not None:
                    break
                core.phase = name
                if name == "down":
                    core.stop()
                elif name == "wrongcert":
                    core.stop()
                    core.start("bad")
                elif core.cert != "good":
                    core.stop()
                    core.start("good")
                before = dict(core.counts)
                t_phase, n_err = time.monotonic() - start, len(err_lines)
                end = time.monotonic() + secs
                while time.monotonic() < end and proc.poll() is None:
                    s = sample(proc)
                    if s:
                        samples.append(s)
                    time.sleep(1)
                phase_log.append({
                    "t": round(t_phase, 1), "phase": name,
                    "requests": sum(v - before.get(k, 0) for k, v in core.counts.items()),
                    "node_stderr_lines": len(err_lines) - n_err})
            cycle += 1
        alive_through_chaos = proc.poll() is None
        # Revocation ends it: exit 3, state erased.
        if core.cert != "good":
            core.stop()
            core.start("good")
        core.phase = "revoked"
        try:
            code = proc.wait(timeout=30)
        except psutil.TimeoutExpired:
            proc.kill()
            code = None
    finally:
        core.stop()
    time.sleep(0.5)
    stderr = "\n".join(f"{t:7.1f}s {line}" for t, line in err_lines)

    def q(key, first):
        body = samples[len(samples) // 10:]           # drop warm-up
        k = max(1, len(body) // 4)
        part = body[:k] if first else body[-k:]
        return statistics.median(x[key] for x in part) if part else None

    rss0, rss1 = q("rss", True), q("rss", False)
    h0, h1 = q("handles", True), q("handles", False)
    th0, th1 = q("threads", True), q("threads", False)
    cpu_s = samples[-1]["cpu"] - samples[0]["cpu"] if len(samples) > 1 else None
    wall = samples[-1]["t"] - samples[0]["t"] if len(samples) > 1 else None
    growth_ok = (rss0 is not None and rss1 <= rss0 * 1.2 + 1_048_576 and h1 <= h0 + 16 and th1 <= th0 + 2)
    report = {
        "minutes": a.minutes, "cycles": cycle, "phases_run": len(phase_log),
        "alive_through_chaos": alive_through_chaos,
        "revoked_exit_code": code, "state_erased": not state.exists(),
        "requests": dict(sorted(core.counts.items())),
        "violations": core.violations[:20], "violation_count": len(core.violations),
        "rss_mb": [round(rss0 / 2**20, 2), round(rss1 / 2**20, 2)] if rss0 else None,
        "handles": [h0, h1], "threads": [th0, th1],
        "cpu_pct_of_one_core": round(100 * cpu_s / wall, 2) if wall else None,
        "no_unbounded_growth": growth_ok,
        "stderr_lines": len(stderr.splitlines()),
        "stderr_tail": stderr.splitlines()[-5:],
        "timeline": phase_log,
    }
    report["passed"] = bool(alive_through_chaos and code == 3 and report["state_erased"]
                            and not core.violations and growth_ok)
    print(json.dumps(report, indent=1))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
