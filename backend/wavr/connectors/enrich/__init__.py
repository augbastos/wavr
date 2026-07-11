"""ENRICH surface: best-effort alert/context enrichers (opt-in, default-OFF,
broker-gated -- see `wavr.connectors` package docstring).

Each module here exposes ONE `make_..._fetch`/`make_..._lookup` factory that
returns a closure gated on every call by `wavr.connectors.http.guarded_call`.
Enrichment is fail-open by design: a disabled or failing enricher returns a
clean `{"ok": False, "status": ...}` and must never suppress or crash the
base alert/context it was asked to enrich (see DESIGN-external-connectors.md
section 3.2) -- callers wire the result in additively.
"""
from __future__ import annotations
