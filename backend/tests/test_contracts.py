"""Every published shape carries a version, and nothing defines its own.

This file exists because versions were scattered — eight shapes each defining
their own constant next to their producer, two carrying none at all. That is
fine until somebody adds a ninth, and a contract that ships unversioned cannot
be changed afterwards without breaking something silently: there is nothing in
the payload for a consumer to check.

So the tests here are guards on the ARRANGEMENT rather than on any one value.
"""
import re
from pathlib import Path

import pytest

from wavr.contracts import CONTRACTS, summary, version

WAVR = Path(__file__).resolve().parents[1] / "wavr"


def test_every_contract_has_a_version():
    for name, v in CONTRACTS.items():
        assert isinstance(v, int) and v >= 1, name


def test_an_unknown_contract_fails_loudly_rather_than_defaulting():
    """A producer stamping a contract this table has never heard of is a
    contract nobody can version, and a `0` would let it ship looking like it
    had one."""
    with pytest.raises(KeyError):
        version("something_nobody_declared")


def test_no_producer_defines_its_own_version_number():
    """The arrangement this file protects. A second definition drifts, and the
    drift is two components disagreeing about what "version 1" means."""
    offenders = []
    for path in WAVR.glob("*.py"):
        if path.name == "contracts.py":
            continue
        src = path.read_text(encoding="utf-8")
        for match in re.finditer(
                r"^([A-Z_]*(?:PROTOCOL|CONTRACT|FORMAT)?_?VERSION)\s*=\s*(\d+)\s*$",
                src, re.M):
            offenders.append(f"{path.name}: {match.group(1)} = {match.group(2)}")
    assert offenders == [], (
        "these define a version literal instead of importing it from "
        f"`contracts`: {offenders}")


def test_the_shapes_that_had_no_version_now_carry_one():
    """`spatial_events` and `anchors` shipped unversioned. A consumer had
    nothing to check when a field's meaning changed."""
    assert "spatial_events" in CONTRACTS and "anchors" in CONTRACTS


def test_an_event_carries_its_version_on_every_frame():
    """Not only at a discovery endpoint: an event arrives on a socket a consumer
    may have opened before that endpoint existed, and a version it has to fetch
    separately is a version half of them will not fetch."""
    from wavr.spatial_events import SpatialEvents
    ev = SpatialEvents()
    state = {"room": "kitchen", "occupied": False, "confidence": 0.0,
             "person_count": None, "precision_level": "room", "sources": [],
             "ts": "2026-09-04T12:00:00+00:00"}
    ev.observe(state)
    out = ev.observe({**state, "occupied": True})
    assert out[0]["v"] == version("spatial_events")


def test_an_anchor_listing_carries_its_version_once():
    """Anchors arrive as a list from one endpoint, so one stamp is enough and
    thirty copies would be noise."""
    from wavr.anchors import AnchorStore, summarize
    store = AnchorStore(":memory:")
    store.create("Counter", "kitchen")
    out = summarize(store.list(), ["kitchen"])
    assert out["v"] == version("anchors")
    assert "v" not in out["anchors"][0], "not repeated per row"


def test_the_summary_says_what_a_bump_means():
    """The bar is not "we changed something". It is "an old reader would now be
    wrong"."""
    body = summary()
    assert "Adding a field is not a break" in body["compatibility"]
    assert "removed or" in body["compatibility"]


def test_the_summary_tells_a_client_what_to_do_with_a_newer_core():
    """Refusing would break every installed application the day somebody updates
    their Core — and that somebody is not whoever wrote the app."""
    assert "Keep reading the fields you know" in summary()["if_this_core_is_newer"]


def test_the_context_the_export_and_the_bundle_all_read_from_the_table():
    from wavr.api_experience import EXPERIENCE_PROTOCOL_VERSION
    from wavr.apple import APPLE_PROTOCOL_VERSION
    from wavr.config_export import BUNDLE_VERSION, CONFIG_VERSION
    assert EXPERIENCE_PROTOCOL_VERSION == version("experience_context")
    assert CONFIG_VERSION == version("config_export")
    assert BUNDLE_VERSION == version("diagnostic_bundle")
    assert APPLE_PROTOCOL_VERSION == version("apple_spatial")


