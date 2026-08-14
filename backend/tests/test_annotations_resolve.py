"""Annotations have to still name something that exists.

`from __future__ import annotations` makes every annotation a string, and a
string annotation is never evaluated at import. That is exactly what makes this
class of breakage invisible: an "unused" typing import can be deleted, the module
imports fine, the whole suite stays green, and the annotation quietly refers to a
name that is gone. It only surfaces when something actually resolves it -
`typing.get_type_hints`, a runtime-hint framework, or a type checker (this repo's
CI runs neither, so nothing else here would catch it).

Caught for real: a code-health change removed `Awaitable` and `Callable` from
companion_presence.py while `resolve_source_mac` still annotated `arp_transport`
as `"Callable[[], Awaitable[str]] | None"`. Nothing failed - until you asked for
the hints, which raised `NameError: name 'Callable' is not defined`.

Guarding the whole package rather than that one function on purpose: the mistake
is generic, and a test that only knew about the module that already broke would
not catch the next one.
"""

import importlib
import pkgutil
import typing

import pytest

import wavr

# Modules whose import has side effects or hard optional deps are skipped by the
# import guard below rather than listed here - see the try/except.
_SKIP_PREFIXES = ("wavr.serve",)


def _modules():
    for info in pkgutil.walk_packages(wavr.__path__, prefix="wavr."):
        if info.name.startswith(_SKIP_PREFIXES):
            continue
        yield info.name


@pytest.mark.parametrize("nome", sorted(_modules()))
def test_annotations_name_something_that_exists(nome):
    try:
        mod = importlib.import_module(nome)
    except Exception:  # noqa: BLE001
        # Optional heavy extras (torch, cv2, bleak…) are not installed by
        # default and that is a documented invariant, not a failure here.
        pytest.skip(f"{nome} not importable in this environment")

    quebrados = []
    for atributo in vars(mod).values():
        if not callable(atributo) or getattr(atributo, "__module__", None) != nome:
            continue
        try:
            typing.get_type_hints(atributo)
        except NameError as e:
            quebrados.append(f"{getattr(atributo, '__qualname__', atributo)}: {e}")
        except Exception:  # noqa: BLE001
            # Forward references to things only imported under TYPE_CHECKING are
            # a deliberate, working pattern; only a NameError means the
            # annotation points at nothing.
            pass

    assert not quebrados, (
        f"{nome} has annotations naming things that do not exist: " + "; ".join(quebrados)
    )
