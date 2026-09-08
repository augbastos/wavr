"""Where the companion app's `mobile/` directory is, without naming a disk.

The phone app lives in a git worktree of this same repository, so several tests
compare a file here against a file there — the role the Core advertises against
the roles the phone accepts, the strings the Core reserves against the strings
the phone shows, and so on. Those comparisons are the only thing standing
between two implementations of one decision drifting apart.

Five of those tests used to carry the worktree's absolute path written into the
file. That made every one of them true on exactly one computer and skipped
everywhere else — and in a quiet summary an all-skipped run and an all-passed
run are the same colour, so the guarantee they describe held nowhere.

Git already knows where its own worktrees are. So it is asked, rather than told.
`WAVR_MOBILE_DIR` overrides for a checkout arranged some other way.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]


def _candidatos() -> list[Path]:
    vindos: list[Path] = []
    env = os.environ.get("WAVR_MOBILE_DIR", "").strip()
    if env:
        vindos.append(Path(env))
    # A checkout that carries `mobile/` in the tree itself.
    vindos.append(RAIZ / "mobile")
    try:
        saida = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(RAIZ), capture_output=True, text=True, timeout=20).stdout
    except Exception:      # noqa: BLE001 -- no git available; not an error here
        saida = ""
    for linha in saida.splitlines():
        if linha.startswith("worktree "):
            vindos.append(Path(linha[len("worktree "):].strip()) / "mobile")
    return vindos


def mobile_dir() -> Path | None:
    """The companion app's `mobile/` directory, or None if it is not checked out.

    Presence is decided by the shim, because the shim is what every caller here
    ultimately reads or reasons about.
    """
    for base in _candidatos():
        if (base / "src" / "wavr-mobile-shim.js").exists():
            return base
    return None
