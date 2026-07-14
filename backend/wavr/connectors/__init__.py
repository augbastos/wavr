"""External connectors package (project_wavr_connectors_vision /
DESIGN-external-connectors.md): opt-in, default-OFF integrations that reach
outward from Wavr, organized by SURFACE (one subpackage per surface) rather
than one-file-per-API:

  connectors/notify/   -- outbound alert/notification channels (Telegram, ...)
  connectors/enrich/   -- alert enrichers (future: AbuseIPDB, URLhaus, ...)

Every connector is gated by the SAME broker (`wavr.connector_store.
ConnectorStore`, kind="generic"): `store.is_enabled(id)` must be True before
any byte leaves the box. `connectors/http.py` holds the one shared egress
chokepoint (`guarded_call`) and the dependency-free transport every connector
module uses -- no connector calls `urllib` directly outside that chokepoint.

Nothing in this package is wired into `app.py` yet -- see the wiring spec
returned alongside this package for the catalog-entry shape the app-level
lead applies.
"""
from __future__ import annotations
