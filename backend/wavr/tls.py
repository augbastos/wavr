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
