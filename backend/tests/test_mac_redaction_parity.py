"""Cross-language parity for the MAC-redaction privacy invariant.

MAC masking is hand-duplicated: `redact_macs` in Python (net_doctor.py) and a `MAC_RE`
regex in the JS receiver (infra/wavr-diag-worker/functions/report.js). Both must mask the
host half and keep the OUI, IDENTICALLY — otherwise a format one side misses is a raw-MAC
leak the other side promised to redact. This test extracts the worker's actual regex +
replacement, runs it under node over a fixture, and asserts byte-for-byte equality with
the Python function. Skips if node isn't available (still runs in CI — ubuntu has node)."""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from wavr.net_doctor import redact_macs

_ROOT = Path(__file__).resolve().parents[2]
_WORKER = _ROOT / "infra" / "wavr-diag-worker" / "functions" / "report.js"

_FIXTURE = [
    "aa:bb:cc:dd:ee:ff",
    "AA:BB:CC:DD:EE:FF",
    "aa-bb-cc-dd-ee-ff",
    "AA-BB-CC-DD-EE-FF",
    "Aa:bB:cC:dD:eE:fF",                       # mixed case
    "gw 11:22:33:44:55:66 vs de:ad:be:ef:00:11 done",  # two in one line
    "gateway 192.168.1.1 now aa:bb:cc:dd:ee:ff (was 11:22:33:44:55:66)",
    "192.168.1.1",                             # non-MAC (IP) — must stay
    "550e8400-e29b-41d4-a716-446655440000",    # non-MAC (UUID) — must stay
    "deadbeef port 5353",                      # non-MAC — must stay
    "no macs here at all",
]

_RAW_MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")


def _worker_redact(lines):
    src = _WORKER.read_text(encoding="utf-8")
    mac_re = re.search(r"const MAC_RE = (/.*?/g);", src).group(1)
    repl = re.search(r'\.replace\(MAC_RE,\s*("(?:[^"\\]|\\.)*")\)', src).group(1)
    script = (
        f"const MAC_RE = {mac_re};\n"
        f"const repl = {repl};\n"
        "const lines = JSON.parse(process.argv[2]);\n"  # argv[1] is the script path under node
        "process.stdout.write(JSON.stringify(lines.map(function(l){return l.replace(MAC_RE, repl);})));\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False, encoding="utf-8") as f:
        f.write(script)
        path = f.name
    try:
        out = subprocess.run(["node", path, json.dumps(lines)],
                             capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)
    finally:
        Path(path).unlink(missing_ok=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_python_and_worker_redact_macs_identically():
    py = [redact_macs(x) for x in _FIXTURE]
    js = _worker_redact(_FIXTURE)
    assert js == py, f"Python/JS MAC-redaction drift:\n python={py}\n    js={js}"
    # and neither leaves a raw MAC in any line that had one
    for line in py:
        assert _RAW_MAC.search(line) is None
    # sanity: the OUI half is kept, the host half masked
    assert redact_macs("aa:bb:cc:dd:ee:ff") == "aa:bb:cc:**:**:**"
