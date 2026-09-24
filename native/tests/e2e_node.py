"""The native `wavr` binary against a real Core, end to end.

    python native/tests/e2e_node.py --wavr build/native/wavr[.exe]
    python native/tests/e2e_node.py --wavr build/aarch64-linux-musl/wavr --adb SERIAL

Starts a multidevice Core on loopback with nodes enabled, in a throwaway
directory (its own database, certificate and port -- nothing of an existing
install is read or touched), then drives the native binary through the Node
Protocol exactly as a device would, checking every step on the Core's side:

  status         native and Python print the same state and exit code
  enroll         the node pins the certificate the Core really presents
  telemetry      replayed LD2450 frames reach fusion as mmWave in the node's room
  wrong pin      the node refuses to talk, and the Core sees nothing from it
  disable        the node goes quiet (no telemetry accepted) but keeps its state
  reactivate     the node-initiated way back, and data flows again
  revoke         the node wipes its state and exits 3

With --adb the binary runs on an Android device instead, from a scratch
directory under /data/local/tmp that is removed afterwards, and reaches the
Core through `adb reverse` on one port -- so the Core never leaves this
machine's loopback. Only that one port mapping is removed at the end.

Run with the interpreter of a dev install (it needs `cryptography` for the
Core's TLS). Exit 0 = every check passed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE          # loopback, a Core of our own making
failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def api(base: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json",
                                          "X-Wavr-Local": "1"})
    try:
        with urllib.request.urlopen(req, context=_CTX, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


def fingerprint(port: int) -> str:
    pem = ssl.get_server_certificate(("127.0.0.1", port))
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, 64, 2))


class Here:
    """The binary runs on this machine."""

    def __init__(self, wavr: str, tmp: Path):
        self.wavr, self.dir = wavr, tmp

    def path(self, name: str) -> str:
        return str(self.dir / name)

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([self.wavr, *args], capture_output=True, text=True, timeout=60)

    def read(self, name: str) -> str | None:
        p = self.dir / name
        return p.read_text() if p.exists() else None

    def write(self, name: str, text: str) -> None:
        (self.dir / name).write_text(text)

    def close(self) -> None:
        pass


class Adb(Here):
    """The binary runs on an Android device, reaching the Core via adb reverse."""

    DIR = "/data/local/tmp/wavr-e2e"

    def __init__(self, serial: str, wavr: str, port: int):
        self.adb, self.port = ["adb", "-s", serial], port
        self._sh(f"rm -rf {self.DIR} && mkdir -p {self.DIR}")
        self._adb("push", wavr, f"{self.DIR}/wavr")
        self._sh(f"chmod 755 {self.DIR}/wavr")
        self._adb("reverse", f"tcp:{port}", f"tcp:{port}")

    def _adb(self, *args: str) -> None:
        subprocess.run([*self.adb, *args], check=True, capture_output=True, timeout=120)

    def _sh(self, cmd: str) -> subprocess.CompletedProcess:
        return subprocess.run([*self.adb, "shell", cmd], capture_output=True, text=True,
                              timeout=60)

    def path(self, name: str) -> str:
        return f"{self.DIR}/{name}"

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return self._sh(" ".join([self.path("wavr"), *map(shlex.quote, args)]))

    def read(self, name: str) -> str | None:
        r = self._sh(f"cat {self.path(name)}")
        return r.stdout if r.returncode == 0 else None

    def write(self, name: str, text: str) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(text)
        self._adb("push", f.name, self.path(name))
        os.unlink(f.name)

    def close(self) -> None:
        subprocess.run([*self.adb, "reverse", "--remove", f"tcp:{self.port}"],
                       capture_output=True, timeout=60)
        self._sh(f"rm -rf {self.DIR}")


def the_node(base: str, node_id: str) -> dict:
    return next((n for n in api(base, "GET", "/api/nodes")[1].get("nodes", [])
                 if n["node_id"] == node_id), {})


def office_mmwave(base: str) -> bool:
    _, state = api(base, "GET", "/api/state")
    rooms = state.get("rooms", state) if isinstance(state, dict) else {}
    text = json.dumps(rooms.get("office", {}) if isinstance(rooms, dict) else rooms)
    return "mmwave" in text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wavr", required=True, help="the native binary under test")
    ap.add_argument("--adb", metavar="SERIAL", help="run the binary on this Android device")
    a = ap.parse_args()
    wavr = os.path.abspath(a.wavr)

    tmp = Path(tempfile.mkdtemp(prefix="wavr-native-e2e-"))
    port = free_port()
    base = f"https://127.0.0.1:{port}"
    env = {k: v for k, v in os.environ.items() if not k.startswith("WAVR_")}
    env.update(WAVR_MULTIDEVICE="1", WAVR_NODES_ENABLED="1", WAVR_BIND="127.0.0.1",
               WAVR_PORT=str(port), WAVR_DB=str(tmp / "wavr.db"),
               WAVR_HOUSE_MAP=str(tmp / "house.json"), WAVR_TLS_CERT=str(tmp / "cert.pem"),
               WAVR_TLS_KEY=str(tmp / "key.pem"), WAVR_FRONTEND=str(REPO / "frontend"),
               PYTHONPATH=str(REPO / "backend"), PYTHON_DOTENV_DISABLED="1")
    log = open(tmp / "core.log", "w", encoding="utf-8")
    core = subprocess.Popen([sys.executable, "-m", "wavr.serve"], env=env, cwd=tmp,
                            stdout=log, stderr=subprocess.STDOUT)
    dev: Here = Here(wavr, tmp)
    try:
        if a.adb:
            dev = Adb(a.adb, wavr, port)
        for _ in range(120):
            try:
                if api(base, "GET", "/api/runtime")[0] == 200:
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.5)
        else:
            print(f"the Core did not come up; see {tmp / 'core.log'}")
            return 2

        # -- status: same answer from both implementations -----------------------
        py = subprocess.run([sys.executable, "-m", "wavr.status", "--url", base, "--json"],
                            env=env, capture_output=True, text=True)
        nat = dev.run("status", "--url", base, "--json")
        check(py.returncode == nat.returncode, f"status exit code (py {py.returncode}, "
                                               f"native {nat.returncode})")
        pj, nj = json.loads(py.stdout), json.loads(nat.stdout)
        check(pj["runtime"].get("state") == nj["runtime"].get("state")
              and pj["runtime"].get("headline") == nj["runtime"].get("headline"),
              "status state and headline")

        # -- enroll -----------------------------------------------------------------
        code, minted = api(base, "POST", "/api/nodes/enroll-code",
                           {"name": "native-e2e", "sensor_type": "ld2450", "room": "office"})
        check(code == 200 and "code" in minted, f"mint enrolment code (HTTP {code})")
        state = dev.path("node.json")
        r = dev.run("node", "enroll", "--url", base, "--code", minted.get("code", ""),
                    "--state", state)
        check(r.returncode == 0, f"enroll exits 0 ({r.returncode}: {r.stderr.strip()})")
        saved = json.loads(dev.read("node.json") or "{}")
        check(saved.get("pin") == fingerprint(port), "pinned the certificate the Core presents")
        node_id = saved.get("node_id", "")
        check(the_node(base, node_id).get("state") == "active", "Core lists the node active")

        # -- telemetry ------------------------------------------------------------
        cases = json.loads((REPO / "conformance" / "ld2450_framing.json").read_text())["cases"]
        frames = [f for c in cases for f in c["frames_hex"]]
        dev.write("frames.txt", "\n".join(frames) + "\n")
        run = ("node", "run", "--sensor", "replay:" + dev.path("frames.txt"), "--state", state)
        check(not office_mmwave(base), "control: no mmWave in the room before the node speaks")
        enrolled_seen = the_node(base, node_id).get("last_seen_ts")
        r = dev.run(*run, "--seconds", "4")
        check(r.returncode == 0, f"run exits 0 ({r.returncode}: {r.stderr.strip()})")
        seen = the_node(base, node_id).get("last_seen_ts")
        check(bool(seen) and seen != enrolled_seen, "Core recorded the node's telemetry")
        check(office_mmwave(base), "fusion has mmWave evidence in the node's room")

        # -- wrong pin: the node must refuse before sending its token -------------
        good = dev.read("node.json") or "{}"
        bad = json.loads(good)
        bad["pin"] = "00:" * 31 + "00"
        dev.write("node.json", json.dumps(bad))
        r = dev.run(*run, "--seconds", "3")
        check("does not match this node's pin" in r.stderr, "a different certificate is refused")
        check(the_node(base, node_id).get("last_seen_ts") == seen,
              "nothing reached the Core past a wrong pin")
        dev.write("node.json", json.dumps({**bad, "pin": json.loads(good).get("pin")}))

        # -- disable -> quiet ------------------------------------------------------
        check(api(base, "POST", f"/api/nodes/{node_id}/disable")[0] == 200, "disable")
        r = dev.run(*run, "--seconds", "3")
        check(json.loads(dev.read("node.json") or "{}").get("state") == "disabled",
              "the node records that it was disabled")
        check("disabled" in r.stderr, "and says so")

        # -- reactivate ----------------------------------------------------------
        r = dev.run("node", "reactivate", "--state", state)
        check(r.returncode == 0 and "Reactivated" in r.stdout,
              f"reactivate ({r.returncode}: {r.stderr.strip()})")
        check(the_node(base, node_id).get("state") == "active", "Core has it active again")
        before = the_node(base, node_id).get("last_seen_ts")
        time.sleep(1.1)
        dev.run(*run, "--seconds", "3")
        check(the_node(base, node_id).get("last_seen_ts") != before, "data flows again")

        # -- revoke -> wiped, exit 3 --------------------------------------------
        check(api(base, "DELETE", f"/api/nodes/{node_id}")[0] == 200, "revoke")
        r = dev.run(*run, "--seconds", "5")
        check(r.returncode == 3, f"a revoked node exits 3 ({r.returncode})")
        check(dev.read("node.json") is None, "and erases its state file")
    finally:
        dev.close()
        core.terminate()                      # our own child, by handle
        try:
            core.wait(timeout=15)
        except subprocess.TimeoutExpired:
            core.kill()
        log.close()

    print(f"\n{'FAILED' if failures else 'passed'}; Core log: {tmp / 'core.log'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
