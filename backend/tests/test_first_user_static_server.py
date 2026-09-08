"""`scripts/first_user_static_server.py`: the download server, attacked.

The mandate this exists for is explicit that "the button has an href" is
not a test here. This starts the REAL server, on a real socket, on
`127.0.0.1` with an OS-assigned port, against a small SANDBOXED fixture
tree (never the real `site/public/` or the real 100+ MB
`_local/first-user-rc/`), and then does what a hostile or merely careless
request would do to it:

  * download an installer and a checksum it byte-for-byte against the
    manifest's own sha256 and Content-Length;
  * ask for the numbered internal filename instead of the human one;
  * ask for `../`, a percent-encoded `../`, an absolute path, and a file
    that exists on disk but was never listed in the manifest;
  * ask for a file outside the sandbox via a symlink, when this OS/user
    allows creating one at all.

Every fixture lives under `tmp_path`; the real repo is never touched.
"""
from __future__ import annotations

import hashlib
import http.client
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import first_user_static_server as server_mod  # noqa: E402
import gen_releases_manifest as genmod  # noqa: E402


@pytest.fixture()
def sandbox(tmp_path: Path):
    """A miniature `site/public/` + `_local/first-user-rc/` pair.

    Also a `secret_outside/` directory next to both, standing in for "the
    rest of the repository" -- `.env`, the database, anything that must
    never be reachable through either allowlist.
    """
    static_root = tmp_path / "site_public"
    static_root.mkdir()
    (static_root / "index.html").write_text(
        "<html><body>Wavr install</body></html>", encoding="utf-8")
    (static_root / "assets").mkdir()
    (static_root / "assets" / "wavr.css").write_text("body{}", encoding="utf-8")

    rc_dir = tmp_path / "first-user-rc"
    rc_dir.mkdir()
    exe_bytes = b"pretend windows installer bytes" * 5000
    apk_bytes = b"pretend android apk bytes" * 4000
    (rc_dir / "Wavr-Setup-Windows.exe").write_bytes(exe_bytes)
    (rc_dir / "2-wavr-app-for-phone-and-tablet.apk").write_bytes(apk_bytes)

    outside = tmp_path / "secret_outside"
    outside.mkdir()
    (outside / "wavr.db").write_bytes(b"this must never be servable")
    (outside / ".env").write_text("SECRET=doNotServe", encoding="utf-8")

    manifest, warnings = genmod.build_manifest(rc_dir=rc_dir)
    assert warnings == []

    return {
        "static_root": static_root,
        "rc_dir": rc_dir,
        "outside": outside,
        "manifest": manifest,
        "exe_bytes": exe_bytes,
        "apk_bytes": apk_bytes,
    }


@pytest.fixture()
def running_server(sandbox):
    httpd = server_mod.build_server(
        "127.0.0.1", 0,
        static_root=sandbox["static_root"],
        rc_dir=sandbox["rc_dir"],
        manifest=sandbox["manifest"])
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    base = f"http://{host}:{port}"
    try:
        yield base, sandbox
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(url: str):
    return urllib.request.urlopen(url, timeout=10)


def test_windows_installer_download_matches_manifest_bytes_and_hash(running_server):
    base, sandbox = running_server
    resp = _get(f"{base}/download/Wavr-Setup-Windows.exe")
    body = resp.read()

    assert resp.status == 200
    assert body == sandbox["exe_bytes"]
    assert hashlib.sha256(body).hexdigest() == \
        _entry(sandbox["manifest"], "windows-installer-exe")["sha256"]
    assert int(resp.headers["Content-Length"]) == len(sandbox["exe_bytes"])
    assert 'filename="Wavr-Setup-Windows.exe"' in resp.headers["Content-Disposition"]


def test_android_apk_download_matches_manifest_bytes_and_hash(running_server):
    base, sandbox = running_server
    resp = _get(f"{base}/download/Wavr-Android.apk")
    body = resp.read()

    assert resp.status == 200
    assert body == sandbox["apk_bytes"]
    assert hashlib.sha256(body).hexdigest() == \
        _entry(sandbox["manifest"], "android-companion")["sha256"]
    assert int(resp.headers["Content-Length"]) == len(sandbox["apk_bytes"])
    assert 'filename="Wavr-Android.apk"' in resp.headers["Content-Disposition"]


def test_head_reports_the_same_content_length_as_get(running_server):
    base, _sandbox = running_server
    conn = http.client.HTTPConnection(base.replace("http://", ""), timeout=10)
    try:
        conn.request("HEAD", "/download/Wavr-Android.apk")
        resp = conn.getresponse()
        head_length = resp.getheader("Content-Length")
        resp.read()
    finally:
        conn.close()

    get_resp = _get(f"{base}/download/Wavr-Android.apk")
    get_length = get_resp.headers["Content-Length"]
    get_resp.read()

    assert head_length == get_length


