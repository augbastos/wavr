"""Wavr device-type taxonomy + local-only classifier.

THE fixed 18-value device-type taxonomy every identity surface (backend recog,
/api/inventory, frontend icons) speaks. Values are Wavr's own (generic industry
terms), NOT copied from any third-party product's enum/label table.

Modeling calls (deliberate, see redesign2-deviceid spec):
- ``router`` = network infrastructure (internet router, mesh node, AP, switch);
  ``gateway`` = a smart-home protocol bridge (Zigbee/Thread/Matter hub) -- a
  different real-world object the privacy dashboard cares about distinctly.
- ``esp_dev`` = raw ESP32/ESP8266 DIY silicon (any firmware); ``iot_sensor`` =
  a branded purpose-built sensor accessory, identified by hostname not chip.

``hostname_type`` is the pure, LOCAL-ONLY hostname-regex tier (first match
wins). The full multi-signal fusion cascade -- hostname > open-port hint >
vendor default > heuristics, each with an honest confidence -- lives in
``wavr.recog.recognize``, which imports these tables/patterns directly; there
is no separate classify_device path in production.
"""
from __future__ import annotations

import re

# The fixed taxonomy. Frontend icon maps and the user type-pin API validate
# against exactly this set -- do not add values casually (each needs an icon).
DEVICE_TYPES: tuple[str, ...] = (
    "router", "gateway", "phone", "tablet", "laptop", "desktop", "tv",
    "streaming_stick", "speaker", "camera", "printer", "nas", "console",
    "iot_sensor", "esp_dev", "smart_plug", "wearable", "unknown",
)

CONFIDENCE_LEVELS: tuple[str, ...] = ("low", "medium", "high")


def _p(pattern: str, dtype: str) -> tuple[re.Pattern, str]:
    return re.compile(pattern, re.IGNORECASE), dtype


# Ordered hostname regex -> device_type. First match wins, so specific
# patterns sit above generic ones (e.g. ``nintendo-switch`` -> console is
# matched long before any generic word could misfire; ``tapo-c2xx`` -> camera
# beats ``tapo-p1xx`` -> smart_plug via the model-letter). Regex (not bare
# substring) precisely so words like "switch" can be scoped safely.
HOSTNAME_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    _p(r"iphone|android|pixel|galaxy-?s\d|galaxy-?a\d", "phone"),
    _p(r"ipad|galaxy-?tab|tablet\b", "tablet"),
    _p(r"macbook|laptop|thinkpad|notebook", "laptop"),
    _p(r"imac|desktop|pc-|optiplex", "desktop"),
    _p(r"\btv\b|bravia|webos|tizen", "tv"),
    _p(r"chromecast|fire-?tv|apple-?tv|shield|roku", "streaming_stick"),
    _p(r"\becho\b|alexa|\bhomepod\b|\bsonos\b", "speaker"),
    _p(r"hikvision|dahua|reolink|\bnvr\b|\bdvr\b|\b(?:web|ip)?cam(?:era)?\b|tapo-?c\d", "camera"),
    _p(r"deskjet|officejet|laserjet|\bepson\b|\bcanon\b|\bprinter\b", "printer"),
    _p(r"synology|\bqnap\b|\bnas\b", "nas"),
    _p(r"playstation|\bps[45]\b|\bxbox\b|nintendo-?switch", "console"),
    _p(r"aqara-?hub|smartthings-?hub|hue-?bridge|wink-?hub", "gateway"),
    _p(r"\bdeco\b|\barcher\b|\beero\b|\bunifi\b|\budm\b|omada|\brouter\b", "router"),
    _p(r"tapo-?[ps]\d|smart-?plug|wemo", "smart_plug"),
    _p(r"esp32|esp8266|esphome|espresense|tasmota", "esp_dev"),
    _p(r"\bsensor\b|\bmotion\b|\bcontact\b|\bmi-?jia\b", "iot_sensor"),
    _p(r"mi-?band|fitbit|\bwatch\b|galaxy-?watch", "wearable"),
)


def hostname_type(hostname: str | None) -> str | None:
    """First HOSTNAME_PATTERNS match for a hostname, or None. Pure/offline."""
    if not hostname:
        return None
    low = hostname.lower()
    for pattern, dtype in HOSTNAME_PATTERNS:
        if pattern.search(low):
            return dtype
    return None


# Collapses one-or-more hyphen/underscore separators to a single space --
# ``display_hostname``'s prettify step.
_SEP_RUN_RE = re.compile(r"[-_]+")


def display_hostname(hostname: str | None) -> str | None:
    """A human-friendly DISPLAY label derived from a raw hostname, or None.
    Pure/offline; never mutates/replaces the raw hostname anywhere else --
    callers project this alongside it (e.g. as a NEW `display_name` view
    field), never in place of it, because `hostname_type` above (and any
    other classifier keyed on the full raw string) must keep seeing the
    original.

    Two-step, explicit and orderable (no ML, no guessing):
    1. Strip the DHCP/router search-domain suffix a PTR lookup adds -- keep
       only the first DNS label. A device's own name effectively never
       contains a dot; the tail is always the router's domain (e.g.
       "Xiaomi-12T-Pto.vodafone.ultrahub" -> "Xiaomi-12T-Pto").
    2. Prettify: collapse hyphen/underscore runs to a single space and trim.
       Each resulting token is Title-cased ONLY when it is a plain word --
       purely alphabetic AND uniformly cased (all-lower or all-upper). A
       token that already mixes case (e.g. "Pto", "iPhone") or carries a
       digit (e.g. "12T", "8Gen1", "C210") is a model/brand token and is
       left completely untouched -- never lowercased, never split.

    Returns None when there is nothing left to show (empty/whitespace-only
    input)."""
    if not isinstance(hostname, str) or not hostname.strip():
        return None
    label = hostname.split(".", 1)[0]
    label = _SEP_RUN_RE.sub(" ", label).strip()
    if not label:
        return None
    tokens = []
    for tok in label.split(" "):
        if tok.isalpha() and (tok.islower() or tok.isupper()):
            tokens.append(tok[:1].upper() + tok[1:].lower())
        else:
            tokens.append(tok)   # mixed-case/digit model token -- preserved as-is
    return " ".join(tokens)
