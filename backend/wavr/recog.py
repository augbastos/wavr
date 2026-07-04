"""Wavr recognition fusion -- local, explainable device identification.

``recognize(signals)`` merges every available per-device signal into ONE
DeviceIdentity using PRECEDENCE (strongest opinion wins), not averaging:

    user_pin  >  protocol self-description (UPnP > Bonjour > SNMP)
              >  DHCP fingerprint  >  hostname pattern  >  open-port hint
              >  OUI vendor default  >  mobile-heavy vendor
              >  randomized-MAC heuristic

Collector threat model (mDNS/SSDP/SNMP): every one of these protocols is a
device SELF-description broadcast on the open LAN multicast group -- any
host can announce whatever TXT/SRV/LOC-XML content it likes, the same
spoofability the M1 OUI-alone fix already treats as a security-relevant
fact (a MAC's OUI is just its first 3 octets, freely settable). So a
protocol self-description signal ALONE is capped at "medium" confidence
here, exactly like an OUI-alone verdict -- it only reaches "high" via the
same consensus bump (a 2nd independent signal agreeing on the same type).

Confidence is the winning signal's own confidence, bumped ONE level when a
second independent signal agrees on the same type -- the same
"consensus-raises-confidence" ethos Wavr's sensor fusion uses. The full
evidence trail is returned in ``sources`` so the UI can explain WHY.

100% LOCAL: pure functions, zero network I/O, public-data heuristics only
(IEEE OUI prefixes, IANA port conventions, hostname conventions). There is no
cloud recognition catalog and never will be -- that is the product's pitch.

Signal keys accepted (all optional -- future passive collectors just add
their key, no engine change needed):
    mac        str  -- for the randomized-MAC heuristic
    vendor     str  -- OUI-resolved manufacturer ("unknown" is fine)
    hostname   str  -- DHCP/NetBIOS/mDNS announced name
    open_ports iterable[int] -- from the OPT-IN connect-only port pass
    user_pin   str  -- taxonomy value the OWNER pinned; always wins
    upnp | bonjour | snmp  dict -- protocol SELF-description hooks (future
               collectors): {"device_type": taxonomy?, "make": str?,
               "model": str?, "os": str?}
    dhcp       dict -- DHCP fingerprint hook: {"device_type": taxonomy?,
               "os": str?}
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from wavr.data.deviceclass import DEVICE_TYPES, hostname_type
from wavr.data.oui import (
    MOBILE_HEAVY_VENDORS,
    VENDOR_DEFAULT_TYPE,
    is_locally_administered,
)
from wavr.data.ports import port_type_hint

# Precedence as explicit weights (documented ordering above). Weights are for
# RANKING + the explainability trail -- the merge is precedence, not a sum.
_WEIGHTS: dict[str, float] = {
    "user_pin": 1.0,
    "upnp": 0.9,
    "bonjour": 0.85,
    "snmp": 0.8,
    "dhcp": 0.7,
    "hostname": 0.65,
    "port_hint": 0.5,
    "oui": 0.4,
    "mobile_vendor": 0.3,
    "random_mac": 0.2,
}

_CONF_ORDER = ("low", "medium", "high")

# Defensive bound on any make/model/os string a future self-describing collector
# (upnp/bonjour/snmp/dhcp) hands us -- mirrors housemap.py's MAX_STR_LEN convention.
# recog never raises on malformed input (see _valid_type), so an oversized value is
# truncated rather than rejected.
_MAX_FIELD_LEN = 200


@dataclass(frozen=True)
class DeviceIdentity:
    """The fused identity of one LAN device. ``sources`` is the evidence
    trail: one {"signal", "value", "weight"} dict per contributing signal,
    strongest first (poor-man's explainable confidence)."""
    device_type: str = "unknown"
    confidence: str = "low"
    make: str | None = None
    model: str | None = None
    os: str | None = None
    sources: tuple = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "device_type": self.device_type,
            "confidence": self.confidence,
            "make": self.make,
            "model": self.model,
            "os": self.os,
            "sources": [dict(s) for s in self.sources],
        }


def _bump(confidence: str) -> str:
    i = _CONF_ORDER.index(confidence)
    return _CONF_ORDER[min(i + 1, len(_CONF_ORDER) - 1)]


def _valid_type(value) -> str | None:
    if isinstance(value, str) and value.strip().lower() in DEVICE_TYPES:
        return value.strip().lower()
    return None


