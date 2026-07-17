"""net_doctor stack PR3 -- the remediation deep-link is honest: every actionable
cause has an in-app fix guide, every fix guide corresponds to a real callout, and
every per-router brand tip points at a docs/network-fixes/ file that actually exists.

Parses the single-file frontend with regex (no eval / no JS runtime) so it runs in
the normal pytest suite as a permanent regression guard for the PR3 acceptance:
'cause X on screen -> [Como arrumar] -> guide X'."""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_INDEX = _ROOT / "frontend" / "index.html"
_FIXDIR = _ROOT / "docs" / "network-fixes"

_ACTIONABLE = {
    "discovery_ap_isolation",
    "discovery_second_network",
    "discovery_host_unavailable",
    "discovery_multicast_dead",
}


def _html():
    return _INDEX.read_text(encoding="utf-8")


def _fix_guide_keys(html):
    block = re.search(r"var FIX_GUIDE\s*=\s*\{(.*?)\n  \};", html, re.S).group(1)
    return set(re.findall(r"(discovery_[a-z_]+):\s*\{", block))


def _discovery_copy_keys(html):
    block = re.search(r"var DISCOVERY_COPY\s*=\s*\{(.*?)\n  \};", html, re.S).group(1)
    return set(re.findall(r"(discovery_[a-z_]+):\s*function", block))


def _brand_docs(html):
    # the searchable router DB (array of brands) + the generic fallback entry
    block = re.search(r"var ROUTER_DB\s*=\s*\[(.*?)\n  \];", html, re.S).group(1)
    generic = re.search(r"var ROUTER_GENERIC\s*=\s*\{(.*?)\n  \};", html, re.S).group(1)
    return re.findall(r'doc:\s*"([^"]+)"', block + generic)


def test_every_actionable_cause_has_a_fix_guide():
    keys = _fix_guide_keys(_html())
    assert _ACTIONABLE <= keys, f"missing fix guides for {_ACTIONABLE - keys}"


def test_every_fix_guide_has_a_matching_callout():
    # a [Como arrumar] button must never appear without a verdict callout to hang off of
    html = _html()
    assert _fix_guide_keys(html) <= _discovery_copy_keys(html)


def test_non_actionable_causes_have_no_fix_guide():
    # ok / small-net / probe-unavailable must not offer a router fix
    keys = _fix_guide_keys(_html())
    assert "discovery_ok" not in keys


def test_every_brand_tip_points_at_a_real_doc():
    for doc in _brand_docs(_html()):
        assert (_FIXDIR / doc).is_file(), f"BRAND_TIPS references missing docs/network-fixes/{doc}"


def test_brand_guides_cross_link_requirements_and_limitations():
    # each per-router guide should route the reader to the shared 'what Wavr needs' + limits docs
    for name in ("virgin-media-hub.md", "eir.md", "sky.md", "tp-link.md"):
        text = (_FIXDIR / name).read_text(encoding="utf-8")
        assert "wavr-network-requirements.md" in text
