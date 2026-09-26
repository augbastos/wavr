"""Package built native runtimes into release-shaped archives -- locally, never published.

    python scripts/package_native.py --out DIST  TARGET=BUILD_DIR [TARGET=BUILD_DIR ...]

    e.g. x86_64-linux-musl=build/x86_64-linux-musl  x86_64-windows-mingw=build/native

Each archive `wavr-native-<version>-<target>.(tar.gz|zip)` holds the `wavr`
executable, the shared library when the build has one, the C header, the
licence, third-party notices, `manifest.json` (version, ABI, file hashes, the
exact build command) and `sbom.cdx.json` (CycloneDX 1.5). A `SHA256SUMS` file
covers every archive, and `release-manifest.json` says for each target what
docs/platform-matrix.json can show was run there -- hardware tested, runtime
tested, simulation tested, compile only or unverified.

Before anything is archived, every file is scanned for things that must never
ship: build-machine paths, a user name, private keys, credential-shaped
strings. A hit aborts the whole run. Nothing here uploads anything.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import io
import json
import re
import sys
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEPS = [
    {"name": "mbedtls", "version": "3.6.7", "license": "Apache-2.0",
     "purl": "pkg:github/Mbed-TLS/mbedtls@mbedtls-3.6.7",
     "url": "https://github.com/Mbed-TLS/mbedtls/releases/tag/mbedtls-3.6.7"},
    {"name": "nlohmann-json", "version": "3.12.0", "license": "MIT",
     "purl": "pkg:github/nlohmann/json@v3.12.0",
     "url": "https://github.com/nlohmann/json/releases/tag/v3.12.0"},
]
RUNTIMES = {
    "musl": {"name": "musl", "license": "MIT", "purl": "pkg:generic/musl"},
    "mingw": {"name": "mingw-w64-runtime", "license": "ZPL-2.1 AND MIT AND BSD-3-Clause",
              "purl": "pkg:generic/mingw-w64"},
    "bionic": {"name": "android-ndk-libc++", "license": "Apache-2.0 WITH LLVM-exception",
               "purl": "pkg:generic/android-ndk@r30"},
    "apple": {"name": "apple-libc", "license": "system", "purl": "pkg:generic/apple-sdk"},
}
FORBIDDEN = [
    (re.compile(r"[A-Za-z]:[\\/](Users|Workspace)[\\/]", re.I), "a Windows build path"),
    (re.compile(r"/(home|Users)/[A-Za-z_][A-Za-z0-9._-]*/"), "a home directory path"),
    # Whoever runs the packager: their account name has no business in a binary.
    # ponytail: generic CI/container accounts are skipped (mbedTLS says "root CA");
    # widen the list if a builder runs under another shared name.
    *([(re.compile(re.escape(_user), re.I), "the build machine's user name")]
      if len(_user := getpass.getuser()) >= 3 and _user.lower() not in {"root", "runner", "user", "admin", "builder"}
      else []),
    # A key is the header plus a body: mbedTLS itself carries the bare header
    # strings as parser constants. Optional RFC 1421 headers (encrypted keys).
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----\r?\n(?:[A-Za-z-]+:[^\r\n]*\r?\n)*\r?\n?"
                r"[A-Za-z0-9+/=]{40,}"), "a private key"),
    (re.compile(r"""["']?(api[_-]?key|secret|password|token)["']?\s*[=:]\s*["'][^"']{8,}""", re.I),
     "a credential-shaped assignment"),
]


def runtime_for(target: str) -> dict:
    if "musl" in target:
        return RUNTIMES["musl"]
    if "windows" in target or "mingw" in target:
        return RUNTIMES["mingw"]
    if "android" in target:
        return RUNTIMES["bionic"]
    return RUNTIMES["apple"]


def scan(name: str, data: bytes) -> list[str]:
    # The same patterns over each way text sits in a binary: ASCII/UTF-8, and
    # UTF-16 (Windows wide strings), little- and big-endian.
    views = [data.decode("latin-1"), data.decode("utf-16-le", "ignore"),
             data.decode("utf-16-be", "ignore")]
    return [f"{name}: {why}" for rx, why in FORBIDDEN if any(rx.search(v) for v in views)]


