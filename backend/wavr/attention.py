"""The things that need a person, in one place, ranked.

## Why this exists

Wavr can already tell you a camera is offline, that a phone wants to pair, that
a device appeared on the network and that a room lost its count. It tells you
each of those in a different screen, and a household is expected to visit six
places to find out whether anything is waiting for them.

Nobody does that. So the honest state of the product today is that actionable
conditions accumulate silently until somebody happens to open the right tab —
which is the same class of failure as a Core that died and never said so.

One list. Ranked. Every item names what happened, what it costs, and where to
go. If nothing is here, nothing needs a person, and that sentence has to be
trustworthy or the whole surface is noise.

## What belongs here, and what does not

**Actionable.** Something a person can DO changes it. "Kitchen radar is
offline" belongs; "the kitchen has no position-capable sensor" is a fact about
the Space, not a task, and it belongs on the coverage screen.

**Not a log.** An event that has already been handled leaves. An alert that
fires every thirty seconds appears once, as one item, with its count.

**Not a duplicate of severity.** A DEGRADED runtime state is a state, and it is
shown as a state. It appears here only when there is a specific thing to do
about it.

The rule that keeps this list short: **an item nobody can act on is a bug in
this module, not an item.** The moment this becomes a feed, people stop reading
it, and the one time it matters they will not look.

## Ranking

Blocking first, then things degrading what Wavr can do, then things that are
merely worth knowing. Within a band, the oldest first: a request that has been
waiting three days is more urgent than one from a minute ago, and sorting by
newest — the default everywhere — would bury it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from wavr.alert_severity import SEVERITY_ALERT, SEVERITY_CRITICAL

# Bands, worst first. The number is the sort key and the API contract; the name
# is what a person is shown.
BLOCKING = "blocking"       # Wavr cannot do its job until somebody acts
DEGRADED = "degraded"       # it works, less well, and a person can fix it
INFO = "info"               # worth knowing, nothing is broken

BANDS = (BLOCKING, DEGRADED, INFO)

# Read from the one alert ladder rather than spelled out here, so this cannot
# drift from the producers again. Everything at `alert` or above is something a
# person is expected to do something about; `watch` and below is ambient.
ACTIONABLE_SEVERITIES = frozenset({SEVERITY_ALERT, SEVERITY_CRITICAL})

# -- The wording, as templates -------------------------------------------------
#
# Written out as named constants rather than inline at the call sites, so that
# one tuple at the bottom of this file can be the whole of what this surface
# says, and a test can require a translation for every line of it.
#
# A slot is `{like_this}` and its value is the household's own word — a camera's
# name, a room's name, a phone's name. Those values NEVER become part of a
# lookup key; see `Item`.
#
# Where a sentence needs a room and might not have one, there are two whole
# sentences rather than one sentence and a fallback word. Substituting an
# English "this room" into a Portuguese sentence is the same mistake in a
# smaller font, and Portuguese agrees gender around the noun besides.
PAIRING_TITLE = "{name} wants to join"
PAIRING_DETAIL = "It cannot do anything until you approve or deny it."
NODE_TITLE = "{label} is asking to be enrolled"
NODE_DETAIL = "A sensor board is waiting to be let in."
SENSOR_TITLE_IN_ROOM = "{sensor} in {room} is not reporting"
SENSOR_TITLE = "{sensor} is not reporting"
COST_POSITION_IN_ROOM = ("While it is down, Wavr can no longer place people "
                         "within {room} — presence and count may still work "
                         "from other sensors.")
COST_POSITION = ("While it is down, Wavr can no longer place people within "
                 "this room — presence and count may still work from other "
                 "sensors.")
COST_COUNT_IN_ROOM = ("While it is down, Wavr may not be able to count people "
                      "in {room}.")
COST_COUNT = "While it is down, Wavr may not be able to count people in here."
COST_EVIDENCE_IN_ROOM = "While it is down, Wavr has less evidence about {room}."
COST_EVIDENCE = "While it is down, Wavr has less evidence about this room."
CAMERA_URL_TITLE = "{name} needs its stream address"
CAMERA_URL_DETAIL = ("A camera's address carries its password, so it is never "
                     "part of a backup. Enter it again and this camera starts "
                     "watching.")
DISCOVERY_TITLE = "Wavr noticed something"
DISCOVERY_SEEN_IN = "Seen in {room}."
DISCOVERY_DETAIL = "Wavr noticed this and would like you to decide."
UPDATE_TITLE = "An update is available"
UPDATE_DETAIL = ("Wavr never updates itself; this is how this install "
                 "updates.")

# The word on the button. Rendered through the catalogue like any other, and
# listed here for the same reason: an action nobody translated is an English
# verb in the middle of a Portuguese row.
ACTION_REVIEW = "Review"
ACTION_FIX = "Fix"
ACTION_ADD_ADDRESS = "Add address"
ACTION_VIEW = "View"
ACTION_HOW = "How"


@dataclass(frozen=True)
class Item:
    """One thing waiting for a person.

    `where` is a UI target rather than a URL: the same item is rendered by the
    dashboard, and one day by a phone and a panel, and a hard-coded path would
    be wrong in two of the three.

    ## Why the wording is a template and never a finished sentence

    `summarise()` at the bottom of this file documents this defect for the
    headline. The ROWS had it too, and worse. The dashboard renders a row with
    `WavrT(title)`, so the catalogue key was the composed sentence — "hall-cam
    needs its stream address", "Seen in sala". A key with the household's own
    words in it can never be in any catalogue, so every row of this inbox
    rendered in English whatever the language was set to, and each miss wrote a
    private name into the missing-translation audit.

    Nothing went red, because an unmatched lookup falls back to correct English.
    The one test that could see it walks the landing screen, so it only noticed
    on the runs where such a row happened to be on screen.

    `title` and `detail` still compose the English. `wavr status` and the tray
    read those and translate nothing; handing either a literal `{name}` would
    be the same bug pointing the other way.

    `*_text` is not a variant of the same thing. It is wording this module did
    not write — a discovery card's own title, an install's update instructions
    — and it is DATA: rendered verbatim, never looked up, because looking it up
    is the leak.

    `evidence` is the producer's specifics ("ip: 10.0.0.9"). It sits outside
    the translated sentence, the way the house-status tile already keeps a
    vendor string outside its own.
    """
    key: str
    band: str
    # Keyword-only past the band, for the reason `runtime_status.Finding` gives:
    # the old signature's 4th slot was a detail STRING and this one's is an args
    # dict, so a call written against the old shape would fill the wrong field
    # and only fail later, inside a render.
    title_template: str = field(default="", kw_only=True)
    title_args: dict = field(default_factory=dict, kw_only=True)
    title_text: str = field(default="", kw_only=True)
    detail_template: str = field(default="", kw_only=True)
    detail_args: dict = field(default_factory=dict, kw_only=True)
    detail_text: str = field(default="", kw_only=True)
    evidence: str = field(default="", kw_only=True)
    where: str = field(default="", kw_only=True)
    action: str = field(default="", kw_only=True)
    since: str = field(default="", kw_only=True)
    count: int = field(default=1, kw_only=True)

    @property
    def title(self) -> str:
        if self.title_template:
            return self.title_template.format(**self.title_args)
        return self.title_text

    @property
    def detail(self) -> str:
        if self.detail_template:
            body = self.detail_template.format(**self.detail_args)
        else:
            body = self.detail_text
        if self.evidence:
            return f"{body} ({self.evidence})".strip()
        return body

    def to_dict(self) -> dict:
        return {"key": self.key, "band": self.band, "title": self.title,
                "detail": self.detail, "where": self.where,
                "action": self.action, "since": self.since,
                "count": self.count,
                "title_template": self.title_template,
                "title_args": dict(self.title_args),
                "title_text": self.title_text,
                "detail_template": self.detail_template,
                "detail_args": dict(self.detail_args),
                "detail_text": self.detail_text,
                "evidence": self.evidence}


def _oldest_first(items):
    # Missing timestamps sort last rather than first: an item with no `since` is
    # not "from 1970", and putting it at the top would be an invented urgency.
    return sorted(items, key=lambda i: (BANDS.index(i.band), i.since or "9999"))


def collect(*, discoveries=(), pending_pairings=(), node_requests=(),
            coverage_rows=(), cameras_needing_url=(), alerts=(),
            update=None, now=None) -> list[Item]:
    """Everything waiting, from facts other modules already established.

    Every argument is optional and an absent one contributes nothing — a caller
    that cannot read pending pairings must not cause "nothing needs you" to be
    printed over three waiting requests, so a failure upstream should leave the
    argument out and the caller should say it could not check.
    """
    items: list[Item] = []

    # -- Somebody is waiting on a decision -----------------------------------
    for p in pending_pairings or ():
        # `requester_name` and `created_at` are what `PairApprovalManager
        # .list_pending()` actually emits. An earlier draft of this guessed
        # `device_name`/`created_ts` and would have rendered every request as
        # "A device wants to join" with no timestamp, which sorts wrong AND
        # reads wrong — a consumer expecting a shape the producer never
        # produces is the commonest bug in this codebase, so the field names
        # here are copied from the producer rather than assumed.
        name = str(p.get("requester_name") or "A device")
        items.append(Item(
            key=f"pairing:{p.get('request_id') or name}",
            band=BLOCKING,
            title_template=PAIRING_TITLE, title_args={"name": name},
            detail_template=PAIRING_DETAIL,
            where="devices", action=ACTION_REVIEW,
            since=str(p.get("created_at") or "")))

    for n in node_requests or ():
        label = str(n.get("label") or n.get("node_id") or "A sensor node")
        items.append(Item(
            key=f"node:{n.get('request_id') or label}",
            band=BLOCKING,
            title_template=NODE_TITLE, title_args={"label": label},
            detail_template=NODE_DETAIL,
            where="devices", action=ACTION_REVIEW,
            since=str(n.get("created_ts") or "")))

    # -- Something Wavr can see is not working --------------------------------
    offline = [r for r in (coverage_rows or ())
               if str(r.get("health")) in ("offline", "silent", "failed")]
    for r in offline:
        room = str(r.get("room") or "")
        cost_template, cost_args = _what_it_costs(r)
        items.append(Item(
            key=f"sensor:{r.get('sensor_id')}",
            band=DEGRADED,
            title_template=SENSOR_TITLE_IN_ROOM if room else SENSOR_TITLE,
            title_args=({"sensor": _sensor_name(r), "room": room} if room
                        else {"sensor": _sensor_name(r)}),
            detail_template=cost_template, detail_args=cost_args,
            where="coverage", action=ACTION_FIX,
            # `SensorCoverage.to_dict()` emits no timestamp at all — this read
            # `last_seen`, which that producer has never had, so every sensor
            # item sorted as undated and a three-day outage ranked level with a
            # one-minute one. The `detail` dict is where a row carries anything
            # extra, so that is where a `since` would come from if one existed.
            since=str((r.get("detail") or {}).get("since") or "")))

    for c in cameras_needing_url or ():
        name = str(c.get("name") or "A camera")
        items.append(Item(
            key=f"camera-url:{name}",
            band=DEGRADED,
            title_template=CAMERA_URL_TITLE, title_args={"name": name},
            detail_template=CAMERA_URL_DETAIL,
            where="devices", action=ACTION_ADD_ADDRESS))

    # -- Something appeared -----------------------------------------------------
    for d in discoveries or ():
        if str(d.get("status") or "pending") != "pending":
            continue
        # A discovery card's title is written by `discovery_feed`, which composes
        # it from what is on the network. This module did not write it and must
        # not pretend it can translate it: it goes through as TEXT. The card's
        # own producer carries a template alongside it where it has one, and
        # that is preferred when present.
        own = d.get("detail") if isinstance(d.get("detail"), dict) else {}
        card_template = str(own.get("title_template") or "")
        card_args = own.get("title_args") if isinstance(own.get("title_args"), dict) else {}
        card_text = str(d.get("title") or "")
        detail_template, detail_args = _discovery_detail(d)
        items.append(Item(
            key=f"discovery:{d.get('discovery_id')}",
            band=INFO,
            title_template=card_template or ("" if card_text else DISCOVERY_TITLE),
            title_args=dict(card_args) if card_template else {},
            title_text="" if card_template else card_text,
            detail_template=detail_template, detail_args=detail_args,
            where="discoveries", action=ACTION_REVIEW,
            since=str(d.get("first_seen") or "")))

    # -- Alerts, collapsed --------------------------------------------------
    #
    # An alert that fires every thirty seconds is ONE thing wrong, not eighty
    # things to read. Grouped by kind, with the count, and only the severities a
    # person is expected to do something about.
    # The ladder is info < note < watch < alert < critical (`alert_severity`).
    # This filtered on "high", which is not a tier and which no producer emits —
    # so a rogue DHCP server, a gateway-identity change and every fall or
    # intrusion alert (all "alert") were dropped, and only a SUSTAINED gateway
    # change ever reached the list. The tray had it right, which is how the two
    # surfaces disagreed about the same event.
    grouped: dict[str, list] = {}
    for a in alerts or ():
        if str(a.get("severity") or "").lower() not in ACTIONABLE_SEVERITIES:
            continue
        grouped.setdefault(str(a.get("kind") or "alert"), []).append(a)
    for kind, group in grouped.items():
        first = group[0]
        # The producers emit `kind`, `severity` and their own facts — none of
        # them carries `title` or `detail`, so reading those gave a generated
        # "Rogue dhcp" with nothing under it. The sentence belongs here, where
        # the audience is a person, rather than in a monitor that is also read
        # by MQTT and the alert log.
        title, detail, evidence = _alert_words(kind, first)
        items.append(Item(
            key=f"alert:{kind}",
            band=DEGRADED,
            # A kind with no words of its own falls back to a readable form of
            # the kind itself — an identifier, so it goes through as text.
            title_template=title if kind in _ALERT_WORDS else "",
            title_text="" if kind in _ALERT_WORDS else title,
            detail_template=detail,
            evidence=evidence,
            where="alerts", action=ACTION_VIEW,
            since=str(min((str(a.get("ts") or "") for a in group), default="")),
            count=len(group)))

    if update and update.get("behind") is True:
        # The instructions come from the install, not from Wavr: whoever
        # packaged this build wrote them, in whatever language they wrote them
        # in. Text, not a lookup.
        instructions = str(update.get("instructions") or "")
        items.append(Item(
            key="update",
            band=INFO,
            title_template=UPDATE_TITLE,
            detail_template="" if instructions else UPDATE_DETAIL,
            detail_text=instructions,
            where="system", action=ACTION_HOW))

    return _oldest_first(items)


# What each alert kind means, for somebody who is not an engineer. Keyed on the
# `kind` the producers actually emit; anything unknown falls back to a readable
# form of the kind itself rather than to an empty row.
_ALERT_WORDS = {
    "rogue_dhcp": (
        "Another device is handing out network addresses",
        "That is normally only your router. It can be a second router somebody "
        "plugged in — or something pretending to be one."),
    "gateway_identity": (
        "Your router's hardware address changed",
        "Either the router was replaced, or something on the network is "
        "answering in its place."),
    "rogue_device": (
        "A device Wavr does not recognise joined the network",
        "New to this network. Worth a look if you were not expecting it."),
    "intrusion": (
        # Not "in the house": a Space is as often a shop, an office or a
        # clinic, and this sentence was one the house-to-Space sweep could not
        # see, because it had never been declared for translation at all.
        "Somebody Wavr does not recognise is here",
        "Presence was detected that does not match anybody you have added."),
    "fall_suspected": (
        "A possible fall was detected",
        "Movement stopped abruptly and did not resume. Wavr is not a medical "
        "device and can be wrong."),
}


def _alert_words(kind: str, alert: dict) -> tuple[str, str, str]:
    """The sentence, and separately the facts behind it.

    Returned apart rather than joined, because the sentence is translated and
    the facts are not. Concatenating them produced a key with an IP address in
    it, which no catalogue can hold, so the whole row stayed English.
    """
    title, detail = _ALERT_WORDS.get(
        kind, (kind.replace("_", " ").capitalize(), ""))
    # The producer's own specifics, beside the sentence rather than replacing
    # it: "10.0.0.9" alone is not an errand, and the sentence alone is not
    # evidence.
    evidence = ""
    for name in ("extra_server", "observed_mac", "ip", "mac"):
        value = alert.get(name)
        if value:
            evidence = f"{name.replace('_', ' ')}: {value}"
            break
    return title, detail, evidence


def _sensor_name(row) -> str:
    """What a person calls it, never the id.

    `sensor_id` is very often the name an operator typed — which is exactly what
    the experience layer had to stop exposing. Here the audience IS the
    operator, so their own words are the right thing to show; this exists so
    that decision is deliberate rather than accidental.
    """
    modality = str(row.get("modality") or "sensor")
    name = str(row.get("sensor_id") or "").strip()
    return name or modality


def _what_it_costs(row) -> tuple[str, dict]:
    """What the household loses while this stays broken.

    "Offline" is a status. "You can no longer count people in the kitchen" is
    the reason to get up and fix it, and it is the sentence Wavr is uniquely
    able to write because it knows what each sensor was contributing.

    Six sentences rather than three with a fallback word: the no-room versions
    used to substitute the English "this room" into the slot, which reads as
    an English phrase dropped into the middle of a Portuguese sentence.
    """
    level = str(row.get("precision_level") or "")
    room = str(row.get("room") or "")
    if level == "position":
        return ((COST_POSITION_IN_ROOM, {"room": room}) if room
                else (COST_POSITION, {}))
    if level == "count":
        return ((COST_COUNT_IN_ROOM, {"room": room}) if room
                else (COST_COUNT, {}))
    return ((COST_EVIDENCE_IN_ROOM, {"room": room}) if room
            else (COST_EVIDENCE, {}))


def _discovery_detail(d) -> tuple[str, dict]:
    detail = d.get("detail")
    if isinstance(detail, dict):
        room = detail.get("room") or detail.get("suggested_room")
        if room:
            return DISCOVERY_SEEN_IN, {"room": str(room)}
    return DISCOVERY_DETAIL, {}


def summarise(items) -> dict:
    """The shape a chip, a tray line or a badge renders.

    One place, so a count on the tray and a count in the header cannot disagree
    — and so "nothing needs you" is produced by the same code that produces
    "three things need you", rather than by a separate emptiness check that can
    drift from it.
    """
    items = list(items or ())
    by_band = {b: sum(1 for i in items if i.band == b) for b in BANDS}
    total = len(items)
    if not total:
        headline = "Nothing needs your attention"
    elif total == 1:
        headline = "1 thing needs your attention"
    else:
        headline = f"{total} things need your attention"
    return {
        "total": total,
        "blocking": by_band[BLOCKING],
        "degraded": by_band[DEGRADED],
        "info": by_band[INFO],
        "headline": headline,
        # The same sentence with the NUMBER as a slot, and a plural pipe.
        #
        # The frontend called `WavrT(headline)` on the composed string, so it
        # looked up "3 things need your attention" — a key that can never be in
        # a catalogue, because the number is part of it. The chip stayed
        # English at every count except zero and one, and nothing could see it:
        # an unmatched lookup falls back to correct English, which is what
        # makes this design safe and also what makes this particular failure
        # silent.
        #
        # `headline` is unchanged, for `wavr status` and the tray — neither
        # translates anything, and both would render a literal `{n}`.
        "headline_template": ("Nothing needs your attention" if not total else
                              "{n} thing needs your attention"
                              "|{n} things need your attention"),
        "items": [i.to_dict() for i in items],
    }


# Every sentence and every button word this module can put in front of a
# person, in one tuple, built from the constants the code above actually uses.
#
# Two tests read it. One requires a catalogue entry for each line, which is the
# check that was missing: nothing anywhere asserted that a sentence composed in
# Python and translated in the browser had a translation, so the whole of this
# inbox was English in Portuguese and no gate went red. The other drives
# `collect()` over one of every kind of item and requires every template it
# emits to be listed here, so a row added with an inline literal fails instead
# of quietly shipping.
#
# Assembled at the bottom of the file because `_ALERT_WORDS` is defined below
# `collect`, and flattening it here is what keeps the alert vocabulary from
# being a second, unchecked list of sentences.
TEMPLATES: tuple[str, ...] = tuple(sorted({
    PAIRING_TITLE, PAIRING_DETAIL,
    NODE_TITLE, NODE_DETAIL,
    SENSOR_TITLE_IN_ROOM, SENSOR_TITLE,
    COST_POSITION_IN_ROOM, COST_POSITION,
    COST_COUNT_IN_ROOM, COST_COUNT,
    COST_EVIDENCE_IN_ROOM, COST_EVIDENCE,
    CAMERA_URL_TITLE, CAMERA_URL_DETAIL,
    DISCOVERY_TITLE, DISCOVERY_SEEN_IN, DISCOVERY_DETAIL,
    UPDATE_TITLE, UPDATE_DETAIL,
    ACTION_REVIEW, ACTION_FIX, ACTION_ADD_ADDRESS, ACTION_VIEW, ACTION_HOW,
    *(t for pair in _ALERT_WORDS.values() for t in pair if t),
}))
