from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()  # reads ./.env (git-ignored) if present

# Default control allowlist (ADR-0005 §5): a SAFE, non-sensitive set of `domain.service`
# pairs. ONLY these may be actuated (unless WAVR_HA_ALLOWED_SERVICES overrides). It is
# deliberately narrow and excludes every sensitive domain (camera / lock /
# alarm_control_panel / media_player / cover / valve / siren) — those are additionally
# refused in code, AS IS the target entity (a switch/scene fronting a sensitive device).
# `scene.turn_on` is intentionally NOT here: a scene is an opaque bundle that can front a
# camera or lock, so the target-entity gate treats it as sensitive-by-default (audit HIGH-1).
DEFAULT_HA_ALLOWED_SERVICES = (
    "light.turn_on,light.turn_off,switch.turn_on,switch.turn_off"
)


@dataclass
class Config:
    db_path: str
    sim_interval: float
    fusion_threshold: float
    net_known_macs: set[str]
    net_interval: float
    net_grace: int
    net_scan_interval: float
    net_inventory: bool
    away_grace: int
    ruview_url: str
    ruview_room: str
    ruview_reconnect: float
    cam_interval: float
    cam_confidence: float
    mqtt_enabled: bool
    mqtt_host: str
    mqtt_port: int
    mqtt_prefix: str
    ha_discovery: bool
    gemini_api_key: str
    gemini_model: str
    narrate_enabled: bool
    house_map: str
    mmwave_port: str
    mmwave_room: str
    ble_known: dict[str, str]
    ble_room: str
    ble_rssi_min: int
    ble_interval: float
    # Multi-device client auth (ADR-0006) — opt-in, all default to loopback-only.
    multidevice: bool
    bind_host: str
    tls_cert: str
    tls_key: str
    port: int
    # Home Assistant read-side (ADR-0005) — the "brain on HA" READ half. LOCAL-ONLY:
    # the user's own HA on the LAN + a locally-stored token. Both empty => disabled.
    ha_url: str
    ha_token: str
    # Home Assistant CONTROL/WRITE side (ADR-0005) — the "brain on HA" WRITE half.
    # OPT-IN, default OFF: the control tool is inert unless `mcp_control` is on, so the
    # read-only default is preserved (nothing actuates). `ha_allowed_services` bounds
    # WHAT may be actuated to explicit `domain.service` pairs; anything not in the set
    # is refused (and sensitive domains are additionally refused in code — ADR-0005 §4).
    mcp_control: bool
    ha_allowed_services: set[str]
    # Self-hosted ntfy notifications (opt-in) — a short human alert POSTed to the
    # user's OWN ntfy topic on derived edge events only (house arrived/left,
    # rogue-device). Empty => disabled (default OFF).
    ntfy_url: str
    # Internet/gateway outage monitor (opt-in, LOCAL) — default OFF. Empty
    # `internet_check_host` => auto-guess the LAN gateway (never a fixed cloud
    # endpoint by default, so zero-egress-by-default holds even when this is on).
    internet_monitor: bool
    internet_check_host: str
    internet_check_interval: float
    internet_fail_threshold: int
    # Passive protocol collectors (defensive-inventory collectors) -- opt-in, default OFF.
    # mDNS/SSDP are standard/public (RFC 6762 / UPnP) LAN multicast listeners
    # feeding richer make/model/os signals into wavr.recog; the LOC-XML fetch
    # is a strictly more active probe than passive listening, so it is its OWN
    # separate opt-in flag on top of `net_ssdp`. `net_collect_duration` bounds
    # how long each scan cycle listens (both collectors run concurrently).
    net_mdns: bool
    net_ssdp: bool
    net_ssdp_location: bool
    net_collect_duration: float


