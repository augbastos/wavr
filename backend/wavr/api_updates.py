"""What version this Core is on, and how this particular install updates.

Two routes, and the split is the design:

  * **GET** answers from what the Core already knows, and makes no request. It
    is the one an operator hits, and on an install that has never enabled the
    check it is a purely local answer.
  * **POST** performs a check, and only if the `update_check` connector is
    switched on. Through `guarded_call`, which is the same gate every other
    outward-reaching feature in this product goes through — a second egress path
    beside it would be a second place to audit and a second place to forget.

Never automatic. There is no timer here and no background task: a check happens
because somebody pressed something, which is also why it needs no schedule, no
backoff and no jitter.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from wavr.connectors.http import get_json, guarded_call
from wavr.updates import CONNECTOR_ID, RELEASES_URL, read_release, status


def build_router(*, running_version, connectors, require_local,
                 require_scope, fetch=None) -> APIRouter:
    """Routes over `updates`.

    `fetch` is injectable for the same reason every other egress seam here is:
    a test that reaches GitHub is a test that fails on a train.
    """
    router = APIRouter()
    _get = fetch or (lambda: get_json(RELEASES_URL, timeout=6.0))

    def _enabled() -> bool:
        try:
            return bool(connectors.is_enabled(CONNECTOR_ID))
        except Exception:      # noqa: BLE001 -- a store read must not be able to
            return False       # answer "may we reach outward" by failing open

    @router.get("/api/updates")
    async def read(_=Depends(require_scope("control"))):
        """Version, channel and the instruction for THIS install.

        Makes no request. On a Core that has never enabled the check, this
        route is entirely local — which is the normal case and should stay the
        cheap one.
        """
        return status(running=running_version,
                      check_enabled=_enabled()).to_dict()

    @router.post("/api/updates/check")
    async def check(_=Depends(require_local),
                    __=Depends(require_scope("admin"))):
        """Look, once, if the connector permits it.

        Local + admin rather than `control`: this is the route that reaches
        outward, and reaching outward is an owner's decision in this product
        even when the thing being fetched is a public list.
        """
        latest = {"tag": "", "error": ""}

        def _do() -> dict:
            try:
                return {"tag": read_release(_get()), "error": ""}
            except Exception as exc:      # noqa: BLE001 -- any transport can fail
                # Reported, not raised. A failed update check is not a broken
                # Core, and a 500 here would make it look like one.
                return {"tag": "", "error": f"could not reach the release list: {exc}"}

        result = guarded_call(connectors, CONNECTOR_ID, _do,
                              disabled_result={"tag": "", "error": "",
                                               "status": "disabled"})
        if result.get("status") == "disabled":
            raise HTTPException(
                status_code=409,
                detail=("Checking for updates is switched off. Turn on the "
                        "'update_check' connector — it is an outward-reaching "
                        "feature like any other, and it stays off until you say "
                        "otherwise."))
        latest = result
        return status(running=running_version, check_enabled=True,
                      checked=not latest["error"],
                      latest=latest["tag"] or None,
                      error=latest["error"]).to_dict()

    return router
