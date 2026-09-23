"""Installed size of every distribution in this interpreter's environment.

Prints JSON: total MB, and MB per distribution (largest first). Tooling that is
not part of a Wavr install (pip, setuptools, wheel) is listed but excluded from
the total. Run with the interpreter of the environment being measured.
"""
from __future__ import annotations

import json
from importlib import metadata
from pathlib import Path

TOOLING = {"pip", "setuptools", "wheel"}


def main() -> None:
    sizes: dict[str, int] = {}
    for dist in metadata.distributions():
        name = (dist.metadata.get("Name") or "?").lower()
        total = 0
        for f in dist.files or ():
            try:
                total += Path(dist.locate_file(f)).stat().st_size
            except OSError:
                pass
        sizes[name] = sizes.get(name, 0) + total
    counted = {k: v for k, v in sizes.items() if k not in TOOLING}
    print(json.dumps({
        "total_mb": round(sum(counted.values()) / 2**20, 1),
        "distributions": len(counted),
        "by_distribution_mb": {k: round(v / 2**20, 2) for k, v in
                               sorted(sizes.items(), key=lambda kv: -kv[1])},
    }))


if __name__ == "__main__":
    main()
