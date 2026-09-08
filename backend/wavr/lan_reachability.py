"""Can another device on this network actually reach this Core?

## The failure this exists for

Turning on "Let other devices connect" binds the Core to the LAN. On Windows
that is the moment the firewall decides, and the decision is not Wavr's:

  * Windows shows a dialog naming `wavr-core.exe` — a filename the person who
    just installed "Wavr" has no reason to recognise — and asks about "private"
    and "public" networks.
  * If they click **Cancel**, Windows writes a persistent BLOCK rule. The Core
    keeps running, keeps reporting healthy, keeps printing a LAN address on the
    setup screen and inside the pairing QR. The phone scans it and nothing
    happens, for ever, with no error anywhere.
  * If the dialog never appears (a service, a machine with a policy, a rule
    already denied once), the same silence follows with no click at all.

Every part of Wavr is honest about that address except the one thing that
decides whether it works. A Core cannot observe its own blocked inbound
packets — they never arrive — so the only way to know is to ask the firewall
what it would do.

## What this does and does not claim

It answers one question: **is there anything in the Windows firewall that would
stop another device reaching this Core?** Three answers, and the third is a real
answer rather than a failure:

  * `BLOCKED` — an enabled inbound BLOCK rule matches this program or its port.
    This is the "clicked Cancel once" state, and it is the one worth shouting
    about, because nothing else in the product can see it.
  * `ALLOWED` — an enabled inbound ALLOW rule matches. Note what this does NOT
    prove: a rule can be scoped to a profile the machine is not currently on, or
    to an interface the phone is not on. It means "the firewall is not the thing
    standing in the way", not "the phone will definitely connect".
  * `UNKNOWN` — not Windows, the query failed, the query timed out, or no rule
    matches either way. **No rule matching is UNKNOWN, not ALLOWED**: Windows
    will prompt on the next bind, and where a policy suppresses the prompt the
    default inbound action is to block. Reporting "fine" there would be exactly
    the reassurance this module exists to stop.

## Why it is not on any hot path

`Get-NetFirewallRule` takes about three seconds on a normal machine and `netsh`
about one and a half. Both are far too slow to sit inside a request that a
dashboard polls. So this is on demand, cached for `CACHE_S`, and executed off
the event loop by its caller. It never raises: every failure resolves to
`UNKNOWN` with a reason, because a firewall check that can break the Core is
worse than no firewall check.

## It reads. It never writes.

Nothing here adds, removes or relaxes a rule. A product that quietly opens a
hole in somebody's firewall — even its own — has made a security decision on
their behalf that they did not ask for and cannot see. Telling them what is
wrong, in words, and letting them fix it is the whole of this module's job.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

# The three answers. Strings rather than an enum so they cross the API boundary
# as themselves and read the same in a diagnostics bundle.
BLOCKED = "blocked"
ALLOWED = "allowed"
UNKNOWN = "unknown"

CACHE_S = 60.0          # a firewall rule does not change minute to minute
QUERY_TIMEOUT_S = 12.0  # netsh is ~1.5s warm; this is the "something is wrong" cliff


@dataclass(frozen=True)
class Reachability:
    """What the firewall would do to an inbound connection to this Core."""

    state: str = UNKNOWN
    # Why, in one line, for a diagnostics bundle. Never shown to a household as
    # is -- the human sentence lives in the attention inbox, which is
    # translated. This is evidence, not copy.
    reason: str = ""
    # The rule names that decided it, so a bundle can be acted on without
    # guessing. Rule names are the operator's own machine configuration, not
    # household data, and they are what `netsh` needs to remove one.
    rules: tuple[str, ...] = field(default_factory=tuple)
    checked: bool = False

    def to_dict(self) -> dict:
        return {"state": self.state, "reason": self.reason,
                "rules": list(self.rules), "checked": self.checked}


_cache: tuple[float, Reachability] | None = None


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _netsh_rules() -> str:
    """Every inbound rule, as text. Empty string on any failure.

    `netsh` rather than `Get-NetFirewallRule`: it is roughly twice as fast, it
    is present on every Windows since 7, and it needs no PowerShell execution
    policy. `CREATE_NO_WINDOW` so a frozen GUI build never flashes a console at
    somebody -- the Core runs as a sidecar behind a tray icon, and a black
    rectangle appearing on its own is the kind of thing that gets a product
    uninstalled.
    """
    flags = 0
    if _is_windows():
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        out = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule",
             "name=all", "dir=in", "verbose"],
            capture_output=True, timeout=QUERY_TIMEOUT_S,
            creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    # `netsh` speaks the console code page, which is not UTF-8 on most Windows
    # installs. Decoded permissively because the only thing being matched is
    # ASCII: a program path, a port number and the words Allow/Block.
    return out.stdout.decode("utf-8", errors="replace")


# The three firewall profiles, as the invariant tokens netsh prints. Localised
# Windows translates the field NAMES but not these, the same reason
# `_verdict_of` keys on Allow/Block rather than on "Action".
_PERFIS = ("domain", "private", "public")


def _active_profiles() -> frozenset[str]:
    """Which firewall profile(s) this machine is on right now.

    `netsh advfirewall show currentprofile` heads its output with the profile
    it is describing -- "Public Profile Settings:" -- and that is the whole
    answer. Empty when it cannot be read, and an empty answer is treated as
    "do not claim", never as "fine".

    Without this, a rule scoped to Public counts as coverage on a machine that
    Windows has since decided is Private, and the Core reports that other
    devices can reach it while nothing can. The first user's machine had four
    ALLOW rules for the Core and every one of them was Public-only; his Wi-Fi
    happened to be Public that afternoon. Nothing about that is stable --
    changing routers, a VPN, or answering "make this PC discoverable" once is
    enough to move it.
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _is_windows() else 0
    try:
        out = subprocess.run(
            ["netsh", "advfirewall", "show", "currentprofile"],
            capture_output=True, timeout=QUERY_TIMEOUT_S, creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    texto = out.stdout.decode("utf-8", errors="replace").lower()
    # netsh opens with a blank line, so the heading is not line zero. Reading
    # line zero returned "" and this said "I cannot tell" on a machine where
    # the answer was printed one line further down -- which then reported
    # "allowed ... (unknown)", a sentence with a hole in the middle of it.
    #
    # Only the lines ABOVE the dashed rule are looked at: past that come the
    # profile's settings, and one of them is `Firewall Policy
    # BlockInbound,AllowOutbound`, which contains no profile word today but is
    # the kind of line that makes a whole-file search wrong later.
    for linha in texto.splitlines():
        l = linha.strip()
        if not l:
            continue
        if set(l) == {"-"}:
            break
        achados = frozenset(p for p in _PERFIS if p in l)
        if achados:
            return achados
        break       # a first content line that names no profile: do not guess
    return frozenset()


def _profiles_of(block: dict) -> frozenset[str]:
    """The profiles one rule applies to, from its own values.

    netsh writes them as a comma list -- `Profiles: Domain,Private` -- and
    `Any` means all three. Read off the values rather than a field name so a
    localised Windows still parses.
    """
    for value in block.values():
        v = value.strip().lower()
        if v in ("any", "qualquer", "alle", "tous"):
            # Only the Profiles field carries a bare "Any" alongside the words
            # below; LocalIP/RemoteIP say Any too, which is why the specific
            # branch above is checked first and this one only widens.
            continue
        partes = {p.strip() for p in v.split(",")}
        if partes and partes <= set(_PERFIS):
            return frozenset(partes)
    # `Profiles: Any` (or unreadable): the rule applies everywhere, so it cannot
    # be the reason coverage is missing.
    return frozenset(_PERFIS)


def _blocks(text: str) -> list[dict]:
    """Parse `netsh`'s rule blocks into dicts of its own field names.

    Rules are separated by a line of dashes and each field is `Name: value`.
    Localised Windows translates the field NAMES, which is why the matching
    below keys on the values (a path, a port, the word Allow/Block) wherever it
    can rather than on an English label.
    """
    out: list[dict] = []
    current: dict = {}
    pendente: tuple[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if set(line) == {"-"}:
            # netsh prints the dashes UNDER the rule's name, not between rules:
            #
            #     Rule Name:   wavr-core
            #     -------------------------
            #     Enabled:     Yes
            #     ...
            #     Action:      Allow
            #
            #     Rule Name:   Microsoft Edge (mDNS-In)
            #     -------------------------
            #
            # Splitting on the dashes therefore closed each block AFTER reading
            # the NEXT rule's name, so every block carried one rule's fields
            # under the following rule's name. The verdicts were right and the
            # names were shifted by one, which is why the first user's Core
            # reported that it was allowed through the firewall by a rule called
            # "Microsoft Edge (mDNS-In)". A report that names the wrong rule
            # cannot be checked by the person reading it, which is the only
            # thing a report of this kind is for.
            #
            # So the name just read starts the block the dashes open.
            #
            # It is held PENDING rather than written into the block as it is
            # read, and that detail is the whole thing working. The first
            # version of this fix wrote every line into the current block and
            # removed the name again when the dashes arrived -- but the next
            # rule's name lands on the same key as the current rule's, so it
            # had already overwritten it, and removing it took both. Every
            # block came out nameless except the last. A pair is only committed
            # once the following line proves it was not a name.
            if current:
                out.append(current)
            current = {} if pendente is None else {pendente[0]: pendente[1]}
            pendente = None
            continue
        if ":" in line:
            if pendente is not None:
                current[pendente[0]] = pendente[1]
            key, _, value = line.partition(":")
            pendente = (key.strip().lower(), value.strip())
    if pendente is not None:
        current[pendente[0]] = pendente[1]
    if current:
        out.append(current)
    return out


def _verdict_of(block: dict) -> str:
    """`allow`, `block` or `""` for one parsed rule.

    Matched on the value, not the field name, because a Portuguese or German
    Windows translates "Action" but not "Allow"/"Block" -- those come back as
    the invariant tokens. A rule whose action cannot be read at all is skipped
    rather than guessed at.
    """
    for value in block.values():
        v = value.strip().lower()
        if v in ("allow", "permitir", "zulassen", "autoriser"):
            return "allow"
        if v in ("block", "bloquear", "blockieren", "bloquer"):
            return "block"
    return ""


def _enabled(block: dict) -> bool:
    for value in block.values():
        v = value.strip().lower()
        if v in ("no", "não", "nao", "nein", "non"):
            # Only ever the Enabled field carries a bare no/yes in these dumps.
            return False
    return True


def _matches(block: dict, program: str, port: int) -> bool:
    """Does this rule govern OUR listener?

    Two ways a rule can name us, and a rule only has to do one:

      * by program -- the executable path, which is how the dialog's Allow and
        Cancel both write it. Compared case-insensitively on the basename as
        well as the full path, because the installed path and the path a
        developer ran from differ while the executable is the same one.
      * by port -- `LocalPort: 8000`, which is how a hand-written rule usually
        reads. Matched exactly, never as a substring: `8000` must not match a
        rule about `18000`, and a range or `Any` is not a match for a specific
        port because it says nothing about us in particular.
    """
    prog = (program or "").lower()
    base = os.path.basename(prog)
    text = " ".join(block.values()).lower()

    if prog and prog in text:
        return True
    if base and base in text:
        return True
    if port:
        for value in block.values():
            for token in re.split(r"[,\s]+", value.strip()):
                if token.isdigit() and int(token) == port:
                    return True
    return False


def check(program: str = "", port: int = 0, *, force: bool = False) -> Reachability:
    """Ask the firewall what it would do. Cached, never raises.

    `program` is the executable actually holding the listening socket -- for a
    packaged install that is the frozen Core, NOT the Python that a checkout
    runs, and passing the wrong one would look at a rule nobody wrote.
    """
    global _cache

    now = time.monotonic()
    if not force and _cache is not None and (now - _cache[0]) < CACHE_S:
        return _cache[1]

    result = _compute(program, port)
    _cache = (now, result)
    return result


def _compute(program: str, port: int) -> Reachability:
    if not _is_windows():
        # Everywhere else the answer is genuinely unknown to us: a Linux box
        # may be behind ufw, nftables or nothing, and guessing "fine" is the
        # reassurance this module exists to refuse.
        return Reachability(state=UNKNOWN, checked=False,
                            reason="not Windows; no firewall query implemented")

    text = _netsh_rules()
    if not text.strip():
        return Reachability(state=UNKNOWN, checked=False,
                            reason="the firewall query returned nothing "
                                   "(timed out, blocked, or netsh is absent)")

    ativos = _active_profiles()
    blocking: list[str] = []
    allowing: list[str] = []
    fora_do_perfil: list[str] = []
    for block in _blocks(text):
        if not _matches(block, program, port):
            continue
        if not _enabled(block):
            continue
        verdict = _verdict_of(block)
        name = ""
        for key, value in block.items():
            if "name" in key and "program" not in key and "group" not in key:
                name = value
                break
        rotulo = name or "(unnamed rule)"
        # A rule scoped to a profile this machine is not on governs nothing
        # here. Counting it was how "allowed" got said about a Core nothing
        # could reach. When the active profile cannot be read, `ativos` is
        # empty and no rule is discarded -- an unreadable profile is a reason
        # to stay quiet, not a reason to throw away evidence.
        if ativos and not (_profiles_of(block) & ativos):
            if verdict == "allow":
                fora_do_perfil.append(rotulo)
            continue
        if verdict == "block":
            blocking.append(rotulo)
        elif verdict == "allow":
            allowing.append(rotulo)

    if blocking:
        # A block wins over an allow, because that is what Windows itself does.
        return Reachability(state=BLOCKED, checked=True,
                            rules=tuple(blocking),
                            reason="an enabled inbound BLOCK rule matches this "
                                   "Core; Windows refuses the connection before "
                                   "it reaches us")
    if allowing:
        # Two different sentences, because they are two different claims. With
        # the profile known this is "the rule applies here"; without it, it is
        # only "a rule exists" — and writing the first while meaning the second
        # is the whole habit this module was built to break.
        escopo = (
            "and applies to the network profile this machine is on ({})".format(
                ", ".join(sorted(ativos)))
            if ativos else
            "though which network profile this machine is on could not be read, "
            "so whether the rule applies here is not established"
        )
        return Reachability(state=ALLOWED, checked=True,
                            rules=tuple(allowing),
                            reason="an enabled inbound ALLOW rule matches this "
                                   "Core, " + escopo)
    if fora_do_perfil:
        # The most misleading state there is, and it now has words of its own:
        # a rule exists, is enabled, allows this Core, and applies to a network
        # this machine is not on. Nothing gets through, and the firewall page
        # looks fine to anybody who goes looking.
        return Reachability(
            state=UNKNOWN, checked=True, rules=tuple(fora_do_perfil),
            reason="the only ALLOW rules for this Core apply to a different "
                   "network profile than the one this machine is on ({}), so "
                   "they do not let anything through right now".format(
                       ", ".join(sorted(ativos))))
    return Reachability(state=UNKNOWN, checked=True,
                        reason="no inbound rule mentions this Core; Windows "
                               "will decide on the next bind, and the default "
                               "inbound action is to refuse")


def reset_cache() -> None:
    """Forget the cached answer. For tests, and for the moment right after
    somebody has been told to go and fix their firewall."""
    global _cache
    _cache = None


# ---------------------------------------------------------------------------
# Which address the listening socket actually took
# ---------------------------------------------------------------------------
#
# The same shape as the scheme, and for the same reason: only the launcher
# knows, and `cfg.bind_host` is a statement of intent that some entry points
# never act on. `backend/Dockerfile` passes `--host` on the command line, which
# `load_config()` cannot see at all.
#
# It matters because the setup screen and the pairing QR hand out a LAN
# address, and a QR is acted on by a phone without a human reading it first. An
# address nothing is listening on fails with no error on either side: the phone
# dials, nobody answers, and the Core never learns that anything happened.

_LOOPBACK = ("127.0.0.1", "::1", "localhost", "")

_bound_host: str = ""
_bound_port: int = 0


def note_bound_host(host: str, port: int = 0) -> None:
    """Called by the launcher with the address it handed to uvicorn.

    `port` too, because a host without a port is not an address. `cfg.port`
    reads `WAVR_PORT`, and a launcher that states its port on the command line
    -- `uvicorn --port`, which is what `backend/Dockerfile` does -- leaves that
    variable unset. The product then advertised 8000 while answering somewhere
    else, which a clean-room reader hit on their first run.
    """
    global _bound_host, _bound_port
    _bound_host = (host or "").strip()
    try:
        _bound_port = int(port or 0)
    except (TypeError, ValueError):
        _bound_port = 0


def bound_host() -> str:
    """What the launcher said, or `""` when no launcher said anything."""
    return _bound_host


def bound_port(fallback: int = 0) -> int:
    """The port the socket is actually on, or `fallback` (`cfg.port`) when no
    launcher reported one."""
    return _bound_port or int(fallback or 0)


def serves_the_lan(fallback: str = "") -> bool:
    """Is this Core reachable from another device on the network?

    `fallback` is `cfg.bind_host`, used only when no launcher reported in --
    the honest best guess for an entry point that never calls
    `note_bound_host`. A wildcard bind (`0.0.0.0` / `::`) is the yes; anything
    loopback is the no.

    This answers "is the socket on the network", NOT "can the phone get
    through". The firewall is the other half, and `check()` above is what
    answers that one.
    """
    host = _bound_host or (fallback or "").strip()
    if not host:
        return False
    return host.lower() not in _LOOPBACK
