"""The website, checked the way somebody arriving at it would be let down.

Tomorrow's first-user test begins on the Wavr website rather than in a build
folder, which makes the site a product surface with the same obligations as the
dashboard: it must not lie, it must not leak, it must not reach the internet,
and every promise it makes must lead somewhere.

These are the checks that can be made without a browser, so they run in the
ordinary suite and fail fast. The browser-level ones -- responsive layout,
focus order, real download bytes -- live with the portal's own tests, where a
server is already running.

## Why no external request is a test and not a convention

The site is served from the owner's own laptop tomorrow, to his own phones, and
one `<script src="https://...">` would mean a stranger's server sees every
device in the house connect. It would also break entirely if the house had no
internet, which is a state Wavr explicitly supports. A convention cannot catch
a CDN link somebody adds in good faith; a test can.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PUBLIC = REPO / "site" / "public"

pytestmark = pytest.mark.skipif(
    not PUBLIC.is_dir(), reason="the site is not in this checkout")


def pages() -> list[Path]:
    return sorted(PUBLIC.rglob("*.html"))


def text_of(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def test_there_is_a_site_to_check():
    """A guard on the guards: a glob that matches nothing passes everything."""
    assert pages(), "no HTML under site/public — every check below is vacuous"
    names = {p.name for p in pages()}
    assert "index.html" in names, sorted(names)


# -- Nothing may leave the machine -------------------------------------------

_EXTERNAL = re.compile(
    r'(?:src|href)\s*=\s*["\'](?:https?:)?//([^"\'/]+)', re.I)

# The only off-machine addresses allowed to APPEAR are ones a person clicks on
# purpose. They are links, never loaded resources, and the distinction is
# enforced below by checking the attribute that carries them.
#
# The list is deliberately short and deliberately boring: the source, and the
# two toolchains the build-from-source page tells a developer to install. A new
# entry should have to be argued for, because a link is also a place somebody
# elses server learns that a Wavr page was open.
_LINKABLE_HOSTS = {
    "github.com", "www.github.com", "raw.githubusercontent.com",
    "rustup.rs",        # the Rust toolchain, for building the desktop shell
    "platformio.org",   # the firmware toolchain, for building a sensor node
}
# Loopback is not off-machine at all: it is the Core this page is about.
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


@pytest.mark.parametrize("page", pages(), ids=lambda p: p.name)
def test_no_page_loads_anything_from_the_internet(page):
    """A stylesheet, script, font or image from somewhere else.

    Served on a home Wi-Fi to a family's phones, one CDN reference means a
    third party sees every one of those devices arrive. It also breaks the site
    completely in the state Wavr is built for -- a working LAN with no internet.
    """
    html = text_of(page)
    offenders = []
    for tag in re.finditer(r"<(script|link|img|iframe|video|audio|source)\b[^>]*>",
                           html, re.I):
        chunk = tag.group(0)
        for host in _EXTERNAL.findall(chunk):
            offenders.append(f"<{tag.group(1)}> -> {host}")
    assert not offenders, (
        f"{page.name} loads from outside this machine: {offenders}. "
        f"Self-host it: the site is served on a home network with no "
        f"guarantee of internet, to devices whose owner did not agree to "
        f"tell anybody they were there.")


@pytest.mark.parametrize("page", pages(), ids=lambda p: p.name)
def test_the_only_external_addresses_are_ones_a_person_clicks(page):
    html = text_of(page)
    for m in re.finditer(r'href\s*=\s*["\'](https?://[^"\']+)', html, re.I):
        host = m.group(1).split("//", 1)[1].split("/", 1)[0].lower()
        if host.split(":", 1)[0] in _LOCAL_HOSTS:
            continue
        assert host in _LINKABLE_HOSTS, (
            f"{page.name} links to {host}, which is not one of the places this "
            f"product deliberately points at ({sorted(_LINKABLE_HOSTS)})")


def test_nothing_smells_like_analytics():
    """Named because a tracker rarely arrives as an obvious `<script src>`; it
    arrives as an inline snippet somebody pasted."""
    # Bounded on the left so an innocent identifier cannot trip it: the first
    # version of this list flagged `createSvgTag(` for containing `gtag(`, and
    # a check that cries wolf is a check somebody deletes.
    banned = (r"googletagmanager", r"google-analytics", r"gtag\s*\(",
              r"fbq\s*\(", r"hotjar", r"mixpanel", r"segment\.com",
              r"plausible\.io", r"posthog", r"clarity\.ms", r"matomo")
    alvos = list(pages()) + sorted((PUBLIC / "assets").rglob("*.js"))
    for f in alvos:
        low = f.read_text(encoding="utf-8", errors="replace").lower()
        for pattern in banned:
            assert not re.search(pattern, low), (
                f"{f.name} matches {pattern!r} -- this site sends nothing "
                f"anywhere, and tomorrow it runs on a home network with no "
                f"internet at all")


# -- Every promise leads somewhere -------------------------------------------

@pytest.mark.parametrize("page", pages(), ids=lambda p: p.name)
def test_every_internal_link_and_asset_exists(page):
    """A dead link on the page somebody was told to start from.

    Checked against the filesystem rather than by fetching, so it fails in the
    ordinary suite instead of only when a server happens to be up.

    `/download/<name>` is the exception, and it is not a loophole. There is no
    `download/` directory: the portal serves those paths from the release
    candidate folder, by an explicit allowlist, so a filesystem check would call
    every working download a dead link. It is checked against the manifest
    instead, which is the stronger question anyway — not "is there a file at
    this path" but "is this a product the site is actually offering".
    """
    manifest_path = PUBLIC / "assets" / "releases.json"
    offered = {}
    if manifest_path.is_file():
        import json
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        for product in data.get("products", []):
            path = product.get("downloadPath")
            if path:
                offered[path] = product

    html = text_of(page)
    missing = []
    for m in re.finditer(r'(?:src|href)\s*=\s*["\']([^"\'#?]+)', html):
        ref = m.group(1).strip()
        if (not ref or ref.startswith(("http://", "https://", "//", "data:",
                                       "mailto:", "tel:", "#", "javascript:"))):
            continue
        if ref.startswith("/download/"):
            product = offered.get(ref)
            if product is None:
                missing.append(f"{ref} (no product in releases.json offers it)")
            elif not product.get("available", True):
                missing.append(f"{ref} (the manifest says it is not available)")
            continue
        target = (PUBLIC / ref.lstrip("/")) if ref.startswith("/") \
            else (page.parent / ref)
        if not target.exists():
            missing.append(ref)
    assert not missing, f"{page.name} points at files that are not there: {missing}"


@pytest.mark.parametrize("page", pages(), ids=lambda p: p.name)
def test_every_link_to_this_page_lands_somewhere(page):
    """A `#name` that matches no `id` scrolls nowhere and reports nothing.

    The neighbouring test skips fragments on purpose — it is asking about files
    — so nothing was asking this. It became worth asking when the document map
    on the Docs page turned its boxes into links: a drawing that leads somewhere
    beats one that does not, and a drawing that *appears* to lead somewhere and
    silently does nothing is worse than either.
    """
    html = text_of(page)
    ids = set(re.findall(r"""\bid\s*=\s*["']([^"']+)["']""", html))
    ids.update(re.findall(r"""\bname\s*=\s*["']([^"']+)["']""", html))
    quebrados = sorted({
        m.group(1) for m in re.finditer(r"""\bhref\s*=\s*["']#([^"']+)["']""", html)
        if m.group(1) not in ids})
    assert not quebrados, (
        f"{page.name} links to fragments that do not exist on it: {quebrados}")


# -- It has to be readable by somebody not using a mouse ---------------------

@pytest.mark.parametrize("page", pages(), ids=lambda p: p.name)
def test_every_image_says_what_it_is(page):
    """`alt` missing entirely is a screen reader reading a filename aloud.
    `alt=""` is allowed and means "decorative", which is a decision; the
    absence of the attribute is an omission."""
    html = text_of(page)
    bad = [t.group(0)[:90] for t in re.finditer(r"<img\b[^>]*>", html, re.I)
           if not re.search(r"\balt\s*=", t.group(0), re.I)]
    assert not bad, f"{page.name} has images with no alt attribute: {bad}"


@pytest.mark.parametrize("page", pages(), ids=lambda p: p.name)
def test_the_page_has_one_h1_and_a_language(page):
    html = text_of(page)
    assert re.search(r"<html[^>]+\blang\s*=", html, re.I), (
        f"{page.name} does not declare a language, so a screen reader guesses "
        f"the pronunciation of every word on it")
    h1s = re.findall(r"<h1\b", html, re.I)
    assert len(h1s) == 1, f"{page.name} has {len(h1s)} <h1>, expected exactly 1"


@pytest.mark.parametrize("page", pages(), ids=lambda p: p.name)
def test_the_page_has_landmarks_and_a_title(page):
    html = text_of(page)
    assert re.search(r"<main\b", html, re.I), (
        f"{page.name} has no <main>, so 'skip to content' has nothing to skip to")
    title = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    assert title and title.group(1).strip(), f"{page.name} has no title text"


# -- It must not claim what the product cannot do ----------------------------

def test_no_invented_social_proof():
    """There is exactly one Wavr user, and tomorrow is the first time they use
    it. A testimonial, a logo wall or a user count would each be a lie told to
    the only person who would read it."""
    banned = [
        "trusted by", "customers", "testimonial", "5 stars", "★★★★★",
        "join thousands", "users worldwide", "loved by", "as seen in",
        "case study", "our clients",
    ]
    for page in pages():
        low = text_of(page).lower()
        for phrase in banned:
            assert phrase not in low, (
                f"{page.name} contains {phrase!r}. Wavr has no users yet; "
                f"tomorrow is the first one.")


def test_the_site_says_it_is_a_pre_release():
    """Somebody about to install an unsigned build on their own laptop is owed
    that fact before they click, not after Windows tells them."""
    html = " ".join(text_of(p).lower() for p in pages())
    honest = ("pre-release", "prerelease", "release candidate", "pré-lançamento",
              "not signed", "unsigned", "not code-signed", "unknown publisher")
    assert any(word in html for word in honest), (
        "no page prepares the visitor for an unsigned pre-release build; "
        "Windows will be the first thing that tells them")


# -- The site and the product must use the same words ------------------------

def test_the_site_does_not_call_a_space_a_house():
    """A Space may be a shop, an office or a warehouse. The product was moved
    off "house" deliberately; a site that says it teaches the wrong word before
    the product gets a chance to use the right one."""
    # USE, not mention. The site is allowed -- and right -- to write
    # Built around a Space, not just "your house"
    # because that sentence exists precisely to reject the assumption. A
    # quoted occurrence is the product talking ABOUT the word; an unquoted one
    # is the product using it as the description. The first version of this
    # check failed that heading, which would have pushed somebody to delete
    # the clearest sentence on the page.
    for page in pages():
        low = text_of(page).lower()
        for phrase in ("your house", "the house", "house map", "your home's rooms"):
            for m in re.finditer(re.escape(phrase), low):
                antes = low[max(0, m.start() - 3):m.start()]
                depois = low[m.end():m.end() + 3]
                if any(c in antes for c in '"“‘') and                    any(c in depois for c in '"”’'):
                    continue        # quoted: the site is naming the assumption to reject it
                raise AssertionError(
                    f"{page.name} says {phrase!r} as its own description. "
                    f"A Space is not always a home: ...{low[max(0,m.start()-60):m.end()+60]}...")


def test_the_site_uses_the_products_own_vocabulary():
    """If the site teaches a word, the app has to answer to it.

    Only checks words the site actually chose to use, so it cannot fail for a
    concept the site legitimately never mentions.
    """
    shell = (REPO / "frontend" / "index.html").read_text(
        encoding="utf-8", errors="replace").lower()
    site = " ".join(text_of(p).lower() for p in pages())

    # site phrase -> the word the product must also use for the same thing
    pairs = {
        "needs attention": "needs attention",
        "trust & privacy": "privacy",
    }
    for phrase, must_exist in pairs.items():
        if phrase in site:
            assert must_exist in shell, (
                f"the site says {phrase!r} and the product never says "
                f"{must_exist!r}, so the two disagree about what to call it")


# -- What the visitor will actually get, said before they download -----------

def test_the_site_prepares_you_for_a_first_run_with_no_sensors():
    """The screenshots show a Space with a radar and a motion sensor in it.

    A fresh install has neither. On day one the network source is the only one
    running — BLE registers only once there is a known device, a camera is
    opt-in and starts off, and radar needs a serial port — so every room says
    "No sensor covers this room" and the dashboard looks nothing like the hero
    image. Somebody should learn that from the site, before installing, and not
    from the product ten minutes later.

    Checked against the sentence the PRODUCT shows, read out of the frontend,
    so the site cannot drift into describing the empty state in words the app
    never uses.
    """
    empty_room = (REPO / "frontend" / "js" / "render.js").read_text(
        encoding="utf-8", errors="replace")
    m = re.search(r'WavrT\("(No sensor covers this room[^"]*)"\)', empty_room)
    assert m, ("the product no longer says 'No sensor covers this room' — find "
               "what it says now and teach this test and the site the new words")
    sentence = m.group(1).split(".")[0]        # the claim, without the advice

    site = " ".join(text_of(p) for p in pages())
    assert sentence in site, (
        f"no page tells the visitor that a fresh install has no sensors in it. "
        f"The product's own words for that state are {sentence!r}; the site "
        f"shows a fully-equipped Space and says nothing about the difference.")


@pytest.mark.parametrize("page", pages(), ids=lambda p: p.name)
def test_a_product_screenshot_comes_with_the_day_one_caveat(page):
    """A page that shows the dashboard has to point at what day one looks like.

    Not because the screenshots are dishonest — they are real product, not a
    mockup — but because "real" and "what you will have in ten minutes" are
    different claims, and only one of them is true. The hero caption used to
    read "this is what you'll see once Wavr is running", which was the false
    one, on the page tomorrow's first user starts from.
    """
    html = text_of(page)
    if "assets/shots/" not in html:
        pytest.skip("no product screenshot on this page")
    if page.name == "download.html":
        return                     # this page IS where the caveat lives
    assert "download.html#day-one" in html, (
        f"{page.name} shows the product but never links to what a fresh "
        f"install actually looks like")
