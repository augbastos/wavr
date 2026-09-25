"""The native command contract against a real Core, across the network.

    python native/tests/e2e_commands.py --wavr build/native/wavr[.exe]

Starts a throwaway multidevice Core (its own database and certificate, a free
port) listening on this machine's LAN address as well as loopback, then plays
two parts with the native `wavr` binary:

  the Core's own screen   loopback, root: approves, promotes, revokes;
  a new device            the LAN address: probes the certificate, asks to
                          join, waits for approval, then acts with the token
                          it was given -- over TLS pinned to what it probed.

Every step is checked on the Core's side, and every refusal the Core owns
(a 'user' may not switch Watch, a device may not approve pairings or sensor
Nodes, a revoked token reads nothing) is checked to hold through the native
client: the command layer shapes calls, it never widens what a caller may do.

Needs a LAN address in a private range (the Core admits same-/24 peers only)
and an interpreter with the Core's dependencies (`cryptography` for TLS).
Exit 0 = every check passed, 2 = could not run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
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
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def lan_ip() -> str | None:
    # The address this machine would use to reach the LAN; nothing is sent.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.168.0.1", 9))
            ip = s.getsockname()[0]
        except OSError:
            return None
    return ip if ip.startswith(("192.168.", "10.", "172.")) else None


def fingerprint(host: str, port: int) -> str:
    pem = ssl.get_server_certificate((host, port))
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, 64, 2))


def raw(base: str, method: str, path: str, body=None, headers=None) -> int:
    """A plain request with exactly the headers given -- to prove what a caller
    WITHOUT the dashboard's CSRF header gets."""
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, context=_CTX, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


