"""Local TLS cert lifecycle tests (ADR-0006 §6, Phase 2 — closes audit H2).

Exercises `wavr.tls.ensure_cert` end to end WITHOUT binding a socket or starting
uvicorn. `cryptography` is available here because it's in the `[dev]` extra; these
tests both drive generation and parse the result back to assert its shape.
"""
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtensionOID, NameOID

from wavr.tls import (
    CERT_CN,
    cert_fingerprint,
    ensure_cert,
    fingerprint_from_pem,
    format_fingerprint,
    remote_cert_fingerprint,
    resolved_cert_path,
)

LOCAL_IP = "192.168.1.5"


def _paths(tmp_path):
    return str(tmp_path / "cert.pem"), str(tmp_path / "key.pem")


def _load_cert(path):
    with open(path, "rb") as fh:
        return x509.load_pem_x509_certificate(fh.read())


def _sans(cert):
    ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
    dns = ext.value.get_values_for_type(x509.DNSName)
    ips = [str(ip) for ip in ext.value.get_values_for_type(x509.IPAddress)]
    return dns, ips


# --------------------------------------------------------------------------- #
# Generation when absent.
# --------------------------------------------------------------------------- #
def test_generates_readable_cert_and_key_when_absent(tmp_path):
    cert_path, key_path = _paths(tmp_path)
    out_cert, out_key = ensure_cert(cert_path, key_path, LOCAL_IP)

    assert out_cert == cert_path and out_key == key_path
    # Both files exist and parse as PEM (cert + private key).
    cert = _load_cert(out_cert)
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    with open(out_key, "rb") as fh:
        key = load_pem_private_key(fh.read(), password=None)
    assert isinstance(key, rsa.RSAPrivateKey)
    assert cert is not None


def test_cert_is_self_signed_with_expected_cn(tmp_path):
    cert_path, key_path = _paths(tmp_path)
    ensure_cert(cert_path, key_path, LOCAL_IP)
    cert = _load_cert(cert_path)
    # Self-signed: issuer == subject, CN == "wavr".
    assert cert.issuer == cert.subject
    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    assert cn == CERT_CN


def test_cert_has_expected_sans(tmp_path):
    cert_path, key_path = _paths(tmp_path)
    ensure_cert(cert_path, key_path, LOCAL_IP)
    dns, ips = _sans(_load_cert(cert_path))
    assert "localhost" in dns
    assert "127.0.0.1" in ips
    assert LOCAL_IP in ips


def test_cert_validity_window_is_about_397_days(tmp_path):
    cert_path, key_path = _paths(tmp_path)
    ensure_cert(cert_path, key_path, LOCAL_IP)
    cert = _load_cert(cert_path)
    span = cert.not_valid_after_utc - cert.not_valid_before_utc
    # ~397 days plus the small clock-skew backdate; comfortably within a day of it.
    assert timedelta(days=396) <= span <= timedelta(days=399)
    assert cert.not_valid_after_utc > datetime.now(timezone.utc)


def test_loopback_local_ip_does_not_duplicate_san(tmp_path):
    # local_ip == loopback: SAN must still list 127.0.0.1 exactly once.
    cert_path, key_path = _paths(tmp_path)
    ensure_cert(cert_path, key_path, "127.0.0.1")
    _dns, ips = _sans(_load_cert(cert_path))
    assert ips.count("127.0.0.1") == 1


# --------------------------------------------------------------------------- #
# Idempotency: a valid pair is reused, not regenerated.
# --------------------------------------------------------------------------- #
def test_existing_valid_pair_is_not_regenerated(tmp_path):
    cert_path, key_path = _paths(tmp_path)
    ensure_cert(cert_path, key_path, LOCAL_IP)
    first = _load_cert(cert_path).serial_number
    with open(cert_path, "rb") as fh:
        first_bytes = fh.read()

    out_cert, out_key = ensure_cert(cert_path, key_path, LOCAL_IP)

    assert (out_cert, out_key) == (cert_path, key_path)
    with open(cert_path, "rb") as fh:
        assert fh.read() == first_bytes            # byte-identical: not rewritten
    assert _load_cert(cert_path).serial_number == first


def test_missing_file_triggers_regeneration(tmp_path):
    # A half-present pair (key deleted) is not "valid" -> regenerate both.
    import os

    cert_path, key_path = _paths(tmp_path)
    ensure_cert(cert_path, key_path, LOCAL_IP)
    first_serial = _load_cert(cert_path).serial_number
    os.remove(key_path)

    ensure_cert(cert_path, key_path, LOCAL_IP)
    assert os.path.exists(key_path)
    assert _load_cert(cert_path).serial_number != first_serial


# --------------------------------------------------------------------------- #
# User-provided cert: returned untouched.
# --------------------------------------------------------------------------- #
def test_user_provided_existing_paths_returned_as_is(tmp_path):
    # Pre-existing files at BOTH paths -> returned verbatim, never overwritten
    # (contents need not even be a real cert on this path).
    cert_path, key_path = _paths(tmp_path)
    with open(cert_path, "w") as fh:
        fh.write("USER-CERT")
    with open(key_path, "w") as fh:
        fh.write("USER-KEY")

    out_cert, out_key = ensure_cert(cert_path, key_path, LOCAL_IP)

    assert (out_cert, out_key) == (cert_path, key_path)
    with open(cert_path) as fh:
        assert fh.read() == "USER-CERT"            # untouched
    with open(key_path) as fh:
        assert fh.read() == "USER-KEY"


