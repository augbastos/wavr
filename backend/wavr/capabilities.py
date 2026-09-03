"""Capability Manifest — what a machine can *physically do* for a Wavr Space.

This is the input to every "what should this device become?" decision (Core /
Node / Client), and it is deliberately SEPARATE from three things it is often
confused with:

  * a *person's* role (`people_store.py`) — what a human may do;
  * a *device's* auth role (`devices.py`: central/user/agent/guest) — what a
    credential may reach;
  * a *device's* function (`space_store.DeviceFunction`) — what job the operator
    assigned it.

A manifest says only "this box has a camera / has BLE / runs on battery". The
operator (or the recommendation below) turns that into a job.

HONESTY RULE (load-bearing, mirrors `SensingEvent.count`'s "never a fabricated
0"): every capability is a TRISTATE — `True` (proven present), `False` (proven
absent), `None` (**unknown / could not determine**). A probe that raises, times
out, or needs a dependency we do not have resolves to `None`, never to `False`.
Downstream, `None` must render as "unknown", never as "no" — recommending
against a Core because a probe crashed would be a lie dressed as a decision.

ZERO EGRESS: the scan reads only local OS state (platform module, /proc, /sys,
registry-free WMI-free shell-outs bounded by a timeout). It never touches the
network, never resolves a hostname, and never sends anything anywhere. It is
safe to run before the operator has consented to anything at all.
"""
from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field, asdict

# -- Vocabulary --------------------------------------------------------------
# The capability KEYS a manifest may carry. A device is free to omit any key
# (omitted == unknown == None); it may NOT invent keys outside this set, because
# the recommendation engine below and the Admin UI both switch on them. New
# hardware classes get a new key here, in one place.
CAPABILITY_KEYS: tuple[str, ...] = (
    # Connectivity
    "wifi", "ethernet", "ble", "bluetooth", "uwb", "nfc",
    # Sensing hardware
    "camera", "microphone", "gps", "accelerometer", "mmwave", "wifi_csi",
    # Compute / power
    "gpu", "display", "battery", "permanent_power", "usb",
    # Software abilities of the host (not hardware). Kept separate from the
    # sensing keys above on purpose: "this machine can drive a serial port" is a
    # different claim from "this machine has a radar", and conflating the two is
    # how a laptop with pyserial installed ends up advertising a radar sensor.
    "docker", "mdns", "network_scan", "raw_socket", "serial_port_support",
)

# The three FUNCTIONS a device can perform for a Space. A device may hold any
# combination — "Core + Node + Client" is the normal single-machine home setup,
# and refusing to model that combination is exactly the coupling this replaces.
ROLE_CORE = "core"      # authoritative runtime: fusion, storage, API, policy
ROLE_NODE = "node"      # contributes sensing/capabilities, holds no global state
ROLE_CLIENT = "client"  # renders the user/admin interface
DEVICE_FUNCTIONS: frozenset[str] = frozenset({ROLE_CORE, ROLE_NODE, ROLE_CLIENT})

# Compute tiers. Coarse ON PURPOSE — the exact core count matters far less than
# "can this thing run YOLO" vs "can this thing run a heartbeat loop".
TIER_MICRO = "micro"    # MCU class (ESP32): node only, no Python runtime
TIER_LOW = "low"        # Pi Zero / old phone: Core possible, no local CV
TIER_MEDIUM = "medium"  # Pi 4/5, mini-PC, modern phone: Core + light CV
TIER_HIGH = "high"      # laptop/desktop: Core + full CV + model inference
COMPUTE_TIERS: tuple[str, ...] = (TIER_MICRO, TIER_LOW, TIER_MEDIUM, TIER_HIGH)

# Platform slugs we recognise. "unknown" is a first-class answer.
PLATFORMS: tuple[str, ...] = (
    "windows", "linux", "macos", "android", "ios", "esp32", "unknown")

# How long any external probe may take before we give up and answer `None`.
# Short by design: a capability scan runs during onboarding, in front of a human
# watching a spinner. An honest "unknown" beats a 10-second stall.
PROBE_TIMEOUT_S = 2.0

