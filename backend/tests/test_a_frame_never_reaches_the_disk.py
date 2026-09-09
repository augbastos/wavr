"""The invariant that is the product, given something that fails when it stops.

AGENTS.md states it, README.md states it, ADR-0002 is built on it, and
`project.json` lists it under `invariants` with the note that these "are enforced
in code and covered by tests":

    Camera frames and pose keypoints are never written to disk. Only derived
    signals -- occupancy, confidence, explanation -- persist.

It was true. Nothing enforced it. Grep the backend for `cv2.imwrite`, for
`.save(`, for a binary `open(..., "wb")`, and there are no hits -- so the
sentence holds today, by the accident of nobody having added one. A reviewer who
did not already know the invariant had nothing to fail against, and the sentence
would have gone quietly false the first time somebody debugged a detection by
dumping a frame "just temporarily".

That is the dominant defect class in this repository, named in AGENTS.md itself:
**a guarantee needs a producer.** This file is that producer.

## What it checks, and what it deliberately does not

It reads the source of the camera and vision path and refuses any call that
writes bytes out of the process. It does NOT try to prove the invariant
dynamically by running a camera -- there is no camera in CI, a mock would prove
only that the mock does not write, and a test that can only pass is not a test.

Static reading has a real limit and it is worth stating rather than hiding: it
catches the obvious way to break this, not a determined one. `getattr(cv2,
"imwri" + "te")` sails through. That is fine. The failure this exists to prevent
is not sabotage, it is a debugging line left in -- and a debugging line is always
spelled the obvious way.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend" / "wavr"

# The modules a frame can actually be inside. Widening this list is cheap; the
# reason it is a list rather than "the whole backend" is that a false positive
# here (a legitimate `.save()` on a database or a config) teaches people to add
# an exemption, and an exemption list is how a guard dies.
VISION_PATHS = [
    BACKEND / "sources" / "camera.py",
    BACKEND / "localize.py",
    BACKEND / "calib_store.py",
]

# Anything that puts bytes somewhere they outlive the process.
FORBIDDEN_CALLS = {
    "imwrite": "OpenCV writes an image file",
    "imsave": "an image is saved",
    "savez": "a numpy archive is written",
    "save": "something is serialised to a path",
    "dump": "something is pickled or serialised",
    "to_csv": "a table is written out",
    "write_bytes": "bytes go to a path",
}

# `save` and `dump` are common words. These callers are not frames.
ALLOWED_RECEIVERS = {
    "json", "yaml", "toml", "conn", "db", "cur", "cursor", "store", "config",
    "session", "self",
}


def _calls(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def _name_of(func: ast.expr) -> tuple[str, str]:
    """(receiver, attribute) for `a.b()`, or ("", "f") for a bare `f()`."""
    if isinstance(func, ast.Attribute):
        recv = func.value.id if isinstance(func.value, ast.Name) else ""
        return recv, func.attr
    if isinstance(func, ast.Name):
        return "", func.id
    return "", ""


@pytest.mark.parametrize("path", VISION_PATHS, ids=lambda p: p.name)
def test_no_frame_is_written_out_of_the_process(path):
    if not path.exists():
        pytest.skip(f"{path.name} does not exist in this tree")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    offending = []
    for call in _calls(tree):
        recv, attr = _name_of(call.func)
        if attr not in FORBIDDEN_CALLS:
            continue
        if recv in ALLOWED_RECEIVERS:
            continue
        offending.append(f"line {call.lineno}: {recv + '.' if recv else ''}{attr}() "
                         f"-- {FORBIDDEN_CALLS[attr]}")

    assert not offending, (
        f"{path.relative_to(REPO)} writes something out of the process:\n  "
        + "\n  ".join(offending)
        + "\n\nCamera frames and pose keypoints never reach the disk. That is not "
          "a preference, it is the sentence README.md, AGENTS.md, project.json "
          "and ADR-0002 all rest on. If this is a legitimate write of DERIVED "
          "state, move it out of the vision path or name the receiver so it is "
          "obviously not a frame."
    )


@pytest.mark.parametrize("path", VISION_PATHS, ids=lambda p: p.name)
def test_nothing_in_the_vision_path_opens_a_file_for_writing(path):
    if not path.exists():
        pytest.skip(f"{path.name} does not exist in this tree")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    offending = []
    for call in _calls(tree):
        _, attr = _name_of(call.func)
        if attr != "open":
            continue
        modes = [a.value for a in call.args[1:2] if isinstance(a, ast.Constant)]
        modes += [k.value.value for k in call.keywords
                  if k.arg == "mode" and isinstance(k.value, ast.Constant)]
        if any(m for m in modes if isinstance(m, str)
               and any(c in m for c in "wxa")):
            offending.append(f"line {call.lineno}: open(..., {modes[0]!r})")

    assert not offending, (
        f"{path.relative_to(REPO)} opens a file for writing:\n  "
        + "\n  ".join(offending)
        + "\n\nSee the docstring: the failure this prevents is a debugging dump "
          "left behind, and this is what one looks like."
    )


def test_the_guard_can_actually_fail():
    """A guard nobody has seen fail is a guard nobody has tested.

    This is the control. It plants the exact line the tests above exist to
    catch and asserts they catch it -- so a refactor that quietly stops
    matching anything is caught here rather than by the leak it was meant to
    prevent.
    """
    planted = ast.parse("import cv2\ndef debug(frame):\n    cv2.imwrite('f.png', frame)\n")
    hits = [c for c in _calls(planted)
            if _name_of(c.func)[1] in FORBIDDEN_CALLS
            and _name_of(c.func)[0] not in ALLOWED_RECEIVERS]
    assert hits, "the detector no longer recognises a plain cv2.imwrite call"

    planted_open = ast.parse("def debug(frame):\n    open('f.raw', 'wb').write(frame)\n")
    found = False
    for call in _calls(planted_open):
        if _name_of(call.func)[1] != "open":
            continue
        modes = [a.value for a in call.args[1:2] if isinstance(a, ast.Constant)]
        if any("w" in m for m in modes if isinstance(m, str)):
            found = True
    assert found, "the detector no longer recognises open(..., 'wb')"
