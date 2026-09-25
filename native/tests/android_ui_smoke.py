"""UI smoke test of the native Android surfaces against a real Core.

    python native/tests/android_ui_smoke.py --serial emulator-5554 \
        --apk core-launcher/app/build/outputs/apk/debug/app-debug.apk --out shots/

For an EMULATOR (or a device you own and may install on). Starts a loopback
Core on this machine with the simulated house on, maps the device's
127.0.0.1:8000 to it with `adb reverse`, installs the debug APK, and checks
what the phone and TV activities actually render -- through the accessibility
tree (`uiautomator dump`), not pixels:

  * phone: the Space's runtime state is shown, the Rooms tab lists the rooms
    the Core reports, nothing says "native runtime not bundled";
  * TV: the status renders and D-pad input moves focus.

Screenshots are saved to --out for a person to look at. Removes its reverse
mapping afterwards. Exit 0 = every check passed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import ssl
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PKG = "dev.wavr.core"
failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("ok   " if ok else "FAIL ") + what, flush=True)
    if not ok:
        failures.append(what)


def adb(serial, *args, text=True, timeout=120):
    # adb prints UTF-8; on Windows the default decoder is the ANSI code page.
    kw = {"encoding": "utf-8", "errors": "replace"} if text else {}
    return subprocess.run(["adb", "-s", serial, *args], capture_output=True, text=text,
                          timeout=timeout, **kw)


def ui(serial) -> str:
    for _ in range(3):
        adb(serial, "shell", "uiautomator", "dump", "/sdcard/wavr-ui.xml")
        xml = adb(serial, "shell", "cat", "/sdcard/wavr-ui.xml").stdout
        # A slow emulator raises "System UI isn't responding" over whatever is on
        # screen; it is not the app under test. Answer "Wait" and look again.
        if "isn&apos;t responding" not in xml and "isn't responding" not in xml:
            return xml
        tap_text(serial, xml, "Wait")
        time.sleep(3)
    return xml


def texts(xml: str) -> list[str]:
    return [t for t in re.findall(r'text="([^"]*)"', xml) if t] + \
           [t for t in re.findall(r'content-desc="([^"]*)"', xml) if t]


def tap_text(serial, xml: str, label: str) -> bool:
    for node in re.findall(r"<node [^>]*>", xml):
        if f'text="{label}"' in node or f'content-desc="{label}"' in node:
            m = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
            if m:
                x1, y1, x2, y2 = map(int, m.groups())
                adb(serial, "shell", "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))
                return True
    return False


def node_of(xml: str, prefix: str):
    """Bounds of the first node whose text starts with `prefix`."""
    for node in re.findall(r"<node [^>]*>", xml):
        m = re.search(r'text="([^"]*)"', node)
        b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
        if m and b and m.group(1).startswith(prefix):
            return tuple(map(int, b.groups()))
    return None


def find(serial, prefix: str, tries: int = 10):
    """Scroll down until a node starting with `prefix` is on screen."""
    for _ in range(tries):
        xml = ui(serial)
        b = node_of(xml, prefix)
        if b:
            return xml, b
        adb(serial, "shell", "input", "swipe", "500", "1500", "500", "700", "400")
        time.sleep(1.5)
    return ui(serial), None


def tap(serial, x: int, y: int) -> None:
    adb(serial, "shell", "input", "tap", str(x), str(y))


def shot(serial, out: Path, name: str) -> None:
    png = adb(serial, "exec-out", "screencap", "-p", text=False).stdout
    (out / f"{name}.png").write_bytes(png)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", required=True)
    ap.add_argument("--apk", required=True)
    ap.add_argument("--out", default="android-ui-shots")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    tmp = Path(tempfile.mkdtemp(prefix="wavr-ui-"))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith("WAVR_")}
    # LAN access on: the Core then serves HTTPS on loopback too, which is what a
    # phone that IS the Core sees -- and pairing approvals exist only in this mode.
    env.update(WAVR_PORT=str(port), WAVR_DB=str(tmp / "w.db"), WAVR_HOUSE_MAP=str(tmp / "h.json"),
               WAVR_FRONTEND=str(REPO / "frontend"), PYTHONPATH=str(REPO / "backend"),
               PYTHON_DOTENV_DISABLED="1", WAVR_MULTIDEVICE="1", WAVR_BIND="127.0.0.1",
               WAVR_TLS_CERT=str(tmp / "cert.pem"), WAVR_TLS_KEY=str(tmp / "key.pem"))
    log = open(tmp / "core.log", "w")
    core = subprocess.Popen([sys.executable, "-m", "wavr.serve"], env=env, cwd=tmp,
                            stdout=log, stderr=subprocess.STDOUT)
    base = f"https://127.0.0.1:{port}"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE      # loopback, a Core of our own making

    def api(method, path, body=None):
        req = urllib.request.Request(base + path, method=method, headers={
            "X-Wavr-Local": "1", "Content-Type": "application/json"},
            data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req, timeout=5, context=ctx) as r:
            return json.loads(r.read() or b"{}")

    try:
        for _ in range(120):
            try:
                api("GET", "/api/runtime")
                break
            except (OSError, ssl.SSLError):
                time.sleep(0.5)
        api("POST", "/api/sources/sim/toggle", {"enabled": True})
        rooms: list[str] = []
        for _ in range(60):
            rooms = sorted(api("GET", "/api/state"))
            if rooms:
                break
            time.sleep(1)
        check(bool(rooms), f"the Core reports rooms from the simulator ({len(rooms)})")
        runtime = api("GET", "/api/runtime")

        adb(a.serial, "reverse", "tcp:8000", f"tcp:{port}")
        r = adb(a.serial, "install", "-r", "-t", a.apk, timeout=300)
        check("Success" in r.stdout, "APK installs")
        if a.serial.startswith("emulator-"):
            # A previous run may have left the app joined to a Core that no longer
            # exists. Only on an emulator: on a real device this would wipe the
            # owner's own app data.
            adb(a.serial, "shell", "pm", "clear", PKG)

        # -- phone ------------------------------------------------------------
        adb(a.serial, "shell", "am", "start", "-W", "-n", f"{PKG}/.nativeui.NativeStatusActivity")
        time.sleep(12)          # two polls of the 5 s view model
        xml = ui(a.serial)
        shot(a.serial, out, "phone-status")
        t = " | ".join(texts(xml))
        check("not bundled" not in t.lower(), "the native runtime is loaded (JNI library present)")
        check(runtime["state"].lower() in t.lower(), f"phone shows the runtime state '{runtime['state']}'")
        if tap_text(a.serial, xml, "Rooms"):
            time.sleep(3)
            xml = ui(a.serial)
            shot(a.serial, out, "phone-rooms")
            t = " | ".join(texts(xml))
            shown = [r for r in rooms if r in t]
            check(len(shown) >= 1, f"Rooms tab lists the Core's rooms ({len(shown)}/{len(rooms)} visible)")
        else:
            check(False, "a Rooms tab to tap")

        # -- phone: Manage (the write side, through the command contract) -------
        width = int(re.search(r"(\d+)x\d+", adb(a.serial, "shell", "wm", "size").stdout).group(1))
        joiner = api("POST", "/api/pair-request", {"requester_name": "smoke-phone", "platform": "test"})
        xml = ui(a.serial)
        check(tap_text(a.serial, xml, "Manage"), "a Manage tab to tap")
        time.sleep(6)
        xml, watch = find(a.serial, "Watch (")
        shot(a.serial, out, "phone-manage")
        check(watch is not None, "Manage shows the Watch switch")
        if watch:
            tap(a.serial, width - 90, (watch[1] + watch[3]) // 2)
            for _ in range(10):
                time.sleep(1)
                if api("GET", "/api/watch").get("on") is True:
                    break
            check(api("GET", "/api/watch").get("on") is True, "tapping Watch turns it on in the Core")
            api("POST", "/api/watch", {"on": False})
        xml, who = find(a.serial, "smoke-phone")
        check(who is not None, "Manage lists the device asking to join")
        xml, field = find(a.serial, "Code shown on that device")
        if field:
            tap(a.serial, (field[0] + field[2]) // 2, (field[1] + field[3]) // 2)
            time.sleep(1)
            adb(a.serial, "shell", "input", "text", joiner["compare_code"])
            adb(a.serial, "shell", "input", "keyevent", "KEYCODE_BACK")   # close the keyboard
            time.sleep(1)
            xml, go = find(a.serial, "Let it in", tries=4)
            shot(a.serial, out, "phone-approve")
            if go:
                tap(a.serial, (go[0] + go[2]) // 2, (go[1] + go[3]) // 2)
            status = {}
            for _ in range(10):
                time.sleep(1)
                status = api("POST", "/api/pair-request/status", {"request_id": joiner["request_id"]})
                if status.get("status") != "pending":
                    break
            check(status.get("status") == "approved",
                  f"typing the device's code and 'Let it in' approves it ({status.get('status')})")
        else:
            check(False, "a code field for the pending device")
        xml, doc = find(a.serial, "Run the Core's checks")
        if doc:
            tap(a.serial, (doc[0] + doc[2]) // 2, (doc[1] + doc[3]) // 2)
            time.sleep(10)
            xml, _ = find(a.serial, "Run the Core's checks", tries=1)
            adb(a.serial, "shell", "input", "swipe", "500", "1500", "500", "700", "400")
            time.sleep(1.5)
            xml = ui(a.serial)
            shot(a.serial, out, "phone-doctor")
            t = " | ".join(texts(xml))
            ids = [c.get("id") for c in api("GET", "/api/health/doctor").get("checks", [])]
            shown = [i for i in ids if i and i in t]
            check(bool(shown) and "Not allowed" not in t,
                  f"the Core's checks are listed ({len(shown)} of {len(ids)} ids on screen)")
        else:
            check(False, "a diagnosis button")

        # -- phone: joining a Space (probe -> ask -> code -> approved -> token) --
        devices_before = len(api("GET", "/api/devices").get("devices", []))
        for _ in range(8):   # back to the top of Manage, where Join is
            adb(a.serial, "shell", "input", "swipe", "500", "700", "500", "1600", "300")
        time.sleep(1)
        xml, field = find(a.serial, "Core address")
        if field:
            tap(a.serial, (field[0] + field[2]) // 2, (field[1] + field[3]) // 2)
            time.sleep(1)
            adb(a.serial, "shell", "input", "keyevent", "KEYCODE_MOVE_END")
            adb(a.serial, "shell", "input", "text", "127.0.0.1:8000")
            adb(a.serial, "shell", "input", "keyevent", "KEYCODE_BACK")
            time.sleep(1)
        xml, chk = find(a.serial, "Check its certificate")
        if chk:
            tap(a.serial, (chk[0] + chk[2]) // 2, (chk[1] + chk[3]) // 2)
            time.sleep(4)
        xml, ask = find(a.serial, "They match: ask to join")
        check(ask is not None, "the certificate is shown for comparison before joining")
        code = None
        if ask:
            tap(a.serial, (ask[0] + ask[2]) // 2, (ask[1] + ask[3]) // 2)
            for _ in range(8):
                time.sleep(1.5)
                m = re.search(r'content-desc="Compare code (\d{6})"', ui(a.serial))
                if m:
                    code = m.group(1)
                    break
                # The code renders below the certificate: bring it on screen.
                adb(a.serial, "shell", "input", "swipe", "500", "1500", "500", "900", "300")
        shot(a.serial, out, "phone-join-code")
        check(code is not None, "the app shows a compare code for the person at the Core")
        if code:
            pending = [r for r in api("GET", "/api/pending-pairings").get("requests", [])
                       if r.get("platform") == "android"]
            check(len(pending) == 1 and pending[0].get("compare_code") == code,
                  "the code on the phone is the Core's own compare code")
            if pending:
                api("POST", f"/api/pending-pairings/{pending[0]['request_id']}/approve",
                    {"role": "user", "confirm_code": code})
            joined = None
            for _ in range(10):
                time.sleep(2)
                xml, joined = find(a.serial, "Certificate pin", tries=2)
                if joined:
                    break
            shot(a.serial, out, "phone-joined")
            check(joined is not None, "once approved the app holds a pinned connection")
            devices = api("GET", "/api/devices").get("devices", [])
            check(len(devices) == devices_before + 1, "the Core issued this device one credential")
            xml, forget = find(a.serial, "Forget this Core")
            if forget:   # leave the emulator as it was found
                tap(a.serial, (forget[0] + forget[2]) // 2, (forget[1] + forget[3]) // 2)
                time.sleep(2)

        # -- TV ---------------------------------------------------------------
        adb(a.serial, "shell", "am", "start", "-W", "-n", f"{PKG}/.nativeui.TvStatusActivity")
        time.sleep(10)
        xml = ui(a.serial)
        shot(a.serial, out, "tv-status")
        t = " | ".join(texts(xml))
        check(runtime["state"].lower() in t.lower() or "wavr" in t.lower(), "TV activity renders the status")
        focused_before = re.findall(r'focused="true"[^>]*bounds="([^"]+)"', xml)
        adb(a.serial, "shell", "input", "keyevent", "KEYCODE_DPAD_RIGHT")
        adb(a.serial, "shell", "input", "keyevent", "KEYCODE_DPAD_DOWN")
        time.sleep(2)
        xml2 = ui(a.serial)
        shot(a.serial, out, "tv-after-dpad")
        focused_after = re.findall(r'focused="true"[^>]*bounds="([^"]+)"', xml2)
        check(bool(focused_after) and focused_after != focused_before, "D-pad moves focus on TV")
    finally:
        adb(a.serial, "reverse", "--remove", "tcp:8000")
        core.terminate()
        try:
            core.wait(15)
        except subprocess.TimeoutExpired:
            core.kill()
        log.close()
    print(f"\n{'FAILED' if failures else 'passed'}; screenshots in {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