class Wavr:
    def __init__(self, exe: str):
        self.exe = exe

    def cmd(self, url: str, name: str, args: dict | None = None, token: str = "",
            pin: str = "") -> tuple[int, dict]:
        argv = [self.exe, "command", name, "--url", url, "--reveal"]
        if args is not None:
            argv += ["--args", json.dumps(args)]
        if token:
            argv += ["--token", token]
        if pin:
            argv += ["--pin", pin]
        p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        try:
            return p.returncode, json.loads(p.stdout)
        except ValueError:
            return p.returncode, {"_stdout": p.stdout, "_stderr": p.stderr}

    def run(self, *args: str) -> tuple[int, dict]:
        p = subprocess.run([self.exe, *args], capture_output=True, text=True, timeout=60)
        try:
            return p.returncode, json.loads(p.stdout)
        except ValueError:
            return p.returncode, {"_stdout": p.stdout, "_stderr": p.stderr}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wavr", required=True, help="the native binary under test")
    a = ap.parse_args()
    w = Wavr(os.path.abspath(a.wavr))
    ip = lan_ip()
    if not ip:
        print("no private LAN address on this machine: cannot test the LAN path")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="wavr-native-cmd-"))
    port = free_port()
    core_url = f"https://127.0.0.1:{port}"     # the Core's own screen (root)
    lan_url = f"https://{ip}:{port}"          # a device on the LAN
    env = {k: v for k, v in os.environ.items() if not k.startswith("WAVR_")}
    env.update(WAVR_MULTIDEVICE="1", WAVR_NODES_ENABLED="1", WAVR_BIND="0.0.0.0",
               WAVR_PORT=str(port), WAVR_DB=str(tmp / "wavr.db"),
               WAVR_HOUSE_MAP=str(tmp / "house.json"), WAVR_TLS_CERT=str(tmp / "cert.pem"),
               WAVR_TLS_KEY=str(tmp / "key.pem"), WAVR_FRONTEND=str(REPO / "frontend"),
               PYTHONPATH=str(REPO / "backend"), PYTHON_DOTENV_DISABLED="1")
    log = open(tmp / "core.log", "w", encoding="utf-8")
    core = subprocess.Popen([sys.executable, "-m", "wavr.serve"], env=env, cwd=tmp,
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            try:
                if raw(core_url, "GET", "/api/runtime", headers={"X-Wavr-Local": "1"}) == 200:
                    break
            except OSError:   # not listening yet
                pass
            time.sleep(0.5)
        else:
            print(f"the Core did not come up; see {tmp / 'core.log'}")
            return 2

        # -- the device meets the Core ---------------------------------------------
        code, probe = w.run("probe", "--url", lan_url)
        fp = fingerprint(ip, port)
        check(code == 0 and probe.get("fingerprint") == fp,
              "probe reports the certificate the Core really presents")
        code, req = w.cmd(lan_url, "pair.request", {"requester_name": "native e2e",
                                                     "platform": "test", "reported_fp": fp},
                          pin=fp)
        data = req.get("data") or {}
        check(code == 0 and data.get("cert_fingerprint") == fp and data.get("compare_code"),
              f"pair.request over a pinned connection ({req.get('error')} {req.get('detail')})")
        rid, compare = data.get("request_id", ""), data.get("compare_code", "")

        # -- the Core's screen sees it, and a wrong code is refused ----------------
        code, pending = w.cmd(core_url, "pairings.list")
        ids = json.dumps(pending.get("data"))
        check(code == 0 and rid in ids, "pairings.list shows the waiting device")
        code, wrong = w.cmd(core_url, "pairings.approve",
                            {"id": rid, "role": "user", "confirm_code": "000000" if compare != "000000" else "111111"})
        check(code == 1 and not wrong.get("ok"), f"a wrong compare code is refused ({wrong.get('error')})")
        code, ok = w.cmd(core_url, "pairings.approve",
                         {"id": rid, "role": "user", "confirm_code": compare})
        check(code == 0 and ok.get("ok"), f"pairings.approve with the right code ({ok.get('detail')})")

        # -- the device collects its token --------------------------------------------
        code, st = w.cmd(lan_url, "pair.status", {"request_id": rid}, pin=fp)
        sd = st.get("data") or {}
        token, device_id = sd.get("token", ""), sd.get("device_id", "")
        check(code == 0 and sd.get("status") == "approved" and token and device_id,
              "pair.status hands over the token once approved")
        code, hidden = w.run("command", "pair.status", "--url", lan_url, "--pin", fp,
                             "--args", json.dumps({"request_id": rid}))
        check("token" not in json.dumps(hidden) or '"***"' in json.dumps(hidden),
              "the CLI does not print a token without --reveal")

        # -- reading across the LAN: Bearer + pin ---------------------------------------
        code, snap = w.run("snapshot", "--url", lan_url, "--token", token, "--pin", fp)
        check(snap.get("reachable") is True and (snap.get("runtime") or {}).get("state"),
              "snapshot over the LAN with the device token (Bearer) reads the runtime")
        code, anon = w.run("snapshot", "--url", lan_url, "--pin", fp)
        check((anon.get("runtime") or None) is None, "without a token the LAN reads nothing")
        code, badpin = w.cmd(lan_url, "sources.list", token=token, pin="00:" * 31 + "00")
        check(code == 2 and badpin.get("error") == "unreachable",
              "a wrong pin stops the request before the token is sent")

        # -- what a 'user' may not do, it may not do through the native client ----------
        code, denied = w.cmd(lan_url, "watch.set", {"on": True}, token=token, pin=fp)
        check(code == 1 and denied.get("error") == "forbidden", "a 'user' may not switch Watch")
        code, admin = w.cmd(lan_url, "pairings.list", token=token, pin=fp)
        check(code == 1 and admin.get("error") == "forbidden",
              "a LAN device may not list pairings (loopback-root only)")

        # -- promoted to central on the Core's screen --------------------------------------
        code, promo = w.cmd(core_url, "device.role", {"id": device_id, "role": "central"})
        check(code == 0, f"device.role central on the Core ({promo.get('detail')})")
        code, on = w.cmd(lan_url, "watch.set", {"on": True}, token=token, pin=fp)
        check(code == 0, f"a 'central' switches Watch on ({on.get('detail')})")
        code, back = w.cmd(lan_url, "watch.set", {"on": False}, token=token, pin=fp)
        check(code == 0, "and off again")
        code, srcs = w.cmd(lan_url, "sources.list", token=token, pin=fp)
        names = []
        d = srcs.get("data")
        if isinstance(d, dict):
            for k in ("sources", "providers"):
                v = d.get(k)
                if isinstance(v, dict):
                    names = list(v)
                elif isinstance(v, list):
                    names = [x.get("name") for x in v if isinstance(x, dict) and x.get("name")]
                if names:
                    break
        check(code == 0, "sources.list as central")
        if names:
            code, off = w.cmd(lan_url, "source.set", {"name": names[0], "enabled": False},
                              token=token, pin=fp)
            check(code == 0, f"source.set turns {names[0]!r} off ({off.get('detail')})")
            w.cmd(lan_url, "source.set", {"name": names[0], "enabled": True}, token=token, pin=fp)
        code, nope = w.cmd(lan_url, "source.set", {"name": "../../api/block", "enabled": False},
                           token=token, pin=fp)
        check(not nope.get("ok") and nope.get("error") in ("not_found", "invalid", "refused"),
              f"a path-shaped source name stays one segment ({nope.get('error')})")

        # -- sensor Nodes: approving one into the house is the Core's screen only -----------
        # A Node asks to join (unauthenticated, in-subnet), then the device tries to
        # let it in: refused even as 'central', and refused on loopback without the
        # dashboard's CSRF header.
        nreq = urllib.request.Request(
            lan_url + "/api/nodes/request", method="POST",
            data=json.dumps({"name_hint": "radar", "sensor_type": "ld2450",
                             "cert_fingerprint": fp}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(nreq, context=_CTX, timeout=10) as r:
                node_id = json.loads(r.read()).get("node_id", "")
        except urllib.error.HTTPError as e:
            node_id = ""
            print(f"      (node request refused: HTTP {e.code})")
        check(bool(node_id), "a Node can ask to join")
        if node_id:
            approve = {"id": node_id, "name": "radar", "sensor_type": "ld2450", "room": "office"}
            code, lan_ok = w.cmd(lan_url, "node.approve", approve, token=token, pin=fp)
            check(code == 1 and lan_ok.get("error") == "forbidden",
                  f"a LAN device may not approve a sensor Node ({lan_ok.get('error')})")
            code, lan_pend = w.cmd(lan_url, "nodes.pending", token=token, pin=fp)
            check(code == 1 and lan_pend.get("error") == "forbidden",
                  "a LAN device may not list pending Nodes")
            status = raw(core_url, "POST", f"/api/nodes/{node_id}/approve",
                         {"name": "radar", "sensor_type": "ld2450", "room": "office"})
            check(status == 403, f"loopback without the CSRF header may not approve ({status})")
            code, root_ok = w.cmd(core_url, "node.approve", approve)
            check(code == 0, f"the Core's screen approves the Node ({root_ok.get('detail')})")

        # -- revoked: the token reads nothing -------------------------------------------------
        code, rv = w.cmd(core_url, "device.revoke", {"id": device_id})
        check(code == 0, "device.revoke on the Core")
        code, after = w.cmd(lan_url, "sources.list", token=token, pin=fp)
        check(code == 1 and after.get("error") == "forbidden", "a revoked token is refused")
    finally:
        core.terminate()
        try:
            core.wait(timeout=15)
        except subprocess.TimeoutExpired:
            core.kill()
        log.close()

    print(("FAILED: " + "; ".join(failures)) if failures else
          f"passed; Core log: {tmp / 'core.log'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
