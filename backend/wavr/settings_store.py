"""Operator settings the UI can write — so nobody has to edit `.env`.

This module exists to fix ONE product failure, and it is the biggest one:
every switch in Wavr is an environment variable. Turning on LAN mode, naming
the instance, enabling nodes, allowing a network scan — all of it means opening
a text file next to a database and typing `WAVR_SOMETHING=1`. That is a fine
interface for the person who wrote the code and a wall for everybody else.

## How it layers, and why this shape

`config.load_config()` reads `os.getenv` directly, in ~90 places, and is covered
by a large test suite. Rewriting it to consult a database would be a wide,
risky change to the most load-bearing function in the backend for no functional
gain. So this store does not intercept anything. At startup it simply
**exports its stored values into `os.environ` for keys the environment does not
already define**, and `load_config()` runs exactly as before.

Two consequences, both deliberate:

  * **The environment always wins.** A key already present in `os.environ` (or
    in the operator's `.env`, which `python-dotenv` has already loaded by then)
    is never overwritten. An operator who deliberately wrote `WAVR_MULTIDEVICE=0`
    cannot have it flipped by a UI click, and a container's env stays
    authoritative. The store is a *fallback*, not an override.
  * **Most changes need a restart**, because `load_config()` runs once. That is
    disclosed per key (`restart_required`) and surfaced in the API rather than
    pretended away.

## Why an allow-list rather than arbitrary keys

An open key-value store writable over HTTP is an environment-injection
primitive. So:

  * only keys in `SETTING_SPECS` can be written — nothing else, ever;
  * every value is type-checked and range-checked before it is stored;
  * **no secret is storable here.** API keys, tokens, TLS material and
    passwords are absent from the allow-list by construction. They stay in the
    environment, where they already are, and where this file cannot read or
    write them;
  * keys that change Wavr's *exposure* (LAN mode, active scanning, identity
    labelling) are marked `sensitive` and refuse to be set without an explicit
    acknowledgement string from the caller — "discover aggressively, activate
    conservatively" (§15) expressed as an API contract, not a checkbox;
  * `WAVR_NET_BLOCKING` (ARP blocking — an active attack primitive) and
    `WAVR_MCP_CONTROL` (agent actuation) are deliberately **NOT** in the
    allow-list. They remain env-only, exactly as hard to turn on as before.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

# The exact phrase a caller must send to set a `sensitive` key. Not a security
# control — anyone who can reach the admin API can type it. It exists so the
# change cannot happen as a side effect of a generic "save settings" POST: a
# sensitive flip has to be a deliberate, separately-worded act.
CONSENT_PHRASE = "i-understand"

# Environment variables THIS PROCESS published from the store, and the exact
# value it published.
#
# Without this, a value the operator set in the UI comes back as
# `source: "env", locked: true` on the next boot -- because `export_to_env` put
# it in `os.environ` itself -- and the settings screen shows a value the user
# just chose as un-editable, owned by a config file they never wrote.
#
# The VALUE matters, not just the name: a bare set of names never self-corrects,
# so once a name was in it, any later environment value -- including one an
# operator genuinely exported by hand -- would be mistaken for ours. Comparing
# the value means the claim evaporates the moment the environment says something
# we did not write.
#
# Process-level rather than per-instance because the store that exports at
# startup is not always the one that later answers `GET /api/settings`
# (`apply_stored_settings` opens a throwaway and closes it).
_EXPORTED_BY_US: dict[str, str] = {}


@dataclass(frozen=True)
class SettingSpec:
    """One writable knob. `env` is the variable `config.py` already reads —
    this store never invents a new configuration surface, it only makes the
    existing one reachable."""

    key: str
    env: str
    kind: str                    # "bool" | "int" | "float" | "str" | "choice"
    default: str
    label: str
    description: str
    restart_required: bool = True
    sensitive: bool = False
    # Writable only from the Core's own screen (loopback root), never from a
    # paired companion. For the switches that take Wavr OFF this machine: an
    # admin's phone is on the network this would newly expose, and `central`
    # holds `admin` by default, so scope alone is not the right fence. Same
    # rationale `require_root` already carries for ARP blocking. Reading them
    # stays open to any admin -- it is the WRITE that widens.
    local_only: bool = False
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None


def _s(*a, **kw) -> SettingSpec:
    return SettingSpec(*a, **kw)


# The complete writable surface. Adding a key here is the ONLY way to make it
# UI-settable, and every addition should answer: could a hostile value of this
# weaken a security gate? If yes, it does not belong here.
SETTING_SPECS: tuple[SettingSpec, ...] = (
    # -- Identity -----------------------------------------------------------
    _s("instance_name", "WAVR_INSTANCE_NAME", "str", "Wavr",
       "Core name",
       "How this Core introduces itself to other devices on your network."),

    # -- Network exposure (sensitive) ---------------------------------------
    _s("lan_access", "WAVR_MULTIDEVICE", "bool", "0",
       "Let other devices connect",
       "Allows phones, tablets and other computers on this same network to "
       "connect to Wavr with a paired credential. Without this, Wavr answers "
       "only to the machine it runs on.",
       sensitive=True, local_only=True),
    _s("bind_host", "WAVR_BIND", "choice", "127.0.0.1",
       "Listen on",
       "Which network address the Core listens on. This only takes effect when "
       "'Let other devices connect' is on.",
       choices=("127.0.0.1", "0.0.0.0"), sensitive=True, local_only=True),
    _s("port", "WAVR_PORT", "int", "8000",
       "Port", "The port the Core listens on.",
       minimum=1024, maximum=65535),
    _s("nodes_enabled", "WAVR_NODES_ENABLED", "bool", "0",
       "Accept sensor nodes",
       "Lets small sensors you flash yourself (ESP32, radar, PIR) enrol and "
       "report to this Core. Requires 'Let other devices connect'.",
       sensitive=True, local_only=True),
    _s("peers_enabled", "WAVR_PEERS_ENABLED", "bool", "0",
       "Connect to other Cores",
       "Lets this Core pair with another Wavr Core in the same Space. "
       "Requires 'Let other devices connect'.",
       sensitive=True, local_only=True),
    _s("developer_mode", "WAVR_DEVELOPER_MODE", "bool", "0",
       "Developer mode",
       "Shows the tools for building applications on top of Wavr: the provider "
       "catalogue, the live event stream, a manifest checker, and scenarios "
       "that simulate a house so you can develop without owning the sensors. "
       "Off by default — nobody who is not writing software needs any of it, "
       "and a normal setup should never make you read the word 'manifest'.",
       restart_required=False, local_only=True),

    # -- Sensing ------------------------------------------------------------
    _s("net_inventory", "WAVR_NET_INVENTORY", "bool", "0",
       "Look at what's on the network",
       "Builds a list of the devices on your network so Wavr can recognise "
       "them. Read-only: Wavr never changes anything on another device.",
       sensitive=True),
    _s("onvif_probe", "WAVR_ONVIF_PROBE", "bool", "0",
       "Look for cameras",
       "Actively asks devices on your network whether they are ONVIF cameras.",
       sensitive=True),
    _s("identity_enabled", "WAVR_IDENTITY_ENABLED", "bool", "0",
       "Recognise who is home",
       "Lets you label a device as belonging to a person, so Wavr can say who "
       "is home. Names you choose — never faces, voices or biometrics.",
       sensitive=True),
    _s("fusion_threshold", "WAVR_FUSION_THRESHOLD", "float", "0.5",
       "Presence sensitivity",
       "How much combined evidence Wavr needs before it calls a room occupied. "
       "Lower is more sensitive and more false alarms.",
       restart_required=True, minimum=0.05, maximum=0.95),
    _s("net_interval", "WAVR_NET_INTERVAL", "float", "15.0",
       "Network check interval (seconds)",
       "How often Wavr re-checks which devices are reachable.",
       minimum=5.0, maximum=600.0),
    _s("occupancy_log", "WAVR_OCCUPANCY_LOG", "bool", "1",
       "Remember room history",
       "Keeps a coarse record of which rooms were occupied and when. Never "
       "positions, never vitals, never camera frames.",
       restart_required=True),
    _s("occupancy_retention_days", "WAVR_OCCUPANCY_RETENTION_DAYS", "float", "60",
       "Keep room history for (days)",
       "Older entries are deleted automatically.",
       minimum=1.0, maximum=730.0),

    # DELIBERATELY ABSENT: `WAVR_HOUSE_MAP`. It is a filesystem path that
    # `housemap.save_house_map` writes to, and a free-form string setting with no
    # path validation is an arbitrary-file-write primitive. It buys almost
    # nothing (the default works, and an operator who genuinely needs to move the
    # floor plan can still set the environment variable), so the safe answer is
    # not to offer it. Any future path-valued setting needs a real path check
    # here BEFORE it goes in this tuple.
)

SPECS_BY_KEY: dict[str, SettingSpec] = {s.key: s for s in SETTING_SPECS}
SPECS_BY_ENV: dict[str, SettingSpec] = {s.env: s for s in SETTING_SPECS}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_ts TEXT NOT NULL
);
"""


