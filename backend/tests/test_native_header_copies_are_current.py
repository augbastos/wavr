"""The C ABI header has one canonical copy (native/include/wavr/wavr.h). Swift
Package Manager cannot point a system-library target outside its package, so
clients/apple carries a byte-identical copy -- and this fails the moment the
two differ, instead of a Swift build on some Mac discovering it later."""
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CANONICAL = REPO / "native" / "include" / "wavr" / "wavr.h"
COPIES = [REPO / "clients" / "apple" / "Sources" / "CWavr" / "include" / "wavr.h"]


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_every_copy_of_the_c_abi_header_is_the_canonical_one():
    want = _text(CANONICAL)
    for copy in COPIES:
        assert copy.exists(), f"{copy} is missing"
        assert _text(copy) == want, f"{copy} differs from {CANONICAL}: copy it again"