def test_numbered_internal_filename_is_not_a_valid_download_name(running_server):
    """The human name is the ONLY door -- the build's own numbering must
    never be a route a user (or a scanner) can hit directly."""
    base, _sandbox = running_server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(f"{base}/download/2-wavr-app-for-phone-and-tablet.apk")
    assert excinfo.value.code == 404


def test_checksum_and_leia_me_files_are_not_downloadable(running_server):
    """Everything in `_local/first-user-rc/` that is not in the manifest's
    download map must be unreachable, even though it sits right next to
    files that ARE servable."""
    base, sandbox = running_server
    (sandbox["rc_dir"] / "SHA256SUMS.txt").write_text("irrelevant", encoding="utf-8")
    for name in ("SHA256SUMS.txt", "LEIA-ME.md", "SHA256SUMS-completo.txt"):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _get(f"{base}/download/{name}")
        assert excinfo.value.code == 404


def test_static_site_is_served(running_server):
    base, _sandbox = running_server
    resp = _get(f"{base}/index.html")
    assert resp.status == 200
    assert b"Wavr install" in resp.read()

    resp = _get(f"{base}/")
    assert resp.status == 200
    assert b"Wavr install" in resp.read()

    resp = _get(f"{base}/assets/wavr.css")
    assert resp.status == 200


@pytest.mark.parametrize("attack_path", [
    "/../secret_outside/wavr.db",
    "/../secret_outside/.env",
    "/%2e%2e/secret_outside/wavr.db",
    "/assets/%2e%2e%2fwavr.db",
    "/..%2f..%2fsecret_outside%2fwavr.db",
])
def test_path_traversal_against_the_static_root_is_refused(running_server, attack_path):
    base, _sandbox = running_server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(f"{base}{attack_path}")
    assert excinfo.value.code in (404, 400)


def test_absolute_windows_path_in_request_is_refused(running_server):
    base, sandbox = running_server
    absolute = str(sandbox["outside"] / "wavr.db")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(f"{base}/{absolute}")
    assert excinfo.value.code in (404, 400)


def test_unc_style_path_is_refused(running_server):
    """A request-target starting with `//` is authority-form under RFC 3986
    (`urlsplit("//host/path")` reads `host` as netloc, not as a directory
    named "host") -- so `urlsplit(self.path).path` inside the handler comes
    out as `/wavr.db`, which the sandbox's static root does not contain.
    Checked directly against the status code: `http.client`, unlike
    `urllib.request`, does not raise for a non-2xx response."""
    base, _sandbox = running_server
    conn = http.client.HTTPConnection(base.replace("http://", ""), timeout=10)
    try:
        conn.request("GET", r"//secret_outside/wavr.db")
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 404
    finally:
        conn.close()


def test_dotfile_and_off_allowlist_extensions_are_refused(running_server, sandbox):
    """`_STATIC_SUFFIXES` is a second gate beyond "resolves inside the
    folder": drop a `.env`-shaped and a `.md`-shaped file INSIDE the static
    root itself and confirm neither is servable just because it is there."""
    base, sb = running_server
    (sb["static_root"] / ".env").write_text("SECRET=leak", encoding="utf-8")
    (sb["static_root"] / "notes.md").write_text("internal notes", encoding="utf-8")

    for name in (".env", "notes.md"):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _get(f"{base}/{name}")
        assert excinfo.value.code == 404


def test_symlink_escape_is_refused_when_this_os_permits_creating_one(running_server, sandbox):
    """Windows without Developer Mode/admin refuses to create a symlink at
    all (`WinError 1314`) -- verified on this machine during development.
    Skip rather than fail when the environment itself blocks the setup;
    this is a property of the server, and it is exercised for real
    wherever symlink creation is actually possible (Linux CI, Developer
    Mode, admin)."""
    target = sandbox["outside"] / "wavr.db"
    link = sandbox["static_root"] / "escape.html"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"cannot create a symlink on this system: {exc}")

    base, _sandbox = running_server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(f"{base}/escape.html")
    assert excinfo.value.code == 404


def test_downloads_map_only_contains_available_products(sandbox):
    """A product the manifest marks unavailable must never appear in the
    download map, even if a stray file with a matching name exists."""
    manifest = sandbox["manifest"]
    downloads = server_mod.build_download_map(manifest, rc_dir=sandbox["rc_dir"])
    available_names = {e["downloadName"] for e in manifest["products"] if e["available"]}
    assert set(downloads.keys()) == available_names
    assert "Wavr-Kiosk-Android.apk" not in downloads  # not built in this sandbox


def _entry(manifest: dict, product_id: str) -> dict:
    for e in manifest["products"]:
        if e["id"] == product_id:
            return e
    raise AssertionError(product_id)
