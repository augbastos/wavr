"""Erasing what Wavr learned. Transport only; `data_erasure.py` decides.

## Why this is not on the privacy router

`api_privacy.py` is read-only by construction, and says so: a route that could
also CHANGE things turns an audit screen into an attack surface. That reasoning
holds, so the acting half is a separate router with a separate gate rather than
one more endpoint beside the three that only read.

## The gate

`require_local` + `require_root`, the same pair that guards ARP blocking — the
most destructive primitive in the product. A paired phone, an admin companion
and an agent all get 403. Somebody has to be at the machine.

That is stricter than a privacy feature might seem to warrant, and it is the
right way round: the cost of refusing is that a person walks to the Core, and
the cost of allowing is that anything holding an admin token can delete a
household's history from across the network.

## Two steps, always

GET /api/privacy/erasable  — what can go, in plain language, with live counts.
POST /api/privacy/erase    — do it, naming the categories AND confirm=true.

The counts come first so that "this deletes 41,208 occupancy rows" is a number
somebody saw before agreeing, not one they discover afterwards. `confirm` is
required and must be exactly `true`: a body assembled by accident does not
carry it.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Body, Depends, HTTPException

from wavr.data_erasure import (DEFAULT_OBSERVATIONS, ErasureError, counts,
                               erasable, erase)


async def _deps_not_wired():
    raise HTTPException(status_code=403,
                        detail="erasure routes have no auth gate wired")


def build_erasure_router(db_path: str = "", deps=None) -> APIRouter:
    router = APIRouter(
        dependencies=deps if deps is not None else [Depends(_deps_not_wired)])

    @router.get("/api/privacy/erasable")
    async def what_can_go():
        """Everything that can be deleted, what it is, and how much of it.

        `kind` separates the two requests people actually have: `observation`
        is what Wavr learned by watching, `setup` is what a person told it.
        Deleting the first costs accuracy that rebuilds itself; deleting the
        second is starting over, and the screen must never blur them.
        """
        entries = erasable()
        # A database that cannot be counted costs the NUMBERS, not the screen:
        # every row falls back to `rows: None`, which the UI renders as "not
        # counted" rather than a comfortable zero. Narrow on purpose — an
        # `ErasureError` here would mean `erasable()` returned a key it does
        # not consider erasable, which is a bug in this module and should
        # surface as a 500 rather than as a quietly empty screen.
        try:
            live = counts(db_path, list(entries))
        except sqlite3.Error:
            live = {}
        for key, e in entries.items():
            e["rows"] = live.get(key)
        return {
            "categories": list(entries.values()),
            # What "erase what Wavr knows about my home" means with nothing
            # named. Deliberately not every observation — see data_erasure.
            "default": list(DEFAULT_OBSERVATIONS),
        }

    @router.post("/api/privacy/erase")
    async def do_erase(categories: list[str] = Body(default=None),
                       confirm: bool = Body(default=False)):
        """Delete the named categories. Nothing happens without `confirm`.

        Omitting `categories` means the default observation set, never
        everything: there is no shorthand for wiping setup, because the
        shorthand is what somebody clicks at speed.
        """
        if confirm is not True:
            raise HTTPException(
                status_code=400,
                detail="This deletes data permanently. Send confirm=true once "
                       "the person has seen what it covers.")
        wanted = categories if categories else list(DEFAULT_OBSERVATIONS)
        try:
            return erase(db_path, wanted)
        except ErasureError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    return router