def notices(build: Path) -> bytes:
    parts = ["Third-party notices for the Wavr native runtime.\n",
             "It is linked statically with the components below; their licences follow.\n"]
    mbed = build / "_deps" / "mbedtls-src" / "LICENSE"
    parts.append("\n==== mbedTLS 3.6.7 (used under Apache-2.0) ====\n")
    parts.append(mbed.read_text(encoding="utf-8") if mbed.exists() else "(LICENSE not found in build)\n")
    js = build / "_deps" / "nlohmann_json" / "include" / "nlohmann" / "json.hpp"
    parts.append("\n==== nlohmann/json 3.12.0 (MIT) ====\n")
    if js.exists():
        head = js.read_text(encoding="utf-8", errors="replace")[:3000]
        m = re.search(r"SPDX-License-Identifier: MIT.*?\n", head)
        parts.append("Copyright (c) 2013-2025 Niels Lohmann. SPDX-License-Identifier: MIT\n"
                     "Permission is hereby granted, free of charge, to any person obtaining a copy of "
                     "this software... (full MIT text: https://opensource.org/license/mit)\n"
                     if m else head)
    parts.append("\n==== C/C++ runtime ====\nSee the toolchain's licence for the runtime named in "
                 "manifest.json (musl: MIT; mingw-w64 runtime: ZPL/MIT/BSD; NDK libc++: "
                 "Apache-2.0 WITH LLVM-exception).\n")
    return "".join(parts).encode()


def sbom(target: str, version: str, files: dict) -> dict:
    rt = runtime_for(target)
    comps = [{"type": "library", "name": d["name"], "version": d["version"],
              "licenses": [{"license": {"id": d["license"]}}], "purl": d["purl"],
              "externalReferences": [{"type": "distribution", "url": d["url"]}]} for d in DEPS]
    comps.append({"type": "library", "name": rt["name"],
                  "licenses": [{"expression": rt["license"]}], "purl": rt["purl"]})
    return {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
            "metadata": {"component": {"type": "application", "name": "wavr-native",
                                       "version": version, "licenses": [{"license": {"id": "AGPL-3.0-or-later"}}],
                                       "hashes": [{"alg": "SHA-256", "content": h} for h in files.values()]}},
            "components": comps}


# Package target -> the row of docs/platform-matrix.json that records its evidence.
MATRIX_IDS = {
    "x86_64-windows-gnu": "windows-x64", "aarch64-windows-gnu": "windows-arm64",
    "x86_64-linux-musl": "linux-x64", "x86-linux-musl": "linux-x86",
    "aarch64-linux-musl": "linux-arm64", "arm-linux-musleabihf": "linux-armv7",
    "mipsel-linux-musleabi": "openwrt-mipsel", "mips-linux-musleabi": "openwrt-mips",
    "aarch64-macos": "macos-arm64", "x86_64-macos": "macos-x64",
    "android-arm64-v8a": "android-arm64", "android-armeabi-v7a": "android-armv7",
    "android-x86_64": "android-x86_64",
}


def evidence(entry: dict | None) -> str:
    """The strongest thing the matrix can show for a runtime, in words a release
    note can repeat. A cross-compile is never more than "compile only"."""
    rt = (entry or {}).get("native_runtime") or {}
    hw = rt.get("hardware_tested")
    if hw is True:
        return "hardware tested"
    if hw == "partial":
        return "hardware tested (partial)"
    if rt.get("simulation_tested") is True:
        return "simulation tested"
    if rt.get("unit_tested") is True:
        return "runtime tested"
    if rt.get("compiles") is True:
        return "compile only"
    return "unverified"