def test_the_developer_endpoint_reports_the_whole_table():
    """A developer asking "what does this Core speak" should not find out one
    404 at a time."""
    from wavr.api_developer import DEVELOPER_PROTOCOL
    assert set(DEVELOPER_PROTOCOL) <= set(CONTRACTS)
    for name, v in DEVELOPER_PROTOCOL.items():
        assert v == CONTRACTS[name], name


# -- the SDKs ship the contract the Core ships ---------------------------------

def test_every_sdk_declares_the_contract_version_the_core_ships():
    """Three constants in three languages, and nothing tied them to this file.

    `experience_context` went to 2 for a breaking change — `sensors[].sensor_id`
    removed, `label` added — and none of the SDKs moved with it. Each declares
    `PROTOCOL_VERSION`, each computes `protocolAhead = coreVersion >
    PROTOCOL_VERSION`, and every one of them therefore reported the Core as
    ahead of itself from the first run, including an SDK and a Core shipped in
    the same commit.

    That is worse than a stale number. Wavr's own `spatial-web` reference page
    renders "This Core speaks a newer contract than this page was written
    against" on every load, and any application that follows the SDKs' advice
    to branch on `protocolAhead` takes its degraded path forever — so the
    signal is spent before a real bump can use it.

    The constants are plain literals in all three files, so this reads them
    directly rather than trusting a changelog.
    """
    import re
    from pathlib import Path
    from wavr.contracts import version

    root = Path(__file__).resolve().parents[2]
    want = version("experience_context")
    files = {
        "sdk/javascript/wavr.js": r"export const PROTOCOL_VERSION = (\d+);",
        "sdk/python/wavr_sdk/__init__.py": r"^PROTOCOL_VERSION = (\d+)$",
        "core-launcher/app/src/main/java/dev/wavr/sdk/WavrClient.kt":
            r"const val PROTOCOL_VERSION = (\d+)",
    }
    wrong = {}
    for rel, pat in files.items():
        path = root / rel
        assert path.is_file(), f"{rel} moved; this test can no longer see it"
        m = re.search(pat, path.read_text(encoding="utf-8"), re.M)
        assert m, f"{rel}: PROTOCOL_VERSION is no longer a plain literal"
        if int(m.group(1)) != want:
            wrong[rel] = int(m.group(1))
    assert not wrong, (
        f"the Core ships experience_context v{want} and these do not: {wrong}. "
        f"Every one of them will report `protocolAhead` on every connection, "
        f"and an application that branches on it degrades permanently.")


# -- a document may not promise a door that does not exist ---------------------

def test_a_module_with_no_route_is_not_described_as_reachable():
    """`docs/EXTERNAL-PROVIDERS.md` filed OpenXR and Apple under "built and
    tested" and said "Identity, fully".

    Both modules ARE built and tested — and imported by nothing but their own
    test files. No route parses an OpenXR entity, none solves an alignment,
    there is no SDK method and no frontend surface. A headset developer read
    that page, went looking for the endpoint, and found none.

    The code was never the problem; the claim was. This holds the two together:
    while nothing imports a module, the document must say so, and the day a
    route appears the exemption disappears with it.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    backend = root / "backend" / "wavr"
    doc = (root / "docs" / "EXTERNAL-PROVIDERS.md").read_text(encoding="utf-8")

    for module in ("openxr", "apple"):
        importers = [
            p.name for p in backend.rglob("*.py")
            if p.stem != module
            and re.search(rf"\b(?:from wavr\.{module} import|import wavr\.{module}\b"
                          rf"|from wavr import [^\n]*\b{module}\b)", 
                          p.read_text(encoding="utf-8", errors="replace"))
        ]
        section = _section_for(doc, module)
        if importers:
            continue          # wired: the document is free to say so
        assert "no endpoint" in section.lower() or "there is no route" in section.lower(), (
            f"nothing in backend/wavr imports `{module}`, so no caller can "
            f"reach it — and its section of EXTERNAL-PROVIDERS.md does not say "
            f"so. Either wire a route or keep the document honest.")


def _section_for(doc: str, module: str) -> str:
    """The chunk of the document that talks about this module."""
    key = "OpenXR runtimes" if module == "openxr" else "Apple · `apple_spatial`"
    i = doc.index(key)
    j = doc.find("\n## ", i)
    return doc[i:j if j > 0 else len(doc)]
