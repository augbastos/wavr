"""Where the companion app lives — inside this repository, like everything else.

Several tests compare a file here against a file in the phone app: the role the
Core advertises against the roles the phone accepts, the words the Core reserves
against the words the phone shows, the pairing body the Core reads against the
one the phone sends. Those comparisons are the only thing standing between two
implementations of one decision drifting apart, and they have caught real
drift — a phone that discarded the Core's own advertisement because it accepted
one spelling of `role`, and a pairing path that never sent the device key.

They used to look for `mobile/` in a git worktree, by absolute path. That made
them true on one computer and skipped everywhere else, and in a quiet summary a
skipped run and a passing one are the same colour. `mobile/` is now part of the
repository, so this resolves relative to it and a missing directory is a broken
checkout rather than a reason to quietly pass.

`WAVR_MOBILE_DIR` still overrides, for a checkout arranged some other way.
"""
from __future__ import annotations

import os
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]


def mobile_dir() -> Path:
    """The companion app's `mobile/` directory. Raises if it is not there."""
    env = os.environ.get("WAVR_MOBILE_DIR", "").strip()
    base = Path(env) if env else RAIZ / "mobile"
    marca = base / "src" / "wavr-mobile-shim.js"
    if not marca.is_file():
        raise AssertionError(
            f"the companion app is not in this checkout: expected {marca}.\n"
            "mobile/ is part of this repository — if it is missing, the "
            "checkout is incomplete, and skipping these tests would report "
            "coverage this run does not have."
        )
    return base
