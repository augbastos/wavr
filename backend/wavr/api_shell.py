"""The dashboard shell: the static files a browser needs before it can ask the
API anything, and the developer-mode reference pages beside them.

Moved out of `create_app` unchanged. Nothing here carries data or changes
state; every route is exempt from the token gate for the same reason `/` is
(see `app._is_static_shell`), and the reasons each is safe are kept with it.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

# The reference experiences served in developer mode, and nothing else.
EXPERIENCE_PAGES = ("spatial-web", "capability-aware", "anchor-demo")

# A bare lowercase `.js` filename: no slash, no dot-segment, no backslash.
_JS_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.js$")


def build_shell_router(*, index: Path, experiences_dir: Path, sdk_dir: Path,
                       developer_mode_on: Callable[[], bool]) -> APIRouter:
    router = APIRouter()
    frontend = index.parent

    # -- Reference experiences, developer mode only -------------------------
    #
    # Served from the Core so they are one click away from the machine that has
    # the data, rather than something a developer has to host themselves and
    # then fight CORS over.
    #
    # Gated on the developer-mode switch and on a fixed list of names, and
    # deliberately on NOTHING else -- no header check and no scope. An earlier
    # version of this comment described a header-and-scope pair that the
    # routes below do not carry and must not: a top-level browser navigation
    # cannot send a custom header, so that gate made the pages impossible to
    # open, which is the single thing they exist for (see `experience_page`,
    # and the `/experiences/*` clause in `_is_static_shell`). They are static
    # HTML carrying no data, exactly as "/" is; the Space stays behind the API
    # they then call, which does send the header and is redacted per
    # experience. A comment describing a gate the code does not have is how
    # the next reader concludes a surface is safer than it is.

    def _developer_file(path):
        """A file under the developer directories, or a 404 that says which.

        `resolve()` and a containment check rather than trusting the name: these
        segments come from a URL, and `..` in one of them would otherwise read
        any file the process can. The names are also matched against a fixed
        tuple above, which alone would be enough — this is the second lock,
        because a future route that forgets the tuple should still be safe.
        """
        if not developer_mode_on():
            raise HTTPException(status_code=403,
                                detail="Developer mode is off. Turn it on in Settings.")
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise HTTPException(status_code=404, detail="not bundled in this build")
        root = index.parent.parent.resolve()
        if root not in resolved.parents:
            raise HTTPException(status_code=404, detail="not found")
        if not resolved.is_file():
            raise HTTPException(status_code=404, detail="not bundled in this build")
        return FileResponse(resolved)

    @router.get("/experiences/{name}/")
    async def experience_page(name: str):
        """A reference page, served like the dashboard shell.

        No `require_local` and no scope, and that is not a relaxation — it is a
        correction. A top-level browser navigation cannot send a custom header,
        so requiring one made these pages impossible to open, which is the
        single thing they exist for. They are static HTML carrying no data,
        exactly as "/" is, and the Space stays behind the API they then call.

        Still gated on developer mode, still restricted to a fixed set of names,
        and still containment-checked before anything is read from disk.
        """
        if name not in EXPERIENCE_PAGES:
            raise HTTPException(status_code=404, detail=f"no experience {name!r}")
        return _developer_file(experiences_dir / name / "index.html")

    @router.get("/sdk/javascript/wavr.js")
    async def sdk_javascript():
        """The SDK the reference pages import.

        The real file, not a copy. A served copy would drift from the one in the
        repository, and the drift would be discovered by whoever trusted the
        page they were reading.
        """
        return _developer_file(sdk_dir / "javascript" / "wavr.js")

    @router.get("/")
    async def dashboard():
        return FileResponse(index)

    # sw.js precaches "./index.html" by name (Cache.addAll is all-or-nothing), but only
    # "/" was ever registered -- so that entry 404'd and the service worker never
    # installed on the live origin (H3 audit fix). Same response as "/"; exempted from
    # the token gate the same way "/" is (see loopback_or_authed in app.py).
    @router.get("/index.html")
    async def dashboard_index_html():
        return FileResponse(index)

    # PWA shell files, served same-origin so the app installs + caches without any
    # external request (the SW registers, the manifest resolves, the icon loads). These
    # are the static shell; like "/" they carry nothing sensitive.
    @router.get("/manifest.webmanifest")
    async def manifest():
        return FileResponse(frontend / "manifest.webmanifest",
                            media_type="application/manifest+json")

    @router.get("/sw.js")
    async def service_worker():
        return FileResponse(frontend / "sw.js", media_type="text/javascript")

    @router.get("/icon.svg")
    async def icon():
        return FileResponse(frontend / "icon.svg", media_type="image/svg+xml")

    # Every module lifted out of index.html, served by ONE route.
    #
    # This was eleven hand-written routes, one per file, under a comment
    # arguing that "an allowlist of two filenames has no traversal surface".
    # That was true at two. At eleven it had turned into a chore, and the chore
    # gates something bigger than one script: a module the shell requests and
    # the backend does not serve 404s, `Cache.addAll` is all-or-nothing, so the
    # service worker's install fails as a unit and the whole OFFLINE shell goes
    # with it. Forgetting a route costs offline launch, not one feature.
    #
    # The traversal surface is closed by construction rather than by
    # enumeration. Two independent checks, either one sufficient:
    #
    #   * the name must be a bare lowercase filename ending in `.js` — no
    #     slash, no dot-segment, no backslash, nothing encoded survives, and
    #     Starlette percent-DECODES the path parameter before this sees it, so
    #     the string being matched is the one that would reach the filesystem;
    #   * the resolved path must be a direct child of `frontend/js` and a
    #     regular file, checked AFTER resolution, so a symlink planted in that
    #     directory and pointing elsewhere still opens nothing.
    js_dir = (frontend / "js").resolve()

    @router.get("/js/{name}")
    async def js_module(name: str):
        """A shell script: markup and script, no data, no action.

        Served without the `X-Wavr-Local` header, like the rest of the shell —
        a browser navigating to the page cannot send a custom header, and a
        script it cannot fetch is a blank screen with nothing to explain it.
        Developer tooling is the same class: `developer.js` is inert on a
        normal install (its first request comes back 403 and it renders
        "developer mode is off"), and gating the FILE would take the offline
        shell down for everybody to hide a script that already says no.
        """
        if not _JS_NAME.match(name):
            raise HTTPException(status_code=404, detail="not found")
        path = (js_dir / name).resolve()
        if path.parent != js_dir or not path.is_file():
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(path, media_type="text/javascript")

    # F2 phone-capture shell (WebXR, "Measure with your phone"). Static, carries nothing
    # sensitive -- like "/" it is token/subnet-exempt so an unpaired LAN phone can load
    # it; the data endpoint (PUT /api/house/room) still requires a central-role token.
    @router.get("/measure.html")
    async def measure_page():
        return FileResponse(frontend / "measure.html", media_type="text/html")

    return router