# --------------------------------------------------------------------------- #
# Fingerprint (audit blocking #1): out-of-band anchor for pairing MitM defense.
# --------------------------------------------------------------------------- #
def test_cert_fingerprint_matches_cryptography_sha256(tmp_path):
    # The pure-stdlib fingerprint must equal cryptography's SHA-256 over the DER cert,
    # in the browser display format (uppercase hex, colon-separated, 32 bytes).
    from cryptography.hazmat.primitives import hashes

    cert_path, key_path = _paths(tmp_path)
    ensure_cert(cert_path, key_path, LOCAL_IP)
    got = cert_fingerprint(cert_path)
    assert got is not None
    parts = got.split(":")
    assert len(parts) == 32                                  # SHA-256 = 32 bytes
    assert all(len(p) == 2 and p == p.upper() for p in parts)
    expected = _load_cert(cert_path).fingerprint(hashes.SHA256()).hex().upper()
    assert got.replace(":", "") == expected                 # same bytes browsers show


def test_format_fingerprint_matches_fingerprint_from_pem(tmp_path):
    # format_fingerprint (DER bytes -> str) is the shared formatting authority
    # peer_client's live-socket check calls directly; it must agree byte-for-byte
    # with fingerprint_from_pem's PEM-path output for the same certificate.
    cert_path, key_path = _paths(tmp_path)
    ensure_cert(cert_path, key_path, LOCAL_IP)
    pem = open(cert_path, encoding="ascii").read()
    der = _load_cert(cert_path).public_bytes(serialization.Encoding.DER)
    assert format_fingerprint(der) == fingerprint_from_pem(pem)


def test_cert_fingerprint_none_for_missing_file(tmp_path):
    assert cert_fingerprint(str(tmp_path / "does-not-exist.pem")) is None


def test_cert_fingerprint_none_for_non_pem(tmp_path):
    # A file that is not a PEM certificate must not raise -> returns None so the UI
    # simply hides the fingerprint line rather than crashing.
    p = tmp_path / "garbage.pem"
    p.write_text("this is not a certificate")
    assert cert_fingerprint(str(p)) is None


def test_resolved_cert_path_prefers_given(tmp_path):
    given = str(tmp_path / "my-cert.pem")
    assert resolved_cert_path(given) == given


def test_resolved_cert_path_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_TLS_DIR", str(tmp_path / "wavrdir"))
    assert resolved_cert_path("").startswith(str(tmp_path / "wavrdir"))


def test_defaults_used_when_no_paths_given(tmp_path, monkeypatch):
    # No explicit paths -> generate under the (overridable) default dir.
    monkeypatch.setenv("WAVR_TLS_DIR", str(tmp_path / "wavrdir"))
    out_cert, out_key = ensure_cert("", "", LOCAL_IP)
    assert out_cert.startswith(str(tmp_path / "wavrdir"))
    assert _load_cert(out_cert) is not None
    import os
    assert os.path.exists(out_key)


# --------------------------------------------------------------------------- #
# fingerprint_from_pem / remote_cert_fingerprint (Phase 1 peer-pairing, 2026-07-09).
# A real self-signed cert is generated via `ensure_cert` (same as every other
# test above) rather than hand-typing base64 PEM bytes.
# --------------------------------------------------------------------------- #
def _generated_pem(tmp_path) -> str:
    cert_path, key_path = _paths(tmp_path)
    ensure_cert(cert_path, key_path, LOCAL_IP)
    with open(cert_path, encoding="ascii") as fh:
        return fh.read()


def test_fingerprint_from_pem_matches_cert_fingerprint_shape(tmp_path):
    pem = _generated_pem(tmp_path)
    fp = fingerprint_from_pem(pem)
    assert fp is not None
    assert len(fp) == 32 * 3 - 1  # 32 hex-pairs, colon-joined
    assert fp == fp.upper()


def test_fingerprint_from_pem_agrees_with_cert_fingerprint(tmp_path):
    # Same bytes, read from disk vs passed as a PEM string -> identical fingerprint.
    cert_path, key_path = _paths(tmp_path)
    ensure_cert(cert_path, key_path, LOCAL_IP)
    with open(cert_path, encoding="ascii") as fh:
        pem = fh.read()
    assert fingerprint_from_pem(pem) == cert_fingerprint(cert_path)


def test_fingerprint_from_pem_none_on_garbage():
    assert fingerprint_from_pem("not a cert") is None


def test_remote_cert_fingerprint_uses_injected_fetch(tmp_path):
    pem = _generated_pem(tmp_path)

    def fake_fetch(host, port, timeout):
        assert (host, port) == ("192.168.1.57", 8443)
        return pem

    fp = remote_cert_fingerprint("192.168.1.57", 8443, fetch=fake_fetch)
    assert fp == fingerprint_from_pem(pem)


def test_remote_cert_fingerprint_none_on_connect_failure():
    def failing_fetch(host, port, timeout):
        raise OSError("connection refused")
    assert remote_cert_fingerprint("10.0.0.99", 8443, fetch=failing_fetch) is None
