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
    return subprocess.run(["adb", "-s", serial, *args], capture_output=True, text=text,
                          timeout=timeout)


def ui(serial) -> str:
    adb(serial, "shell", "uiautomator", "dump", "/sdcard/wavr-ui.xml")
    return adb(serial, "shell", "cat", "/sdcard/wavr-ui.xml").stdout


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
    env.update(WAVR_PORT=str(port), WAVR_DB=str(tmp / "w.db"), WAVR_HOUSE_MAP=str(tmp / "h.json"),
               WAVR_FRONTEND=str(REPO / "frontend"), PYTHONPATH=str(REPO / "backend"),
               PYTHON_DOTENV_DISABLED="1")
    log = open(tmp / "core.log", "w")
    core = subprocess.Popen([sys.executable, "-m", "wavr.serve"], env=env, cwd=tmp,
                            stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"

    def api(method, path, body=None):
        req = urllib.request.Request(base + path, method=method, headers={
            "X-Wavr-Local": "1", "Content-Type": "application/json"},
            data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read() or b"{}")

    try:
        for _ in range(120):
            try:
                api("GET", "/api/runtime")
                break
            except OSError:
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
