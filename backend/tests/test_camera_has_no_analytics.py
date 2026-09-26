"""Person detection must not phone home.

Ultralytics, which runs Wavr's camera detection, sends a Google Analytics event
at the end of every `predict` call by default -- keyed by a hash of the
machine's MAC address -- and resolves public DNS names when it is imported. Wavr
promises "no analytics, no telemetry", and until `camera.import_yolo` existed
that promise was false for anybody who enabled a camera. These tests keep it
true: one import site, and it switches the network behaviour off BEFORE the
package loads (after is too late -- the flag is read at import).

Neither needs ultralytics installed: the first reads the source, the second
imports a stand-in that records what it was given.
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

WAVR = Path(__file__).resolve().parents[1] / "wavr"


def _ultralytics_imports():
    """(file, enclosing function) for every import of ultralytics in wavr/."""
    found = []
    for path in WAVR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        stack: list[str] = []

        def visit(node):
            is_fn = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if is_fn:
                stack.append(node.name)
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            if any(n == "ultralytics" or n.startswith("ultralytics.") for n in names):
                found.append((path.relative_to(WAVR).as_posix(),
                              stack[0] if stack else "<module>"))
            for child in ast.iter_child_nodes(node):
                visit(child)
            if is_fn:
                stack.pop()

        visit(tree)
    return found


def test_ultralytics_is_imported_in_exactly_one_place():
    sites = sorted(set(_ultralytics_imports()))
    assert sites == [("sources/camera.py", "import_yolo")], (
        f"ultralytics imported at {sites}. Import it through "
        f"wavr.sources.camera.import_yolo, which switches its analytics off "
        f"before the package can read the setting.")


def test_the_network_switches_are_set_before_the_package_loads(tmp_path, monkeypatch):
    pkg = tmp_path / "ultralytics"
    (pkg / "utils").mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        "import os\n"
        "SEEN = {k: os.environ.get(k) for k in ('YOLO_OFFLINE', 'YOLO_AUTOINSTALL')}\n"
        "class YOLO:\n"
        "    def __init__(self, weights):\n"
        "        self.weights = weights\n", encoding="utf-8")
    (pkg / "utils" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "utils" / "events.py").write_text(
        "class _Events:\n    enabled = True\nevents = _Events()\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    # An inherited environment must not be able to turn analytics back on.
    monkeypatch.setenv("YOLO_OFFLINE", "0")
    for mod in [m for m in sys.modules if m == "ultralytics" or m.startswith("ultralytics.")]:
        monkeypatch.delitem(sys.modules, mod)
    try:
        from wavr.sources.camera import import_yolo
        YOLO = import_yolo()
        import ultralytics
        from ultralytics.utils import events
        assert ultralytics.SEEN == {"YOLO_OFFLINE": "1", "YOLO_AUTOINSTALL": "False"}
        assert events.events.enabled is False
        assert YOLO("w.pt").weights == "w.pt"
    finally:
        for mod in [m for m in sys.modules
                    if m == "ultralytics" or m.startswith("ultralytics.")]:
            sys.modules.pop(mod, None)
        for k in ("YOLO_AUTOINSTALL",):
            os.environ.pop(k, None)
