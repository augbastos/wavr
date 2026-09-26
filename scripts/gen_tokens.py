"""Design tokens for native clients, read from the dashboard's own stylesheet.

    python scripts/gen_tokens.py [--out tokens.json]

The web dashboard's `:root` custom properties in frontend/index.html are the
source of truth for Wavr's visual language (owned by the frontend; this script
only reads them). A native client -- Compose, SwiftUI, Slint -- generates its
colours from this output at build time instead of copying hex values by hand,
so a change made once in the dashboard reaches every client, and a client can
never show a runtime state in a colour the dashboard does not.

Output: {"--bg": "#0B0E12", "--state-healthy": "#3db54a", ...} with every
`var(--x)` reference resolved. Values that are not plain colours (sizes, font
stacks, rgba()) are passed through as written.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_ROOT = re.compile(r":root\s*\{([^}]*)\}")
_DECL = re.compile(r"(--[\w-]+)\s*:\s*([^;]+);")
_VAR = re.compile(r"var\(\s*(--[\w-]+)\s*(?:,\s*([^)]*))?\)")


def tokens(css_or_html: str) -> dict[str, str]:
    raw: dict[str, str] = {}
    for block in _ROOT.findall(css_or_html):
        for name, value in _DECL.findall(block):
            raw[name] = " ".join(value.split())

    def resolve(value: str, seen: frozenset[str]) -> str:
        def one(m: re.Match) -> str:
            name, fallback = m.group(1), m.group(2)
            if name in seen:
                raise ValueError(f"circular token {name}")
            if name in raw:
                return resolve(raw[name], seen | {name})
            if fallback is not None:
                return fallback.strip()
            raise ValueError(f"undefined token {name}")
        return _VAR.sub(one, value)

    return {name: resolve(value, frozenset({name})) for name, value in raw.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    a = ap.parse_args()
    text = json.dumps(tokens((REPO / "frontend" / "index.html").read_text(encoding="utf-8")),
                      indent=1, sort_keys=True) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