# Below this, we will not recommend Core. A Core holds the fusion loop, SQLite
# and the API; on less than this it thrashes. Chosen to still admit a Pi Zero 2 W
# (512 MB) as a *reluctant* Core, which is the "one old laptop / one Pi" promise.
MIN_CORE_RAM_MB = 450


@dataclass(frozen=True)
class CapabilityManifest:
    """What one device reports it can do. Serialised over the Wavr Protocol
    (`docs/WAVR-PROTOCOL.md` §5) and stored per-device by the Core.

    `capabilities` maps a key from CAPABILITY_KEYS to True/False/None. A key that
    is absent from the dict is identical to None (unknown) — consumers MUST use
    `.capability()` below rather than indexing, so the two spellings can never
    diverge."""

    platform: str = "unknown"
    arch: str = ""
    os_version: str = ""
    # Which functions this device is *technically able* to perform. Not what it
    # was assigned — `space_store` holds the assignment.
    functions_supported: tuple[str, ...] = ()
    capabilities: dict = field(default_factory=dict)
    ram_mb: int | None = None
    cpu_count: int | None = None
    compute_tier: str = TIER_LOW
    # Free-form, human-facing. "Acer a laptop", "Raspberry Pi 5 Model B".
    model: str = ""
    # Protocol version this manifest was minted under. Lets a Core reject or
    # down-convert a manifest from a future Node instead of misreading it.
    protocol_version: int = 1

    def capability(self, key: str) -> bool | None:
        """Tristate read. Absent key == unknown == None. NEVER returns False for
        a key we simply did not probe — see the module docstring's honesty rule."""
        return self.capabilities.get(key)

    def supports(self, function: str) -> bool:
        return function in self.functions_supported

    def to_dict(self) -> dict:
        d = asdict(self)
        d["functions_supported"] = list(self.functions_supported)
        return d

    @classmethod
    def from_dict(cls, raw: dict) -> "CapabilityManifest":
        """Parse an untrusted manifest off the wire. Every field is bounded and
        type-checked: a Node is a device on the LAN, and `nodes.py` already
        establishes that a node must never be able to assert load-bearing facts
        about itself. A capability claim is *evidence for a recommendation*, not
        an authorisation — but it still must not be able to inject junk keys or
        blow up the Admin UI."""
        if not isinstance(raw, dict):
            raise ValueError("manifest must be an object")

        plat = str(raw.get("platform", "unknown")).strip().lower()
        if plat not in PLATFORMS:
            plat = "unknown"

        caps: dict[str, bool | None] = {}
        raw_caps = raw.get("capabilities")
        if isinstance(raw_caps, dict):
            for k, v in raw_caps.items():
                if k in CAPABILITY_KEYS:
                    # Anything that isn't a real bool becomes None (unknown),
                    # never a coerced truthy — "1", "yes" and [] are all junk
                    # from a device that didn't read the spec.
                    caps[k] = v if isinstance(v, bool) else None

        funcs = tuple(
            f for f in (raw.get("functions_supported") or [])
            if isinstance(f, str) and f in DEVICE_FUNCTIONS)

        tier = str(raw.get("compute_tier", TIER_LOW)).strip().lower()
        if tier not in COMPUTE_TIERS:
            tier = TIER_LOW

        return cls(
            platform=plat,
            arch=str(raw.get("arch", ""))[:32],
            os_version=str(raw.get("os_version", ""))[:64],
            functions_supported=funcs,
            capabilities=caps,
            ram_mb=_bounded_int(raw.get("ram_mb"), 0, 4_194_304),
            cpu_count=_bounded_int(raw.get("cpu_count"), 0, 4096),
            compute_tier=tier,
            model=str(raw.get("model", ""))[:96],
            protocol_version=_bounded_int(raw.get("protocol_version"), 1, 999) or 1,
        )