def release_manifest(version: str, c_abi: str | None, made: list[Path]) -> dict:
    matrix = json.loads((REPO / "docs" / "platform-matrix.json").read_text(encoding="utf-8"))
    rows = {t["id"]: t for t in matrix["targets"]}
    targets = []
    for arc in made:
        target = arc.name[len(f"wavr-native-{version}-"):].rsplit(".tar.gz", 1)[0].rsplit(".zip", 1)[0]
        mid = MATRIX_IDS.get(target)
        entry = rows.get(mid)
        targets.append({"target": target, "archive": arc.name,
                        "sha256": hashlib.sha256(arc.read_bytes()).hexdigest(),
                        "platform_matrix_id": mid, "evidence": evidence(entry),
                        "how": ((entry or {}).get("native_runtime") or {}).get("how")})
    # No "published" flag: this file is attached to a release as it is, so it must not
    # state its own publication (this script never uploads anything).
    return {"schema": 1, "product": "wavr-native", "version": version, "c_abi": c_abi,
            "evidence_source": "docs/platform-matrix.json",
            "note": "Evidence is what was run, not what is supported: compile only is never support.",
            "targets": targets}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("builds", nargs="+", help="TARGET=BUILD_DIR")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    version = (REPO / "VERSION").read_text().strip()
    header = (REPO / "native" / "include" / "wavr" / "wavr.h").read_bytes()
    abi = re.search(rb"WAVR_ABI_VERSION_MAJOR (\d+).*?WAVR_ABI_VERSION_MINOR (\d+)", header, re.S)
    problems, sums, made, ready = [], [], [], []
    for spec in a.builds:
        if "=" not in spec:
            print(f"REFUSED: {spec!r} is not TARGET=BUILD_DIR")
            return 2
    for spec in a.builds:
        target, build = spec.split("=", 1)
        build = Path(build)
        payload: dict[str, bytes] = {}
        for name in ("wavr", "wavr.exe", "libwavr_native.so", "libwavr_native.dll",
                     "libwavr_native.dylib", "libwavr_native_jni.so"):
            p = build / name
            if p.exists():
                payload[f"bin/{name}"] = p.read_bytes()
        if not any(k.startswith("bin/wavr") for k in payload):
            problems.append(f"{target}: no wavr executable in {build}")
            continue
        payload["include/wavr/wavr.h"] = header
        payload["LICENSE"] = (REPO / "LICENSE").read_bytes()
        payload["THIRD-PARTY-NOTICES.txt"] = notices(build)
        hashes = {k: hashlib.sha256(v).hexdigest() for k, v in payload.items()}
        manifest = {
            "name": "wavr-native", "version": version, "target": target,
            "c_abi": f"{abi.group(1).decode()}.{abi.group(2).decode()}" if abi else None,
            "runtime": runtime_for(target)["name"],
            "files": hashes,
            "build": "cmake -S native -B <dir> -G Ninja -DCMAKE_BUILD_TYPE=Release"
                     + ("" if "mingw" in target else
                        f" -DCMAKE_TOOLCHAIN_FILE=<zig or NDK toolchain> (target {target})"),
            "packaged_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "published": False,
        }
        payload["manifest.json"] = json.dumps(manifest, indent=1).encode()
        payload["sbom.cdx.json"] = json.dumps(sbom(target, version, hashes), indent=1).encode()
        for name, data in payload.items():
            if name == "LICENSE":
                continue   # the AGPL text itself is not scanned for credential shapes
            problems += scan(f"{target}/{name}", data)
        ready.append((target, payload))
    if problems:   # nothing has been written yet, for any target
        for p in problems:
            print("REFUSED:", p)
        return 1
    for target, payload in ready:
        base = f"wavr-native-{version}-{target}"
        arc = out / (base + (".zip" if "windows" in target or "mingw" in target else ".tar.gz"))
        if arc.suffix == ".zip":
            with zipfile.ZipFile(arc, "w", zipfile.ZIP_DEFLATED) as z:
                for name, data in sorted(payload.items()):
                    z.writestr(f"{base}/{name}", data)
        else:
            with tarfile.open(arc, "w:gz") as t:
                for name, data in sorted(payload.items()):
                    info = tarfile.TarInfo(f"{base}/{name}")
                    info.size, info.mode, info.mtime = len(data), 0o755 if name.startswith("bin/") else 0o644, 0
                    t.addfile(info, io.BytesIO(data))
        made.append(arc)
    for arc in made:
        sums.append(f"{hashlib.sha256(arc.read_bytes()).hexdigest()}  {arc.name}")
    # LF even on Windows: `sha256sum -c` reads a CR as part of the file name.
    (out / "SHA256SUMS").write_bytes(("\n".join(sums) + "\n").encode())
    c_abi = f"{abi.group(1).decode()}.{abi.group(2).decode()}" if abi else None
    (out / "release-manifest.json").write_bytes(
        json.dumps(release_manifest(version, c_abi, made), indent=1).encode())
    print("\n".join(f"{a.name}  {a.stat().st_size} bytes" for a in made))
    return 0


if __name__ == "__main__":
    sys.exit(main())
