"""How a Wavr Core is updated, and why it will not update itself.

## The constraint this design starts from

Wavr's central promise is that it needs no internet connection and reports to
nobody. An auto-updater is the most common way a local-first product quietly
breaks that: it phones home on a timer, carries an install identifier so the
check can be counted, and eventually becomes the one component that has to reach
outward for the rest to keep working.

So the shape here is the opposite of the usual one:

  * **Nothing checks anything by default.** An install that is never told to look
    never makes a request, and the module is inert.
  * **A check is opt-in, through the existing connector gate**, not a new egress
    path of its own. There is one screen in this product where anything outward
    is switched on, and this belongs on it.
  * **A check carries nothing.** No install id, no version, no platform, no
    count. It reads a public list of releases; the request looks like anybody
    else's, because it is.
  * **Wavr never applies an update to itself.** A Core that updated overnight
    would change what the sensors do while nobody was watching, on a machine
    somebody chose specifically because it does not do things behind their back.

## What it does instead, and why that is most of the value

It tells the operator **how THIS install updates**, which is genuinely
different per channel and is the actual question. Somebody who installed the
Docker image needs `docker pull`; somebody who ran the Linux script re-runs it;
somebody on Windows downloads an installer; somebody on Android taps an APK. A
product that says "an update is available" and stops there has answered the easy
half.

The channel is DETECTED, not configured. A wrong instruction is worse than none:
it sends somebody to run a command that does nothing and leaves them believing
they have updated.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

# The connector row this feature is gated on. Absent or disabled means no check
# is ever made — see `connector_store`, which is the one place anything outward
# is switched on in this product.
CONNECTOR_ID = "update_check"

# Where a check would look. A public, unauthenticated list of releases, read with
# no identifying header. Named as a constant so an audit can find every outward
# address in this file by reading one line.
RELEASES_URL = "https://api.github.com/repos/augbastos/wavr/releases/latest"

# How this Core was installed. Detected rather than configured — a wrong
# instruction sends somebody to run a command that does nothing and leaves them
# believing they have updated.
CHANNEL_DOCKER = "docker"
CHANNEL_ANDROID = "android"
CHANNEL_WINDOWS = "windows"
CHANNEL_SCRIPT = "script"        # the Linux/Pi installer
CHANNEL_SOURCE = "source"        # a git checkout, running from the repo
CHANNEL_UNKNOWN = "unknown"

# What to do, per channel. Each is the whole instruction, not a hint: the
# question an operator has is "what do I type", and half an answer is why people
# end up on a forum.
INSTRUCTIONS: dict[str, str] = {
    CHANNEL_DOCKER: (
        "docker pull ghcr.io/augbastos/wavr:latest, then recreate the "
        "container. Your wavr.db and house.json live in the volume, so the "
        "Space survives."),
    CHANNEL_ANDROID: (
        "Install the new APK over the old one. Android keeps the app's own "
        "storage, so the Space survives — but stop the Core from its "
        "notification first, so nothing is mid-write."),
    CHANNEL_WINDOWS: (
        "Download the new installer and run it. It replaces the program and "
        "leaves your Space where it is."),
    CHANNEL_SCRIPT: (
        "Re-run the install script. It updates in place and leaves your Space "
        "alone — wavr.db and house.json are not touched."),
    CHANNEL_SOURCE: (
        "git pull, then reinstall the backend: pip install -e backend. Check "
        "the release notes for a migration before restarting."),
    CHANNEL_UNKNOWN: (
        "Wavr could not tell how it was installed, so it will not guess at an "
        "instruction that might do nothing. Update the way you installed it."),
}


@dataclass(frozen=True)
class UpdateStatus:
    """What this Core knows about its own version. Usually not much, by design."""

    running: str
    channel: str
    #: The newest release, when a check was made AND permitted. `None` is the
    #: normal state: nothing has looked, because nothing was asked to.
    latest: str | None = None
    checked: bool = False
    check_enabled: bool = False
    error: str = ""

    @property
    def behind(self) -> bool | None:
        """Whether a newer release exists, or `None` for "nobody has looked".

        Tristate, and the third state is the usual one. A boolean here would
        make "no check has been made" indistinguishable from "you are up to
        date", and the second is a claim this module has no basis for.
        """
        if not self.checked or not self.latest:
            return None
        return _newer(self.latest, self.running)

    def to_dict(self) -> dict:
        out = {
            "running": self.running,
            "channel": self.channel,
            "how_to_update": INSTRUCTIONS.get(self.channel,
                                              INSTRUCTIONS[CHANNEL_UNKNOWN]),
            "check_enabled": self.check_enabled,
            "checked": self.checked,
            "up_to_date": None if self.behind is None else not self.behind,
            "note": ("Wavr never updates itself. A Core that changed overnight "
                     "would change what the sensors do while nobody was "
                     "watching."),
        }
        if self.latest:
            out["latest"] = self.latest
        if self.error:
            out["error"] = self.error
        if not self.check_enabled:
            out["why_no_check"] = (
                "Checking for updates is off. It is a connector like any other "
                "outward thing — switch it on in Connectors if you want it. "
                "Nothing is sent either way: the check reads a public list and "
                "carries no identifier, no version and no count.")
        return out


def detect_channel(*, environ=None, argv0: str = "", frozen: bool | None = None,
                   repo_marker=None) -> str:
    """How this Core was installed.

    Every signal is passed in rather than read from globals, so the detection is
    testable without five different machines — which is the only way it will
    ever be right for the four channels nobody here can run.
    """
    env = environ if environ is not None else os.environ
    # Chaquopy sets this on the Android build; nothing else does.
    if env.get("WAVR_ANDROID") or "ANDROID_ROOT" in env:
        return CHANNEL_ANDROID
    # The image sets it, and a container without it is not our image.
    if env.get("WAVR_DOCKER") or os.path.exists("/.dockerenv"):
        return CHANNEL_DOCKER
    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if is_frozen:
        return CHANNEL_WINDOWS if sys.platform.startswith("win") else CHANNEL_SCRIPT
    # A checkout has a .git beside the package. Checked last, because a
    # developer running the Docker image from a checkout is on the Docker
    # channel for update purposes.
    marker = repo_marker
    if marker is None:
        marker = os.path.exists(
            os.path.join(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))), ".git"))
    if marker:
        return CHANNEL_SOURCE
    if env.get("WAVR_INSTALL_CHANNEL") in INSTRUCTIONS:
        return env["WAVR_INSTALL_CHANNEL"]
    return CHANNEL_UNKNOWN


def _parts(tag: str) -> tuple:
    """A version as comparable numbers. Anything unparseable sorts as nothing."""
    cleaned = str(tag or "").strip().lstrip("vV").split("+")[0].split("-")[0]
    out = []
    for chunk in cleaned.split("."):
        if not chunk.isdigit():
            break
        out.append(int(chunk))
    return tuple(out)


def _newer(candidate: str, running: str) -> bool:
    a, b = _parts(candidate), _parts(running)
    if not a or not b:
        # One of them is not a version Wavr can compare. Saying "no" is the
        # honest answer: claiming an update exists on the strength of a string
        # comparison would send somebody to reinstall for nothing.
        return False
    return a > b


def status(*, running: str, check_enabled: bool, latest: str | None = None,
           checked: bool = False, error: str = "", channel: str | None = None,
           **detect_kw) -> UpdateStatus:
    """Assemble what this Core knows. Makes no request of its own.

    The fetch lives with the caller, which is what keeps this module free of a
    network path — and means an audit of "what can reach outward" does not have
    to read it.
    """
    return UpdateStatus(
        running=running,
        channel=channel or detect_channel(**detect_kw),
        latest=latest, checked=checked, check_enabled=check_enabled,
        error=error)


def read_release(payload) -> str:
    """The tag from a releases response, or "" if it is not one.

    Reads exactly one field. A release payload from a public API is untrusted
    input like any other, and the alternative — trusting its shape — is how a
    body that changed becomes a traceback on somebody's dashboard.
    """
    if not isinstance(payload, dict):
        return ""
    tag = payload.get("tag_name") or payload.get("name") or ""
    tag = str(tag).strip()[:64]
    # A tag has to look like one. Anything else is refused rather than shown,
    # because this string ends up rendered next to "a new version is available".
    return tag if _parts(tag) else ""