def _bounded_int(v, lo: int, hi: int) -> int | None:
    """int(v) clamped to [lo, hi], or None for anything non-numeric. bool is
    rejected explicitly — `True` is an int in Python and would silently become
    a RAM figure of 1 MB."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return max(lo, min(n, hi))


# -- Host scan ---------------------------------------------------------------

def _run(cmd: list[str]) -> str | None:
    """Best-effort bounded shell-out. Returns stdout, or None on ANY failure
    (missing binary, non-zero exit, timeout, decode error) so every caller's
    fallback is the honest `None`, never a false `False`."""
    exe = shutil.which(cmd[0])
    if exe is None:
        return None
    try:
        out = subprocess.run(
            [exe, *cmd[1:]], capture_output=True, timeout=PROBE_TIMEOUT_S,
            text=True, errors="replace",
            # Windows: never flash a console window during onboarding.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def detect_platform() -> str:
    """Slug for the running host. Android is checked FIRST because it reports
    `platform.system() == "Linux"` — treating a phone as a generic Linux box is
    exactly how a Core ends up ignoring Doze and dying overnight."""
    if "ANDROID_ROOT" in os.environ or "ANDROID_DATA" in os.environ:
        return "android"
    # Chaquopy / Termux both leave this fingerprint on sys.platform's sibling.
    if "android" in platform.release().lower():
        return "android"
    sysname = platform.system().lower()
    if sysname == "windows":
        return "windows"
    if sysname == "darwin":
        return "macos"
    if sysname == "linux":
        return "linux"
    return "unknown"


def _ram_mb() -> int | None:
    # Ordered cheapest-first; each returns None rather than guessing.
    try:                                     # Linux / Android
        with open("/proc/meminfo", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    if hasattr(os, "sysconf"):               # POSIX / macOS
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            size = os.sysconf("SC_PAGE_SIZE")
            if pages > 0 and size > 0:
                return (pages * size) // (1024 * 1024)
        except (ValueError, OSError, AttributeError):
            pass
    if sys.platform == "win32":              # Windows, no pywin32 dependency
        try:
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = _MemStatus()
            st.dwLength = ctypes.sizeof(_MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return int(st.ullTotalPhys) // (1024 * 1024)
        except Exception:      # noqa: BLE001 — a probe must never break the scan
            pass
    return None


def _has_battery() -> bool | None:
    """True/False/None. A machine with NO battery is a *better* Core candidate
    (permanent power), so getting this wrong in either direction misleads — hence
    the honest None when we cannot tell."""
    if sys.platform.startswith("linux"):
        try:
            supplies = os.listdir("/sys/class/power_supply")
        except OSError:
            return None
        for name in supplies:
            try:
                with open(f"/sys/class/power_supply/{name}/type", "r",
                          encoding="utf-8", errors="replace") as fh:
                    if fh.read().strip().lower() == "battery":
                        return True
            except OSError:
                continue
        return False       # the directory listed and held no Battery-type supply
    if sys.platform == "win32":
        try:
            import ctypes

            class _PowerStatus(ctypes.Structure):
                _fields_ = [("ACLineStatus", ctypes.c_byte),
                            ("BatteryFlag", ctypes.c_byte),
                            ("BatteryLifePercent", ctypes.c_byte),
                            ("SystemStatusFlag", ctypes.c_byte),
                            ("BatteryLifeTime", ctypes.c_ulong),
                            ("BatteryFullLifeTime", ctypes.c_ulong)]

            st = _PowerStatus()
            if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
                return None
            # BatteryFlag 128 == "no system battery"; 255 == "unknown".
            if st.BatteryFlag == 128:
                return False
            if st.BatteryFlag == 255:
                return None
            return True
        except Exception:      # noqa: BLE001
            return None
    if sys.platform == "darwin":
        out = _run(["pmset", "-g", "batt"])
        if out is None:
            return None
        return "InternalBattery" in out
    return None


# Substrings that identify an interface CLASS from its name. Names are
# user-renameable and locale-dependent, so these can only ever prove PRESENCE.
# Windows in particular ships "WiFi", "Wi-Fi" and "Wireless Network Connection"
# depending on version and OEM -- a real machine here was called plain "WiFi",
# which an earlier "wi-fi"-only match reported as *no Wi-Fi adapter*. That is
# precisely the fabricated-False this module's honesty rule forbids.
_WIFI_TOKENS = ("wifi", "wi-fi", "wlan", "wireless", "802.11", "wl")
_ETH_TOKENS = ("ethernet", "eth", "en0", "eno", "ens", "enp", "local area connection")


def _has_ethernet_and_wifi() -> tuple[bool | None, bool | None]:
    """(ethernet, wifi), each a TRISTATE.

    Interface-name heuristics only -- we deliberately do NOT bring up a socket,
    associate, or scan, because this runs before the operator has consented to
    anything.

    Crucially, a name that matches proves PRESENCE, but a name that does not
    match proves NOTHING: interface names are renameable and localised. So a
    non-match resolves to None ("couldn't tell"), never to False. The only way
    to get False here is to have enumerated interfaces successfully on a
    platform whose naming we trust (Linux/BSD kernel names) and found none."""
    names: list[str] = []
    if sys.platform.startswith("linux"):
        try:
            names = os.listdir("/sys/class/net")
        except OSError:
            names = []
    if not names and hasattr(socket, "if_nameindex"):
        try:
            names = [nm for _, nm in socket.if_nameindex()]
        except OSError:
            names = []

    if sys.platform == "win32":
        # `if_nameindex` on Windows yields numeric-ish names carrying no class
        # information, so ask netsh for the real adapter names.
        out = _run(["netsh", "interface", "show", "interface"])
        if out is None:
            return (None, None)
        text = out.lower()
        eth = True if any(t in text for t in _ETH_TOKENS) else None
        wifi = True if any(t in text for t in _WIFI_TOKENS) else None
        return (eth, wifi)

    if not names:
        return (None, None)
    low = [nm.lower() for nm in names]
    eth = any(nm.startswith(("eth", "en", "eno", "ens", "enp")) for nm in low)
    wifi = any(nm.startswith(("wlan", "wl", "wlp", "wifi")) for nm in low)
    if not sys.platform.startswith("linux"):
        # macOS names its Wi-Fi interface `en0`/`en1`, never `wl*`, so the
        # heuristic above returns a CONFIDENT False on every Mac -- the exact
        # fabricated negative already fixed for Windows, one branch down. Only
        # Linux kernel names are predictable enough to justify a negative.
        return (eth or None, True if wifi else None)
    return (eth, wifi)


def _has_bluetooth() -> bool | None:
    if sys.platform.startswith("linux"):
        try:
            return len(os.listdir("/sys/class/bluetooth")) > 0
        except OSError:
            return None
    if sys.platform == "win32":
        # No cheap stdlib probe for the RADIO itself that doesn't shell out to
        # PowerShell (slow, and noisy in front of a user). The presence of the
        # `bleak` package tells us Wavr COULD use Bluetooth here; it does not
        # prove a radio exists, so this is deliberately True-or-unknown and
        # never a confident False. Resolved without importing (see
        # `_module_present`) so the probe cannot pull WinRT into the process.
        return True if _module_present("bleak") else None
    if sys.platform == "darwin":
        return None
    return None


def _has_camera() -> bool | None:
    """Presence of a camera DEVICE — never opens it. Opening a camera during a
    capability scan would violate the boot-OFF invariant (ADR-0002) in the most
    literal way possible: the lens would light up before the operator agreed to
    anything."""
    if sys.platform.startswith("linux"):
        try:
            return any(n.startswith("video") for n in os.listdir("/dev"))
        except OSError:
            return None
    return None      # Windows/macOS: no probe that doesn't risk claiming the device


def _has_gpu() -> bool | None:
    if shutil.which("nvidia-smi"):
        return True
    if sys.platform.startswith("linux"):
        try:
            return any(n.startswith("card") for n in os.listdir("/dev/dri"))
        except OSError:
            return None
    return None


def _pi_model() -> str | None:
    """Raspberry Pi self-identifies here; nothing else does. Used both for the
    human-facing model string and to justify a MEDIUM tier on 1 GB of RAM."""
    for path in ("/sys/firmware/devicetree/base/model", "/proc/device-tree/model"):
        try:
            with open(path, "rb") as fh:
                return fh.read().decode("utf-8", "replace").strip("\x00 \n")
        except OSError:
            continue
    return None


def _compute_tier(ram_mb: int | None, cpus: int | None, plat: str) -> str:
    """Coarse bucket. Unknown RAM degrades to LOW rather than optimistically
    guessing — a wrong HIGH tells the operator to run camera inference on a Pi
    Zero and the product feels broken; a wrong LOW just under-sells."""
    if plat == "esp32":
        return TIER_MICRO
    if ram_mb is None:
        return TIER_LOW
    if ram_mb >= 7000 and (cpus or 0) >= 4:
        return TIER_HIGH
    if ram_mb >= 1800:
        return TIER_MEDIUM
    if ram_mb >= MIN_CORE_RAM_MB:
        return TIER_LOW
    return TIER_MICRO


def scan_host() -> CapabilityManifest:
    """Probe THIS machine. Never raises, never touches the network, never opens
    a camera or a radio.

    Every individual probe already degrades to None, and the whole body is
    additionally wrapped: this is called from inside first-run Space creation,
    where an exception on an exotic OS would abort the setup itself. A surprise
    yields a minimal-but-valid manifest instead of a failed onboarding."""
    try:
        return _scan_host()
    except Exception:      # noqa: BLE001 -- onboarding must survive any host
        import logging
        logging.warning("capability scan failed; reporting an empty manifest",
                        exc_info=True)
        return CapabilityManifest(
            platform=detect_platform(),
            functions_supported=(ROLE_NODE, ROLE_CLIENT))


def _scan_host() -> CapabilityManifest:
    plat = detect_platform()
    ram = _ram_mb()
    cpus = os.cpu_count()
    eth, wifi = _has_ethernet_and_wifi()
    battery = _has_battery()
    pi = _pi_model()

    caps: dict[str, bool | None] = {
        "wifi": wifi,
        "ethernet": eth,
        "ble": _has_bluetooth(),
        "bluetooth": _has_bluetooth(),
        "camera": _has_camera(),
        "gpu": _has_gpu(),
        "battery": battery,
        # "Permanent power" is the INVERSE of battery only when we actually know
        # there is no battery. Unknown battery => unknown permanence.
        "permanent_power": (None if battery is None else not battery),
        "docker": shutil.which("docker") is not None or None,
        "display": _has_display(plat),
        # Software abilities of this host, resolved by import rather than guessed.
        "mdns": _module_present("zeroconf"),
        "network_scan": True,     # every platform we support can ARP/ping-sweep
        "raw_socket": _can_raw_socket(),
        # NOT a radar probe. `pyserial` being importable says Wavr *could* talk
        # to a serial radar, not that one is plugged in -- and a False here would
        # claim the absence of hardware we never looked for. It is listed under
        # sensing hardware, and `recommend()` turns a True into the operator-
        # facing sentence "it has a radar sensor", so a package check must never
        # produce either answer on its own.
        "mmwave": None,
        # An honest, useful claim in its own right: it is what a Node needs to
        # host a USB radar, and unlike "mmwave" it is exactly what we measured.
        "serial_port_support": _module_present("serial"),
        "microphone": None,       # no probe that doesn't risk claiming the device
        "gps": True if plat in ("android", "ios") else None,
        "accelerometer": True if plat in ("android", "ios") else None,
        "uwb": None,
        "nfc": None,
        "usb": None,
        "wifi_csi": None,
    }

    tier = _compute_tier(ram, cpus, plat)
    # A Pi is a MEDIUM Core even at 1 GB: it has permanent power, an ethernet
    # port and no thermal cliff, which is what actually makes a Core survive.
    if pi and tier == TIER_LOW and (ram or 0) >= 900:
        tier = TIER_MEDIUM

    functions = [ROLE_NODE]                      # anything that runs this is a Node
    if _can_be_core(plat, ram, tier):
        functions.insert(0, ROLE_CORE)
    if caps["display"] is not False:             # unknown display still gets Client
        functions.append(ROLE_CLIENT)

    model = pi or platform.machine() or ""
    if plat == "windows" and not pi:
        model = os.environ.get("COMPUTERNAME", "") or model

    return CapabilityManifest(
        platform=plat,
        arch=platform.machine() or "",
        os_version=f"{platform.system()} {platform.release()}".strip(),
        functions_supported=tuple(functions),
        capabilities=caps,
        ram_mb=ram,
        cpu_count=cpus,
        compute_tier=tier,
        model=model[:96],
    )


def _has_display(plat: str) -> bool | None:
    if plat == "windows" or plat == "macos":
        return True
    if plat in ("android", "ios"):
        return True
    if plat == "linux":
        # A headless Pi has no DISPLAY/WAYLAND_DISPLAY. That does not stop it
        # being a Core — it stops it being a Client.
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return None


def _module_present(name: str) -> bool | None:
    """Is this optional dependency installed?

    Uses `importlib.util.find_spec`, which resolves the module WITHOUT executing
    it. That matters twice over: importing `bleak` on Windows drags in the whole
    WinRT stack (slow, in front of a first-run spinner), and this repo holds a
    hard invariant that heavy sensing extras (torch, cv2, bleak, pyserial, paho)
    are imported lazily and never by the base path. A capability probe asking
    "is it there?" must not be the thing that loads it.

    Returns None (unknown), never False, when resolution fails for a reason
    other than absence — a half-installed native extension is not an absence."""
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        # A missing PARENT package: genuinely absent.
        return False
    except (ImportError, ValueError):
        # A module that exists but cannot be resolved (a broken install, a
        # __spec__-less entry already in sys.modules). Absence is not proven,
        # so this is unknown -- as the docstring above has always claimed.
        return None
    except Exception:      # noqa: BLE001 — a broken finder on an exotic install
        return None


def _can_raw_socket() -> bool | None:
    """Raw sockets unlock ARP/DHCP sniffing. On Linux they need CAP_NET_RAW; on
    Windows they need admin. We test by *asking the OS*, not by assuming."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
    except PermissionError:
        return False
    except OSError:
        return None
    except (AttributeError, ValueError):
        return None
    s.close()
    if sys.platform == "win32":
        # Creating the socket succeeds for a non-elevated user on Windows; the
        # send is what fails. Without an elevation check this reports True on a
        # box where ARP/DHCP sniffing cannot work.
        try:
            import ctypes
            if not ctypes.windll.shell32.IsUserAnAdmin():
                return None
        except Exception:      # noqa: BLE001
            return None
    return True