def load_config() -> Config:
    return Config(
        db_path=os.getenv("WAVR_DB", "wavr.db"),
        sim_interval=float(os.getenv("WAVR_SIM_INTERVAL", "1.0")),
        fusion_threshold=float(os.getenv("WAVR_FUSION_THRESHOLD", "0.5")),
        net_known_macs={
            m.strip().replace("-", ":").lower()
            for m in os.getenv("WAVR_NET_MACS", "").split(",")
            if m.strip()
        },
        net_interval=float(os.getenv("WAVR_NET_INTERVAL", "15.0")),
        net_grace=int(os.getenv("WAVR_NET_GRACE", "2")),
        net_scan_interval=float(os.getenv("WAVR_NET_SCAN_INTERVAL", "30.0")),
        net_inventory=os.getenv("WAVR_NET_INVENTORY", "").lower() in ("1", "true", "yes"),
        away_grace=int(os.getenv("WAVR_AWAY_GRACE", "3")),
        ruview_url=os.getenv("WAVR_RUVIEW_URL", "ws://localhost:3000/ws/sensing"),
        ruview_room=os.getenv("WAVR_RUVIEW_ROOM", "sala"),
        ruview_reconnect=float(os.getenv("WAVR_RUVIEW_RECONNECT", "3.0")),
        cam_interval=float(os.getenv("WAVR_CAM_INTERVAL", "0.5")),
        cam_confidence=float(os.getenv("WAVR_CAM_CONFIDENCE", "0.4")),
        mqtt_enabled=os.getenv("WAVR_MQTT_ENABLED", "").lower() in ("1", "true", "yes"),
        mqtt_host=os.getenv("WAVR_MQTT_HOST", "localhost"),
        mqtt_port=int(os.getenv("WAVR_MQTT_PORT", "1883")),
        mqtt_prefix=os.getenv("WAVR_MQTT_PREFIX", "wavr"),
        ha_discovery=os.getenv("WAVR_HA_DISCOVERY", "").lower() in ("1", "true", "yes"),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_model=os.getenv("WAVR_GEMINI_MODEL", "gemini-1.5-flash"),
        narrate_enabled=os.getenv("WAVR_NARRATE_ENABLED", "").lower() in ("1", "true", "yes"),
        house_map=os.getenv("WAVR_HOUSE_MAP", ""),
        mmwave_port=os.getenv("WAVR_MMWAVE_PORT", ""),
        mmwave_room=os.getenv("WAVR_MMWAVE_ROOM", "sala"),
        ble_known={
            pair.split("=", 1)[0].strip().replace("-", ":").lower():
                (pair.split("=", 1)[1].strip() if "=" in pair else "")
            for pair in os.getenv("WAVR_BLE_KNOWN", "").split(",")
            if pair.split("=", 1)[0].strip()
        },
        ble_room=os.getenv("WAVR_BLE_ROOM", "casa"),
        ble_rssi_min=int(os.getenv("WAVR_BLE_RSSI_MIN", "-80")),
        ble_interval=float(os.getenv("WAVR_BLE_INTERVAL", "15.0")),
        # Multi-device (ADR-0006): default OFF -> zero behaviour change, loopback-only
        # exactly as today. `bind_host` is only honoured when multidevice is on; the
        # TLS paths are empty until Phase 2 (self-signed cert generation).
        multidevice=os.getenv("WAVR_MULTIDEVICE", "").lower() in ("1", "true", "yes"),
        bind_host=os.getenv("WAVR_BIND", "127.0.0.1"),
        tls_cert=os.getenv("WAVR_TLS_CERT", ""),
        tls_key=os.getenv("WAVR_TLS_KEY", ""),
        # Listen port for `python -m wavr.serve` (both plain and TLS modes).
        port=int(os.getenv("WAVR_PORT", "8000")),
        # HA read-side (ADR-0005): empty => disabled. Local HA URL + long-lived token.
        ha_url=os.getenv("WAVR_HA_URL", ""),
        ha_token=os.getenv("WAVR_HA_TOKEN", ""),
        # HA control-side (ADR-0005): default OFF -> control tool inert, read-only as
        # today. Allowlist unset -> the SAFE default set; set-but-empty -> deny all
        # (fail closed). Stored lowercased so the tool's gate compares case-insensitively.
        mcp_control=os.getenv("WAVR_MCP_CONTROL", "").lower() in ("1", "true", "yes"),
        ha_allowed_services={
            s.strip().lower()
            for s in os.getenv(
                "WAVR_HA_ALLOWED_SERVICES", DEFAULT_HA_ALLOWED_SERVICES
            ).split(",")
            if s.strip()
        },
        # ntfy (opt-in): empty => disabled. A full topic URL on the user's own
        # self-hosted ntfy server, e.g. http://nas.local:8080/wavr.
        ntfy_url=os.getenv("WAVR_NTFY_URL", ""),
        # Internet/gateway monitor (opt-in): default OFF. Empty check host =>
        # InternetMonitor auto-guesses the LAN gateway at construction time.
        internet_monitor=os.getenv("WAVR_INTERNET_MONITOR", "").lower() in ("1", "true", "yes"),
        internet_check_host=os.getenv("WAVR_INTERNET_CHECK_HOST", ""),
        internet_check_interval=float(os.getenv("WAVR_INTERNET_CHECK_INTERVAL", "15.0")),
        internet_fail_threshold=int(os.getenv("WAVR_INTERNET_FAIL_THRESHOLD", "3")),
        # Passive collectors (opt-in, default OFF): join the standard mDNS/SSDP
        # multicast groups and feed self-description signals into recog.
        net_mdns=os.getenv("WAVR_NET_MDNS", "").lower() in ("1", "true", "yes"),
        net_ssdp=os.getenv("WAVR_NET_SSDP", "").lower() in ("1", "true", "yes"),
        # A strictly more active probe (one same-LAN HTTP GET per host) on top
        # of passive SSDP listening -- its own opt-in, independent of net_ssdp.
        net_ssdp_location=os.getenv("WAVR_NET_SSDP_LOCATION", "").lower() in ("1", "true", "yes"),
        net_collect_duration=float(os.getenv("WAVR_NET_COLLECT_DURATION", "3.0")),
    )
