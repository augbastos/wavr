"""A minimal, allowlisted static + download server for the first-user test.

## Why this exists

Phones and tablets on the home Wi-Fi need to reach the Wavr install site and
download the actual installers from the machine running this, without anyone
typing an address or a port. This is that server: stdlib
only (`http.server`), no new framework, bound to one address for the
duration of one test session.

## Why not `SimpleHTTPRequestHandler` pointed at a directory

"Serve this directory" is exactly the shape of bug this file exists to rule
out. There are two explicit allowlists, built once, and nothing outside
them is ever opened:

  1. `STATIC_ROOT` (`site/public/`) -- a request resolves to a real file
     strictly inside this directory, with a suffix the site is actually
     built from, or it is refused. Decoding (`%2e%2e` etc.) happens BEFORE
     the containment check, and containment is checked against the fully
     RESOLVED path -- so a literal `..`, a percent-encoded `..`, an
     absolute path, a UNC share, or a symlink (leaf or ancestor) pointing
     outside the root are all refused the same way, because after
     `Path.resolve()` they all collapse to a real path either inside or
     outside `STATIC_ROOT`, and only "inside" is ever served.
  2. The download map, built from `releases.json` -- so `/download/<name>`
     can only ever open one of the exact files that manifest currently
     lists as available. `_local/first-user-rc/` also holds
     `SHA256SUMS*.txt` and `LEIA-ME.md`; neither is reachable through this
     route, because neither is in the map.

## Transport honesty

This is plain HTTP. It exists to hand a phone on the same Wi-Fi a public,
unsigned installer -- not a secret -- so there is nothing here claiming
encryption. Do not read this as a statement about Wavr's own local API,
which is TLS-backed elsewhere in this repo; this portal and that API are
different servers with different jobs.

    python scripts/first_user_static_server.py            # binds the LAN IP
    python scripts/first_user_static_server.py 127.0.0.1  # binds one address
"""
from __future__ import annotations

import mimetypes
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = (ROOT / "site" / "public").resolve()
RC_DIR = (ROOT / "_local" / "first-user-rc").resolve()
DEFAULT_PORT = 8791
DOWNLOAD_PREFIX = "/download/"

# Extensions the site is actually built from today. A file that resolves
# inside STATIC_ROOT but is not one of these (a stray .md, a future `.env`
# dropped next to the HTML by accident) is still refused: "resolves inside
# the folder" is a weaker guarantee than "is part of the site".
_STATIC_SUFFIXES = {".html", ".css", ".js", ".json", ".svg", ".png",
                     ".ico", ".webmanifest", ".txt"}


def _safe_static_path(root: Path, url_path: str) -> Path | None:
    """Resolve `url_path` under `root`, or None if it must be refused."""
    decoded = unquote(url_path)
    if "\x00" in decoded or ":" in decoded or decoded.startswith("\\\\"):
        return None  # NUL, a drive letter / ADS marker, or a UNC share
    decoded = decoded.lstrip("/\\") or "index.html"
    candidate = (root / decoded).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None  # resolved OUTSIDE root -- the traversal this exists to stop
    if not candidate.is_file():
        return None
    if candidate.suffix.lower() not in _STATIC_SUFFIXES:
        return None
    return candidate


def build_download_map(manifest: dict, rc_dir: Path = RC_DIR) -> dict[str, Path]:
    """`{human download name: real file path}`, only for what is available now.

    Every entry is re-verified against `rc_dir` here, independent of
    whatever `releases.json` claims -- a manifest is data on disk and this
    function does not trust it any further than a static file is trusted
    for `_safe_static_path` above.
    """
    rc_dir = rc_dir.resolve()
    out: dict[str, Path] = {}
    for entry in manifest.get("products", []):
        if not entry.get("available"):
            continue
        internal = entry.get("internalFilename")
        name = entry.get("downloadName")
        if not internal or not name:
            continue
        real = (rc_dir / internal).resolve()
        try:
            real.relative_to(rc_dir)
        except ValueError:
            continue  # a manifest entry cannot point outside its own rc_dir
        if real.is_file():
            out[name] = real
    return out