class SettingsError(ValueError):
    """Bad key or bad value — the API layer turns this into a 400/422."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off", "")


def coerce(spec: SettingSpec, raw) -> str:
    """Validate and normalise one value into the exact string `config.py`
    expects to read out of the environment. Raises `SettingsError` on anything
    it cannot vouch for — never silently substitutes a default, because a
    silently-ignored setting is worse than a rejected one."""
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return "1" if raw else "0"
        val = str(raw).strip().lower()
        if val in _TRUE:
            return "1"
        if val in _FALSE:
            return "0"
        # Anything else is REJECTED, not quietly read as "off". Silently
        # storing `{"value": "enabled"}` as disabled and answering 200 is the
        # worst possible outcome: the operator believes they turned something
        # on, and nothing anywhere says otherwise.
        raise SettingsError(
            f"{spec.key} must be true or false (got {str(raw)[:32]!r})")

    if spec.kind == "choice":
        val = str(raw).strip()
        if val not in spec.choices:
            raise SettingsError(f"{spec.key} must be one of {list(spec.choices)}")
        return val

    if spec.kind in ("int", "float"):
        try:
            num = float(raw)
        except (TypeError, ValueError):
            raise SettingsError(f"{spec.key} must be a number") from None
        # Reject NaN/inf explicitly: both survive float() and both go on to
        # produce nonsense downstream (a NaN threshold makes every comparison
        # false, which reads as "the sensor is broken").
        if num != num or num in (float("inf"), float("-inf")):
            raise SettingsError(f"{spec.key} must be a finite number")
        if spec.minimum is not None and num < spec.minimum:
            raise SettingsError(f"{spec.key} must be at least {spec.minimum}")
        if spec.maximum is not None and num > spec.maximum:
            raise SettingsError(f"{spec.key} must be at most {spec.maximum}")
        return str(int(num)) if spec.kind == "int" else str(num)

    val = " ".join(str(raw).split())[:128]
    if not val:
        raise SettingsError(f"{spec.key} must not be empty")
    # An env value carrying a newline could smuggle a second assignment into a
    # generated .env; `split()` above already removes them, this is the assert.
    if any(c in val for c in "\r\n\x00"):
        raise SettingsError(f"{spec.key} contains an illegal character")
    return val


class SettingsStore:
    """SQLite-backed operator settings. Shares `wavr.db`, owns `settings`."""

    def __init__(self, path: str = "wavr.db", now_fn=_utcnow_iso,
                 environ: dict | None = None):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._now = now_fn
        # Injectable so a test can drive the env-wins rule without touching the
        # real process environment.
        self._env = environ if environ is not None else os.environ
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def get(self, key: str) -> str | None:
        """The STORED value, ignoring the environment. `None` = never set."""
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key = ?",
                                     (key,)).fetchone()
        return row["value"] if row else None

    def set(self, key: str, value, consent: str | None = None) -> str:
        spec = SPECS_BY_KEY.get(key)
        if spec is None:
            raise SettingsError(f"unknown setting: {key}")
        if spec.sensitive and consent != CONSENT_PHRASE:
            raise SettingsError(
                f"{key} changes what Wavr is exposed to — it needs an explicit "
                f"acknowledgement (consent='{CONSENT_PHRASE}')")
        val = coerce(spec, value)
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings (key, value, updated_ts) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                " updated_ts = excluded.updated_ts",
                (key, val, self._now()))
            self._conn.commit()
        return val

    def unset(self, key: str) -> bool:
        spec = SPECS_BY_KEY.get(key)
        with self._lock:
            cur = self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            self._conn.commit()
        if spec is not None:
            # Stop claiming we published this one. Whatever we exported is still
            # live in os.environ until the next restart, and after an unset that
            # is honestly what it now is: the environment, until restart.
            _EXPORTED_BY_US.pop(spec.env, None)
        return cur.rowcount > 0

    def effective(self, key: str) -> dict:
        """What this setting resolves to RIGHT NOW, and where that came from.

        `source` is one of `env` / `stored` / `default`. Rendering the source is
        the whole point: an operator whose `.env` is quietly beating their UI
        click needs to be told that, not left clicking."""
        spec = SPECS_BY_KEY.get(key)
        if spec is None:
            raise SettingsError(f"unknown setting: {key}")
        env_val = self._env.get(spec.env)
        if env_val not in (None, "") and _EXPORTED_BY_US.get(spec.env) != env_val:
            # Present in the environment and NOT put there by us: the operator's
            # own `.env` or container config wins, and the UI must say so rather
            # than accept a click that will never take effect.
            return {"key": key, "value": env_val, "source": "env", "locked": True}
        stored = self.get(key)
        if stored is not None:
            return {"key": key, "value": stored, "source": "stored", "locked": False}
        return {"key": key, "value": spec.default, "source": "default", "locked": False}

    def describe(self) -> list[dict]:
        """Every knob, its current value, its provenance and its metadata — one
        payload the settings UI can render without knowing anything else."""
        out = []
        for spec in SETTING_SPECS:
            eff = self.effective(spec.key)
            out.append({
                **eff,
                "env": spec.env, "kind": spec.kind, "default": spec.default,
                "label": spec.label, "description": spec.description,
                "restart_required": spec.restart_required,
                "sensitive": spec.sensitive,
                # So the UI can disable it with a reason on a companion rather
                # than offering a click that will come back 403.
                "local_only": spec.local_only,
                "choices": list(spec.choices),
                "minimum": spec.minimum, "maximum": spec.maximum,
            })
        return out

    def export_to_env(self, environ: dict | None = None) -> list[str]:
        """Publish stored values into the process environment for keys the
        environment does not already define, and return the names actually set.

        Call this ONCE, before `load_config()`. It is the entire integration
        point with the existing configuration system — deliberately a handful
        of lines, so there is no second place where a Wavr setting can come
        from and no chance of the two disagreeing."""
        env = environ if environ is not None else self._env
        applied: list[str] = []
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        for row in rows:
            spec = SPECS_BY_KEY.get(row["key"])
            if spec is None:
                continue          # a key removed from the allow-list stays inert
            if env.get(spec.env) not in (None, ""):
                continue          # the environment wins, always
            env[spec.env] = row["value"]
            _EXPORTED_BY_US[spec.env] = row["value"]
            applied.append(spec.env)
        return applied

    def close(self) -> None:
        self._conn.close()


def apply_stored_settings(db_path: str, environ: dict | None = None) -> list[str]:
    """Convenience wrapper for the launcher: open the store, export, close.

    Never raises. A settings database that is missing, locked or corrupt must
    not stop Wavr from booting — the operator would lose the very UI they need
    to fix it. On failure the process simply runs on env + defaults, which is
    exactly the pre-existing behaviour."""
    try:
        store = SettingsStore(db_path, environ=environ)
    except sqlite3.Error:
        return []
    try:
        return store.export_to_env(environ)
    except sqlite3.Error:
        return []
    finally:
        try:
            store.close()
        except sqlite3.Error:
            pass