def _can_be_core(plat: str, ram_mb: int | None, tier: str) -> bool:
    """Can this machine host the authoritative runtime at all?

    Deliberately permissive — "one old laptop / one Pi / one old phone" is the
    product promise (PRODUCT.md), so the bar is *can it hold the loop*, not *is
    it nice*. The recommendation below is where we express preference."""
    if plat == "esp32" or tier == TIER_MICRO:
        return False
    if plat == "ios":
        return False        # no background runtime we can keep alive; Client only
    if ram_mb is not None and ram_mb < MIN_CORE_RAM_MB:
        return False
    return True


# -- Recommendation ----------------------------------------------------------

@dataclass(frozen=True)
class Recommendation:
    """What this device should become, and — crucially — WHY. The `reasons` list
    is rendered verbatim to the operator: a recommendation the user cannot
    interrogate is indistinguishable from a guess (PRODUCT.md principle 1,
    "explain, don't just display")."""

    functions: tuple[str, ...]
    label: str
    reasons: tuple[str, ...] = ()
    # Functions the device *could* take on but we did not recommend, so the UI
    # can offer them as an override instead of hiding them.
    also_possible: tuple[str, ...] = ()
    # True when this device would make a poor PRIMARY Core but a fine standby —
    # a phone, a laptop that sleeps. Drives the "portable Core" UX (§31).
    prefer_standby: bool = False

    def to_dict(self) -> dict:
        return {
            "functions": list(self.functions), "label": self.label,
            "reasons": list(self.reasons), "also_possible": list(self.also_possible),
            "prefer_standby": self.prefer_standby,
        }


