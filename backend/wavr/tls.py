"""Local TLS for multi-device LAN access (ADR-0006 §6, Phase 2).

Closes audit finding H2: with `WAVR_MULTIDEVICE` on, tokens / pairing tickets /
the RoomState stream were plaintext on the Wi-Fi. `ensure_cert` gives the central
a self-signed HTTPS/WSS certificate so those secrets are no longer sniffable on
the LAN.

Two paths, decided by the arguments:

  * User-provided cert — if both `cert_path` and `key_path` are given AND both
    files already exist, they are returned untouched. `cryptography` is never
    imported on this path, so a base install (no `[tls]` extra) that supplies its
    own cert works fine.
  * Auto-generated cert — otherwise a self-signed cert+key is written to the
    resolved output paths (the given paths, or the defaults under `~/.wavr/`).
    Idempotent: an existing, still-valid pair is reused; regeneration happens only
    when the pair is missing or expired.

`cryptography` is a LAZY, in-function import used ONLY on the generate/validate
path (an optional `[tls]` extra). The default loopback-only mode never calls into
this module, so the base install never needs `cryptography`.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Self-signed cert parameters.
CERT_CN = "wavr"
CERT_VALID_DAYS = 397           # CA/Browser-forum max for leaf certs; ample for local use
RSA_KEY_BITS = 2048             # broadly compatible with mobile TLS stacks
_CLOCK_SKEW = timedelta(minutes=5)   # backdate not_valid_before to tolerate skew


def _default_dir() -> Path:
    """Default data dir for auto-generated material: `~/.wavr/`. Kept out of the
    cwd so running Wavr from anywhere reuses the same cert. Overridable with
    `WAVR_TLS_DIR` (useful for packaging / tests)."""
    return Path(os.getenv("WAVR_TLS_DIR", str(Path.home() / ".wavr")))


def _default_paths() -> tuple[str, str]:
    d = _default_dir()
    return str(d / "cert.pem"), str(d / "key.pem")


def ensure_cert(cert_path: str, key_path: str, local_ip: str) -> tuple[str, str]:
    """Return `(cert_path, key_path)` for the LAN HTTPS/WSS listener.

    - If both paths are given and both files exist -> returned as-is (user cert;
      no `cryptography` import).
    - Otherwise generate a self-signed cert+key (CN=`wavr`; SANs = `localhost`,
      `127.0.0.1`, `local_ip`) at the resolved output paths and return those.
      Idempotent: an existing, unexpired pair is reused.
    """
    # 1. User-provided cert: both paths given AND both files present -> use as-is.
    #    Pure filesystem checks; `cryptography` is not touched here.
    if cert_path and key_path and os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path

    # 2. Resolve where the auto-generated pair lives: honour any given path, else
    #    fall back to the `~/.wavr/` defaults.
    default_cert, default_key = _default_paths()
    out_cert = cert_path or default_cert
    out_key = key_path or default_key

    # 3. Idempotent reuse: a full, still-valid pair on disk is kept as-is.
    if os.path.exists(out_cert) and os.path.exists(out_key) and _cert_is_valid(out_cert):
        return out_cert, out_key

    # 4. (Re)generate. This is the ONLY path that needs `cryptography`.
    _generate_self_signed(out_cert, out_key, local_ip)
    return out_cert, out_key


def resolved_cert_path(cert_path: str) -> str:
    """The path `ensure_cert` WOULD serve for `cert_path`, WITHOUT generating or
    touching anything: the given path if non-empty, else the `~/.wavr/` default.

    Lets the app read the LIVE serving cert's fingerprint (see `cert_fingerprint`)
    for out-of-band pairing verification, using the same resolution `serve.py` used
    to hand the cert to uvicorn."""
    if cert_path:
        return cert_path
    default_cert, _ = _default_paths()
    return default_cert


def cert_fingerprint(cert_path: str) -> str | None:
    """SHA-256 fingerprint of the DER certificate at `cert_path`, formatted exactly
    as a browser's certificate viewer shows it: uppercase hex, colon-separated
    (e.g. `AB:CD:...`). Returns `None` if the file is missing/unreadable or is not a
    parseable PEM certificate.

    This is the out-of-band anchor that defeats a pairing-time TLS MitM: the operator
    reads it off the TRUSTED loopback dashboard and compares it against the fingerprint
    the phone's browser shows in its certificate-warning dialog. A MitM's substituted
    self-signed cert has a different fingerprint, so the mismatch is visible.

    Pure stdlib (base64 + hashlib) so it works on the base install WITHOUT the `[tls]`
    extra — `cryptography` is never imported here."""
    try:
        pem = Path(cert_path).read_text(encoding="ascii", errors="ignore")
    except OSError:
        return None
    return fingerprint_from_pem(pem)


def format_fingerprint(der: bytes) -> str:
    """SHA-256 of DER cert bytes as uppercase colon-separated hex (browser
    cert-viewer style). The single formatting authority shared by
    fingerprint_from_pem (disk/PEM path) and peer_client's per-call TLS pin
    check (live-socket DER path) so the two can never silently desync."""
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def fingerprint_from_pem(pem: str) -> str | None:
    """SHA-256 fingerprint of the first `CERTIFICATE` block in `pem`, formatted
    uppercase colon-separated hex (browser-style). None if no parseable block.
    Extracted from `cert_fingerprint` (Phase 1 peer-pairing, 2026-07-09) so a
    PEM fetched over the network (see `remote_cert_fingerprint`) can be
    fingerprinted the same way as one read from disk -- one formatting rule,
    two sources."""
    der = _first_cert_der(pem)
    return None if der is None else format_fingerprint(der)


def verification_code(fingerprint_hex: str, pair_code: str) -> str:
    """6-digit CONVENIENCE-tier verification code binding a cert fingerprint to the
    live rotating pairing code (pinned derivation, 2026-07-13 companion pairing):

        input  = <fp_hex_lowercase_no_colons> + "|" + <pair_code>
        digest = SHA-256(input)                                          (32 bytes)
        code   = (first 4 bytes of digest, big-endian uint32) mod 1_000_000,
                 zero-padded to 6 decimal digits

    Binding to `pair_code` -- the SAME short-TTL code `/api/pair-code` already
    mints -- means this 6-digit isn't offline-grindable: a MitM would have to
    brute-force it within that code's ~2-minute TTL, the accepted tradeoff for a
    number short enough to type on a phone keyboard. This is deliberately NOT the
    strong anchor -- `cert_fingerprint`/`format_fingerprint` (full 256-bit,
    eyeball- or QR-compared) remains the source of truth; `verify6` only makes the
    common case faster to check without weakening it, since a mismatch still
    hard-fails to the existing interception screen exactly like a fingerprint
    mismatch would.

    `fingerprint_hex` may be given either in the colon-separated, uppercase form
    `cert_fingerprint`/`format_fingerprint` return (e.g. "AB:CD:...") or as plain
    hex, and in either case is normalized to lowercase-no-colons here -- so the
    backend and any caller that re-derives from a differently-formatted source
    (e.g. a browser's own colon/uppercase cert display) always hash the SAME
    bytes for the SAME certificate.

    STRONG TIER (not built here): a full-256-bit fingerprint compare over a QR
    code, for camera-equipped pairing. `verification_code` is only the
    lower-friction convenience tier; leave the QR/camera path for later.
    """
    normalized = fingerprint_hex.replace(":", "").lower()
    input_string = f"{normalized}|{pair_code}"
    digest = hashlib.sha256(input_string.encode("utf-8")).digest()
    n = int.from_bytes(digest[:4], "big") % 1_000_000
    return f"{n:06d}"


def _default_remote_fetch(host: str, port: int, timeout: float) -> str:
    """Real network TOFU-fetch: connect and return the PEM of whatever
    certificate the peer presents, WITHOUT validating it against any CA --
    validation is the caller's job (compare the resulting fingerprint against
    an admin-confirmed value). Pure stdlib `ssl`."""
    import ssl
    return ssl.get_server_certificate((host, port), timeout=timeout)


def remote_cert_fingerprint(host: str, port: int, timeout: float = 5.0,
                             fetch=None) -> str | None:
    """SHA-256 fingerprint of the certificate `host:port` presents RIGHT NOW,
    for the peer-pairing exchange (Phase 1): the admin compares this against
    the peer's own on-screen fingerprint before the pairing is trusted. `fetch`
    is injectable ((host, port, timeout) -> PEM str); the default makes a real
    TLS connection and returns whatever cert is presented, unvalidated -- this
    function is the TOFU probe, not the trust decision. Returns None on any
    connection failure or unparseable response (never raises -- a peer that's
    offline or mid-reboot is an honest 'can't fingerprint yet', not a crash)."""
    fetcher = fetch or _default_remote_fetch
    try:
        pem = fetcher(host, port, timeout)
    except Exception:
        return None
    return fingerprint_from_pem(pem)


def _first_cert_der(pem: str) -> bytes | None:
    """Decode the first PEM `CERTIFICATE` block to DER bytes, or `None` if the text
    has no well-formed certificate block."""
    begin = "-----BEGIN CERTIFICATE-----"
    end = "-----END CERTIFICATE-----"
    start = pem.find(begin)
    if start == -1:
        return None
    body_start = start + len(begin)
    stop = pem.find(end, body_start)
    if stop == -1:
        return None
    b64 = "".join(pem[body_start:stop].split())
    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None


def _cert_is_valid(cert_path: str) -> bool:
    """True if `cert_path` parses as an X.509 cert that has not yet expired. Any
    parse error (corrupt / truncated file) counts as invalid -> triggers a
    regenerate. Lazy-imports `cryptography`."""
    try:
        x509 = _import_x509()
        cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
        return datetime.now(timezone.utc) < cert.not_valid_after_utc
    except Exception:
        return False


def _generate_self_signed(cert_path: str, key_path: str, local_ip: str) -> None:
    """Write a fresh self-signed cert + private key (PEM) to the given paths.
    Lazy-imports `cryptography`; raises a clear error if the `[tls]` extra is
    absent."""
    x509 = _import_x509()
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=RSA_KEY_BITS)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CERT_CN)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)                       # self-signed: issuer == subject
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _CLOCK_SKEW)
        .not_valid_after(now + timedelta(days=CERT_VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(_sans(x509, local_ip)), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    _write_secret(cert_path, cert_pem, private=False)
    _write_secret(key_path, key_pem, private=True)


def _sans(x509, local_ip: str) -> list:
    """SAN list: DNS `localhost` plus the loopback and LAN IPs, de-duplicated so a
    `local_ip` that is missing / already loopback doesn't create a duplicate
    entry."""
    sans = [x509.DNSName("localhost")]
    seen: set[str] = set()
    for ip in ("127.0.0.1", local_ip):
        if not ip or ip in seen:
            continue
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
            seen.add(ip)
        except ValueError:
            continue   # not a literal IP -> skip rather than crash generation
    return sans


def _write_secret(path: str, data: bytes, *, private: bool) -> None:
    """Write PEM bytes, creating parent dirs. The private key is written 0600 and
    its dir 0700 where the OS honours it (best-effort on Windows)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if private:
        try:
            os.chmod(p.parent, 0o700)
        except OSError:
            pass
    p.write_bytes(data)
    if private:
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass


def _import_x509():
    """Lazy import of `cryptography.x509` with a friendly error pointing at the
    optional extra. Only ever reached on the generate/validate path."""
    try:
        from cryptography import x509
    except ImportError as exc:   # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "Local TLS needs the 'cryptography' package. Install the extra: "
            "pip install -e backend[tls]"
        ) from exc
    return x509