def make_handler(static_root: Path, downloads: dict[str, Path],
                  rc_dir: Path) -> type[BaseHTTPRequestHandler]:
    """Build a handler class closed over this server's own allowlists.

    A class (not an instance) because `http.server` instantiates the
    handler once per connection; the allowlists are the same for every
    connection this process serves, so they are class attributes computed
    once here rather than rebuilt per request.
    """

    class Handler(BaseHTTPRequestHandler):
        server_version = "WavrFirstUserPortal/1"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:  # noqa: A002
            pass  # the portal prints its own summary; keep the console quiet

        def do_GET(self) -> None:
            self._handle(send_body=True)

        def do_HEAD(self) -> None:
            self._handle(send_body=False)

        def _handle(self, send_body: bool) -> None:
            path = urlsplit(self.path).path
            if path.startswith(DOWNLOAD_PREFIX):
                self._serve_download(path[len(DOWNLOAD_PREFIX):], send_body)
            else:
                self._serve_static(path, send_body)

        def _serve_download(self, name: str, send_body: bool) -> None:
            name = unquote(name)
            real = downloads.get(name)
            if real is None:
                self._not_found()
                return
            # Re-checked on every request: an artifact can be rebuilt or
            # removed between server start and this request, and a stale
            # handle must 404, not silently serve whatever is now there.
            try:
                resolved = real.resolve()
                resolved.relative_to(rc_dir)
                if not resolved.is_file():
                    raise FileNotFoundError(resolved)
            except (FileNotFoundError, ValueError, OSError):
                self._not_found()
                return
            size = resolved.stat().st_size
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                              f'attachment; filename="{name}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if send_body:
                with resolved.open("rb") as handle:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)

        def _serve_static(self, path: str, send_body: bool) -> None:
            candidate = _safe_static_path(static_root, path)
            if candidate is None:
                self._not_found()
                return
            size = candidate.stat().st_size
            ctype = mimetypes.guess_type(candidate.name)[0] or "text/plain"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if send_body:
                self.wfile.write(candidate.read_bytes())

        def _not_found(self) -> None:
            body = b"Not found."
            self.send_response(HTTPStatus.NOT_FOUND)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

    return Handler


def build_server(host: str, port: int, *, static_root: Path = STATIC_ROOT,
                  rc_dir: Path = RC_DIR, manifest: dict | None = None
                  ) -> ThreadingHTTPServer:
    """Build (but do not start) the server bound to `(host, port)`.

    `port=0` asks the OS for a free ephemeral port -- what tests use, so
    parallel test runs never collide on a fixed port. The real caller
    (`first_user_portal.py`) always passes a real port and falls back to 0
    only if that one is already taken.
    """
    if manifest is None:
        import gen_releases_manifest  # local import: optional dependency
        manifest, _ = gen_releases_manifest.build_manifest(rc_dir=rc_dir)
    downloads = build_download_map(manifest, rc_dir=rc_dir)
    handler = make_handler(static_root.resolve(), downloads, rc_dir.resolve())
    server = ThreadingHTTPServer((host, port), handler)
    # A per-request thread must never outlive a shutdown: without this, a
    # client that opens a connection and goes silent (a phone that locks
    # its screen mid-download) can keep this process alive after Ctrl+C.
    server.daemon_threads = True
    return server


def main(argv: list[str]) -> int:
    import gen_releases_manifest

    host = argv[1] if len(argv) > 1 else "127.0.0.1"
    manifest, warnings = gen_releases_manifest.build_manifest()
    for warning in warnings:
        print(f"WARNING: {warning}")
    httpd = build_server(host, DEFAULT_PORT, manifest=manifest)
    bound_host, bound_port = httpd.server_address[:2]
    print(f"serving http://{bound_host}:{bound_port}/  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