_LABELS = {
    ("client",): "Wavr Client",
    ("node",): "Wavr Node",
    ("core",): "Wavr Core",
    ("node", "client"): "Node + Client",
    ("core", "client"): "Core + Client",
    ("core", "node"): "Core + Node",
    ("core", "node", "client"): "Primary Core + Node + Client",
}


def recommend(manifest: CapabilityManifest, space_has_core: bool = False) -> Recommendation:
    """Turn a manifest into a proposed set of functions.

    `space_has_core` flips the recommendation from "be the Core" to "join the
    existing Core" — the same laptop is the right Primary Core in an empty Space
    and the wrong second Core in a Space that already has a healthy one."""
    reasons: list[str] = []
    functions: list[str] = []
    also: list[str] = []

    can_core = manifest.supports(ROLE_CORE)
    battery = manifest.capability("battery")
    permanent = manifest.capability("permanent_power")
    tier = manifest.compute_tier
    plat = manifest.platform

    # -- Core?
    if can_core and not space_has_core:
        functions.append(ROLE_CORE)
        if tier == TIER_HIGH:
            reasons.append("Plenty of memory and CPU — it can run camera analysis locally.")
        elif tier == TIER_MEDIUM:
            reasons.append("Enough memory and CPU to run the Wavr Core comfortably.")
        else:
            reasons.append("Modest hardware, but enough to run the Wavr Core.")
        if permanent is True:
            reasons.append("Always on mains power, so it can stay awake.")
    elif can_core and space_has_core:
        also.append(ROLE_CORE)
        reasons.append("This Space already has a Core, so this device joins it instead.")

    # -- Node?
    # The local network comes FIRST, and on its own is enough. Wavr's whole
    # premise is that the network is a spatial signal, not just plumbing: a box
    # with nothing but an address can already tell the Space which devices are
    # here and when they arrive. Everything below refines that; nothing replaces
    # it. Listing Wi-Fi as the fourth item in a flat "useful for sensing"
    # sentence had it exactly backwards.
    on_network = (manifest.capability("wifi") is True
                  or manifest.capability("ethernet") is True)
    refining = [k for k in ("camera", "ble", "mmwave", "microphone")
                if manifest.capability(k) is True]

    if on_network or refining:
        functions.append(ROLE_NODE)
        if on_network:
            reasons.append("It's on your network, so it can already help Wavr see "
                           "which devices are here and when they come and go.")
        if refining:
            pretty = {"camera": "a camera", "ble": "Bluetooth",
                      "mmwave": "a radar sensor", "microphone": "a microphone"}
            lead = "It also has " if on_network else "It has "
            reasons.append(lead + _join_human([pretty[k] for k in refining])
                           + ", which sharpens that into room-level presence.")
    elif ROLE_NODE in manifest.functions_supported:
        also.append(ROLE_NODE)

    # -- Client?
    if manifest.capability("display") is not False:
        functions.append(ROLE_CLIENT)
        reasons.append("It has a screen, so you can use Wavr on it directly.")
    else:
        reasons.append("No screen detected — you'll manage this device from another one.")

    # A battery-powered Core is a *portable* Core: legitimate and explicitly
    # supported (§31), but it should not silently become the thing the whole
    # house depends on.
    prefer_standby = bool(ROLE_CORE in functions and battery is True and permanent is not True)
    if prefer_standby:
        reasons.append(
            "It runs on battery, so Wavr will treat it as a Core that can move "
            "or go to sleep rather than the always-on one.")

    if not functions:
        functions = [ROLE_CLIENT]
        reasons.append("We couldn't detect much about this device, so we're "
                       "starting it as a Client. You can change this later.")

    key = tuple(f for f in (ROLE_CORE, ROLE_NODE, ROLE_CLIENT) if f in functions)
    label = _LABELS.get(key, " + ".join(k.capitalize() for k in key))
    if prefer_standby and ROLE_CORE in key:
        label = label.replace("Primary Core", "Portable Core")

    return Recommendation(
        functions=key, label=label, reasons=tuple(reasons),
        also_possible=tuple(dict.fromkeys(also)), prefer_standby=prefer_standby,
    )


def _join_human(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]