def _candidates(signals: Mapping) -> list[dict]:
    """Collect every signal that offers a device-type opinion, as
    {signal, dtype, conf, value, weight} dicts. Order of collection is
    irrelevant -- ranking is by weight."""
    out: list[dict] = []

    def add(signal: str, dtype: str, conf: str, value: str) -> None:
        out.append({"signal": signal, "dtype": dtype, "conf": conf,
                    "value": value, "weight": _WEIGHTS[signal]})

    pin = _valid_type(signals.get("user_pin"))
    if pin:
        add("user_pin", pin, "high", pin)

    # Protocol self-description hooks (mDNS/SSDP/SNMP collectors). Capped at
    # "medium" ALONE -- see the module docstring's collector threat-model note;
    # a 2nd agreeing signal still reaches "high" via the normal consensus bump.
    for key in ("upnp", "bonjour", "snmp"):
        info = signals.get(key)
        if isinstance(info, Mapping):
            dtype = _valid_type(info.get("device_type"))
            if dtype:
                add(key, dtype, "medium", f"self-described as {dtype}")

    dhcp = signals.get("dhcp")
    if isinstance(dhcp, Mapping):
        dtype = _valid_type(dhcp.get("device_type"))
        if dtype:
            add("dhcp", dtype, "medium", f"DHCP fingerprint: {dtype}")

    hostname = signals.get("hostname")
    dtype = hostname_type(hostname)
    if dtype:
        add("hostname", dtype, "high", f"{hostname} -> {dtype}")

    hint = port_type_hint(signals.get("open_ports"))
    if hint:
        add("port_hint", hint[0], "medium", hint[1])

    vendor = signals.get("vendor") or ""
    if vendor in VENDOR_DEFAULT_TYPE:
        dtype, conf = VENDOR_DEFAULT_TYPE[vendor]
        add("oui", dtype, conf, f"{vendor} -> {dtype}")
    elif vendor in MOBILE_HEAVY_VENDORS:
        add("mobile_vendor", "phone", "low", f"{vendor} is a mobile-heavy vendor")
    elif vendor in ("", "unknown") and is_locally_administered(signals.get("mac") or ""):
        add("random_mac", "phone", "low", "randomized (locally-administered) MAC")

    return sorted(out, key=lambda c: c["weight"], reverse=True)


def _first_str(signals: Mapping, keys: tuple[str, ...], attr: str) -> str | None:
    """First non-empty ``attr`` across the given collector dicts, in
    precedence order."""
    for key in keys:
        info = signals.get(key)
        if isinstance(info, Mapping):
            value = info.get(attr)
            if isinstance(value, str) and value.strip():
                return value.strip()[:_MAX_FIELD_LEN]
    return None


def recognize(signals: Mapping) -> DeviceIdentity:
    """Fuse all populated signals for one device into a DeviceIdentity.

    Pure/offline. The winner is the highest-precedence type opinion; its own
    confidence is bumped one level when >=2 independent signals agree on the
    same type. ``make`` falls back to the OUI vendor (the manufacturer IS the
    best local make guess); ``model``/``os`` only ever come from
    self-describing collectors (never invented).
    """
    cands = _candidates(signals)
    sources = tuple(
        {"signal": c["signal"], "value": c["value"], "weight": c["weight"]}
        for c in cands
    )

    make = _first_str(signals, ("upnp", "bonjour", "snmp"), "make")
    if make is None:
        vendor = signals.get("vendor") or ""
        make = vendor if vendor not in ("", "unknown") else None
    model = _first_str(signals, ("upnp", "bonjour", "snmp"), "model")
    os_name = _first_str(signals, ("upnp", "bonjour", "snmp", "dhcp"), "os")

    if not cands:
        return DeviceIdentity(make=make, model=model, os=os_name, sources=sources)

    winner = cands[0]
    confidence = winner["conf"]
    if winner["signal"] == "oui":
        # M1 audit fix: OUI is the first 3 MAC octets -- freely settable by any LAN
        # device -- so an OUI-ALONE verdict must never claim "high" on its own (a
        # rogue device could otherwise self-select a high-confidence vendor prefix
        # and blend into rogue-alert triage). A 2nd independent agreeing signal can
        # still restore "high" via the consensus bump below.
        confidence = min(confidence, "medium", key=_CONF_ORDER.index)
    agreeing = {c["signal"] for c in cands if c["dtype"] == winner["dtype"]}
    if len(agreeing) >= 2:
        confidence = _bump(confidence)

    return DeviceIdentity(
        device_type=winner["dtype"],
        confidence=confidence,
        make=make,
        model=model,
        os=os_name,
        sources=sources,
    )
