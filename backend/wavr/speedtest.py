"""Internet speed test (A3.3) — THE single sanctioned external egress.

This is the ONE Wavr feature that deliberately reaches OUTSIDE the LAN, so it is
treated exactly like the AI narrator (Wavr's other sanctioned egress) but with
one extra gate because the M-Lab / NDT7 provider PUBLISHES the caller's public
IP. Three gates, enforced at the route (app.py):
  1. WAVR_NET_SPEEDTEST (speedtest_enabled(), default OFF) -> route 503s.
  2. Provider factor: the IP-publishing NDT7/M-Lab path is only reachable when
     WAVR_SPEEDTEST_PROVIDER=ndt7 (default `cloudflare`). The single
     WAVR_NET_SPEEDTEST flag alone can never trigger IP publication.
  3. Per-invocation confirm=true in the POST body -> 409 without it.

DISCLOSURE (surfaced by the route + describe()):
  * cloudflare: contacts speed.cloudflare.com; your public IP is visible to
    Cloudflare for the duration of the test; nothing is stored publicly.
  * ndt7: M-Lab PERMANENTLY PUBLISHES your public IP + the test timestamp into a
    public open-data set that anyone can download; this is inherent to how M-Lab
    works and cannot be turned off.

LICENSE: the NDT7 client here is an inline stdlib implementation of the PUBLIC,
vendor-neutral ndt7 protocol (Apache-2.0 reference clients). NO proprietary tools
proprietary asset (e.g. ndt7-client.exe) is vendored. In tests the locate + WS
transports are injected, so CI makes zero real M-Lab contact.

NOTE / LIMITATION: the NDT7 path implemented here measures DOWNLOAD throughput +
latency only (a correct minimal ndt7 download client); upload_mbps is reported
as null for ndt7. The Cloudflare path measures both (netutils.internet_health).
Numbers are always measured, never faked.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import time
from typing import Callable
from urllib.parse import urlsplit

from wavr import netutils

_LOCATE_URL = "https://locate.measurementlab.net/v2/nearest/ndt/ndt7"
_WS_SUBPROTOCOL = "net.measurementlab.ndt.v7"
_VALID_PROVIDERS = ("cloudflare", "ndt7")


def speedtest_enabled() -> bool:
    """True only if WAVR_NET_SPEEDTEST is explicitly enabled. OFF by default."""
    return netutils.speedtest_enabled()


def speedtest_provider() -> str:
    """The configured provider, `cloudflare` (default, lower-disclosure) or
    `ndt7` (M-Lab, publishes the public IP). Unknown values fall back to the
    safe default so the IP-publishing path is never reached by a typo."""
    p = os.getenv("WAVR_SPEEDTEST_PROVIDER", "cloudflare").strip().lower()
    return p if p in _VALID_PROVIDERS else "cloudflare"


def describe(provider: str) -> str:
    """Plain-language egress disclosure for the given provider (returned in the
    API response so the frontend modal and any API caller see exactly what
    leaves the box)."""
    if provider == "ndt7":
        return ("Contacts M-Lab (measurementlab.net). M-Lab PERMANENTLY PUBLISHES "
                "your public IP address and the test timestamp in a public "
                "open-data set that anyone can download; this cannot be turned off.")
    return ("Contacts speed.cloudflare.com. Your public IP is visible to "
            "Cloudflare for the duration of the test; nothing is stored publicly.")


# ---------------------------------------------------------------------------
# NDT7 / M-Lab client (inline, stdlib-only, injectable transports)
# ---------------------------------------------------------------------------
def _default_locate(timeout: float = 10.0) -> dict:
    """GET the M-Lab Locate API v2 to find a nearby ndt7 server. THIS request
    reveals the box's public IP to M-Lab (see module disclosure)."""
    import urllib.request  # stdlib, lazy
    req = urllib.request.Request(_LOCATE_URL, headers={"User-Agent": "wavr-speedtest"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def pick_download_url(locate: dict) -> "tuple[str, str] | None":
    """Extract (machine, download_wss_url) from a Locate API response, or None.
    Defensive: any unexpected shape -> None (never raises)."""
    try:
        for result in locate.get("results", []):
            urls = result.get("urls", {})
            for key, url in urls.items():
                if "/ndt/v7/download" in key and str(url).startswith("ws"):
                    return result.get("machine", ""), url
    except Exception:
        return None
    return None


def _default_ndt7_download(url: str, duration: float = 10.0) -> "float | None":
    """Minimal ndt7 download: a stdlib WebSocket-over-TLS client that counts the
    application bytes the server streams over `duration` seconds and returns
    Mbps. Returns None on any failure. No third-party WS dependency."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if not host:
            return None
        port = parts.port or (443 if parts.scheme in ("wss", "https") else 80)
        path = parts.path + (("?" + parts.query) if parts.query else "")

        raw = socket.create_connection((host, port), timeout=duration)
        try:
            if parts.scheme in ("wss", "https"):
                ctx = ssl.create_default_context()
                sock = ctx.wrap_socket(raw, server_hostname=host)
            else:
                sock = raw
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            handshake = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                f"Sec-WebSocket-Protocol: {_WS_SUBPROTOCOL}\r\n\r\n"
            )
            sock.sendall(handshake.encode("ascii"))
            if not _read_handshake(sock, duration):
                return None
            total = _count_ws_bytes(sock, duration)
        finally:
            try:
                raw.close()
            except OSError:
                pass
    except (OSError, ssl.SSLError):
        return None
    if not total:
        return None
    total_bytes, elapsed = total
    if elapsed <= 0 or total_bytes <= 0:
        return None
    return (total_bytes * 8) / elapsed / 1e6


def _read_handshake(sock, timeout: float) -> bool:
    sock.settimeout(timeout)
    buf = b""
    while b"\r\n\r\n" not in buf and len(buf) < 8192:
        chunk = sock.recv(1024)
        if not chunk:
            return False
        buf += chunk
    return buf.startswith(b"HTTP/1.1 101") or b" 101 " in buf.split(b"\r\n", 1)[0]


def _count_ws_bytes(sock, duration: float) -> "tuple[int, float] | None":
    """Read WebSocket frames (server->client, unmasked) counting application
    payload bytes for up to `duration` seconds. Returns (bytes, elapsed)."""
    start = time.perf_counter()
    total = 0
    sock.settimeout(duration)
    while (time.perf_counter() - start) < duration:
        header = _recv_exact(sock, 2)
        if header is None:
            break
        b0, b1 = header[0], header[1]
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        length = b1 & 0x7F
        if length == 126:
            ext = _recv_exact(sock, 2)
            if ext is None:
                break
            length = struct.unpack(">H", ext)[0]
        elif length == 127:
            ext = _recv_exact(sock, 8)
            if ext is None:
                break
            length = struct.unpack(">Q", ext)[0]
        if masked:
            if _recv_exact(sock, 4) is None:
                break
        payload = _recv_exact(sock, length) if length else b""
        if payload is None:
            break
        if opcode == 0x8:  # close
            break
        total += len(payload)
    return total, time.perf_counter() - start


def _recv_exact(sock, n: int) -> "bytes | None":
    if n <= 0:
        return b""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _run_ndt7(locate_fn: "Callable[[], dict] | None" = None,
              download_fn: "Callable[[str], float | None] | None" = None,
              latency_fn: "Callable[[], float | None] | None" = None) -> dict:
    locate_fn = locate_fn or _default_locate
    try:
        locate = locate_fn()
    except Exception:
        locate = {}
    server = pick_download_url(locate) if isinstance(locate, dict) else None
    if not server:
        return _ndt7_result(None, None, None)
    machine, url = server
    download_fn = download_fn or _default_ndt7_download
    try:
        down = download_fn(url)
    except Exception:
        down = None
    lat = (latency_fn or netutils._default_latency)()
    return _ndt7_result(machine or None, lat, down)


def _ndt7_result(server, latency_ms, download_mbps) -> dict:
    return {
        "provider": "ndt7",
        "server": server,
        "latency_ms": latency_ms,
        "download_mbps": download_mbps,
        "upload_mbps": None,  # LIMITATION: ndt7 upload not implemented this pass
        "disclosed_egress": True,
    }


def run_speedtest(provider: "str | None" = None, *,
                  latency_fn=None, download_fn=None, upload_fn=None,
                  locate_fn=None, ndt7_download_fn=None) -> dict:
    """Run the speed test for `provider` (defaults to speedtest_provider()).

    cloudflare -> netutils.internet_health (latency + down + up).
    ndt7 -> inline M-Lab download client (latency + down; up = null).
    All transports are injectable so tests never touch the real internet."""
    provider = (provider or speedtest_provider())
    if provider == "ndt7":
        return _run_ndt7(locate_fn=locate_fn, download_fn=ndt7_download_fn,
                         latency_fn=latency_fn)
    res = netutils.internet_health(latency_fn=latency_fn, download_fn=download_fn,
                                   upload_fn=upload_fn)
    return {"provider": "cloudflare", "server": None, "disclosed_egress": True, **res}
