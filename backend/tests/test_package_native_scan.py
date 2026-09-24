"""scripts/package_native.py refuses to package anything that carries a build
path, a user name, a private key or a credential -- in any encoding a binary
may hold it. Each case below slipped through an earlier version (red-team).

The leak shapes are assembled at run time from pieces. Written out whole, a
fake key header or a fake home path is exactly what the repository's own
secret scans (the pre-push scan, scripts/publication_gate.py) exist to stop,
and a test file full of them teaches everyone to ignore those scans."""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("package_native", REPO / "scripts" / "package_native.py")
pkg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pkg)

DASHES = "-" * 5
PRIVATE_KEY = "PRIVATE" + " KEY"


def pem(kind: str) -> str:
    """A PEM header line such as the one that opens a private key file."""
    return f"{DASHES}BEGIN {kind}{DASHES}"


def test_every_known_leak_shape_is_refused():
    home, users = "/" + "home", "/" + "Users"
    windows_home = "C:" + "\\" + "Users" + "\\x\\"
    leaks = [
        f"{users}/alice/build/x.c".encode(),
        f"{home}/alice/src/x.c".encode(),
        windows_home.encode("utf-16-le"),
        ("D:/" + "Workspace/proj/x.c").encode(),
        ('{"pass' + 'word": "hunter2hunter2"}').encode(),
        ("api" + "_key='abcdefghijkl'").encode(),
        (pem(PRIVATE_KEY) + "\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\n").encode(),
        (pem("RSA " + PRIVATE_KEY) + "\r\nProc-Type: 4,ENCRYPTED\r\nDEK-Info: AES-128-CBC,00\r\n\r\n"
         "MIIEpAIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt\r\n").encode(),
    ]
    for data in leaks:
        assert pkg.scan("f", data), data


def test_the_packagers_own_user_name_is_refused():
    import getpass
    assert pkg.scan("f", b"built by " + getpass.getuser().encode())


def test_what_a_clean_binary_carries_is_not_refused():
    # The build tree after -ffile-prefix-map: relative, machine-free.
    assert pkg.scan("f", b"wavr native 0.1.0 build/_deps/mbedtls-src/library/ssl_tls.c") == []
    # mbedTLS's PEM parser constants: headers with no key behind them.
    constants = pem(PRIVATE_KEY) + "\x00" + f"{DASHES}END {PRIVATE_KEY}{DASHES}" + "\x00" \
        + pem("EC " + PRIVATE_KEY) + "\x00"
    assert pkg.scan("f", constants.encode()) == []
