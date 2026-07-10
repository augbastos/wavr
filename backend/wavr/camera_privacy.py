"""Tapo privacy-mode CONTROL (feature 2 of the camera privacy-mode work) -- a deliberate
STUB, not a working actuator.

CONTEXT: `wavr.sources.camera` (feature 1, shipped) DETECTS privacy mode honestly from
the RTSP stream's own behaviour -- no new protocol, no credential handling beyond what
already exists for the RTSP url. This module was the planned follow-up: let the operator
toggle privacy mode FROM Wavr, over the LOCAL network, reusing the camera's already-stored
credentials.

WHY IT IS NOT IMPLEMENTED (researched, not guessed):
  * ONVIF (Profile S, which Tapo advertises) has NO privacy-mode operation in its
    standard service set -- TP-Link's own support material confirms ONVIF-compliant
    software cannot toggle it; only the Tapo app can.
  * The only known local-network path is TP-Link's UNDOCUMENTED "camera account" HTTPS
    control API (the protocol the reverse-engineered `pytapo` project / the Home
    Assistant Tapo integration implement) -- a proprietary encrypted (RSA handshake +
    AES) request/response scheme TP-Link has never published. Implementing that from
    scratch, in this repo, without a real camera to validate the handshake against,
    would be GUESSING a security-sensitive protocol -- exactly what Wavr's operating
    rules forbid. Vendoring/porting a third-party implementation would also pull in
    unaudited crypto code and a dependency this repo doesn't carry, for a proprietary
    protocol TP-Link could change at any firmware update without notice.

DECISION: ship detection (feature 1) fully; leave control OFF and honestly stubbed here.
`set_privacy_mode()` below always raises `PrivacyControlNotImplemented` -- it IS wired to
`app.py`'s `POST /api/cameras/{name}/privacy-mode` route (gated identically to `/rebind`:
require_local CSRF + "control" scope), which calls straight into this stub and turns the
raised exception into an honest `501 Not Implemented` response. Nothing in the frontend
calls that route yet. This module exists so the gap is documented in code (and reachable/
discoverable via the API, not silently absent), and so a future implementation has a
single, obvious place to land (with real-hardware validation as a hard prerequisite --
see the module docstring of `wavr.sources.camera.CameraPrivacySignal`).

If this is ever implemented: it MUST stay local-only (no cloud egress), MUST reuse only
the credentials already stored in `CameraStore` (never accept new ones over an API), MUST
never log a credential or the raw request/response bodies, and MUST default OFF behind an
explicit opt-in flag (mirroring `WAVR_PTZ`) -- the same gates `wavr.ptz` uses for its
ONVIF actuator.
"""
from __future__ import annotations


class PrivacyControlNotImplemented(NotImplementedError):
    """Raised by every call into this module. Tapo privacy-mode control has no
    documented local/ONVIF path (see module docstring) -- not yet verified, not
    guessed, not shipped."""


def set_privacy_mode(name: str, rtsp_url: str, enabled: bool) -> None:
    """Stub. Always raises PrivacyControlNotImplemented -- see module docstring for why.
    Never contacts the network; never reads/logs `rtsp_url` (which carries credentials)."""
    raise PrivacyControlNotImplemented(
        "Tapo privacy-mode control has no documented local/ONVIF path; "
        "not yet verified against real hardware -- detection-only for now"
    )
