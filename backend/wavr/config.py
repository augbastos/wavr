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
    # F3 camera IP-drift health: consecutive seconds a camera source must fail to
    # yield a frame before it is reported unhealthy (edge-triggered health hook).
    cam_unhealthy_secs: float
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
    # HA-import kill-switch (A4.1). Default ON: when HA is configured, the
    # user-triggered POST /api/ha/import may pull the local HA device registry
    # to enrich recog. Set WAVR_HA_IMPORT=0 for operators who want read-only HA
    # (mcp read/control) WITHOUT the registry-import path. Import is never
    # automatic/timed regardless -- this only gates the manual endpoint.
    ha_import: bool
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
    # NetBIOS/SNMP (defensive-inventory #5/#8) -- active, TARGETED unicast probes (not
    # passive multicast listeners like mdns/ssdp), sent only to hosts THIS
    # scan cycle's ARP sweep already resolved (never their own subnet sweep).
    # Opt-in, default OFF. `net_netbios_scope_known_only`/
    # `net_snmp_scope_known_only` default ON -- audit fix #4: unlike
    # `netutils.WAVR_NET_PORTSCAN_SCOPE` (a connect-only pass), an ACTIVE
    # unicast NBSTAT/SNMP probe is a more intrusive footprint on a
    # shared/guest subnet, so it defaults to the known-MAC allowlist only;
    # set `WAVR_NET_NETBIOS_SCOPE`/`WAVR_NET_SNMP_SCOPE=all` to explicitly
    # widen to every ARP-discovered host. `net_snmp_community` is
    # read-only-by-construction (the collector has no SET-Request encoder)
    # and is never logged.
    net_netbios: bool
    net_netbios_scope_known_only: bool
    net_snmp: bool
    net_snmp_community: str
    net_snmp_scope_known_only: bool
    # DHCP option-55/60 fingerprint (defensive-inventory #6) -- passive listener,
    # same opt-in/default-OFF rule as mdns/ssdp.
    net_dhcp_fp: bool
    # Reverse-DNS hostname resolution (gateway-anchored PTR) -- opt-in, default
    # OFF. Feeds the recog hostname signal with real device names via the LAN
    # gateway resolver (wavr.hostname_resolver). LOCAL-ONLY: queries only the
    # gateway, never a public resolver.
    net_hostnames: bool
    # Per-device LAN latency probe (WiFiman-style live ping, wifiman.md #1) --
    # opt-in, default OFF. Actively TCP-connects each host (netutils.ping_host),
    # same shared-subnet footprint class as the port pass, so it is gated.
    net_latency: bool
    # Rogue / multiple-DHCP-server detector (defensive-inventory #7) -- opt-in,
    # default OFF. `net_dhcp_probe` is a SECOND opt-in on top (an active
    # broadcast DHCPDISCOVER), same "active probing is opt-in on top of
    # opt-in" rule as `net_ssdp_location`. `net_dhcp_known_servers` seeds the
    # allowlist baseline (e.g. the router's own IP); empty => auto-baseline
    # on first cycle (see wavr.dhcp_monitor.RogueDhcpMonitor docstring).
    net_dhcp_monitor: bool
    net_dhcp_probe: bool
    net_dhcp_known_servers: set[str]
    net_dhcp_interval: float
    net_dhcp_alert_threshold: int
    # Gateway-MAC-identity tracker (gateway-identity-rogue-dhcp, inventory feature #2).
    # ON by default -- unlike every active collector it opens NO socket and makes
    # ZERO egress (it only reads the is_gateway binding the routing-table pass
    # already produced), and it is Wavr's headline privacy differentiator vs
    # a proprietary tool's cloud-brained version, so it earns being on out of the box. Set
    # WAVR_NET_GATEWAY_MONITOR=0 to disable. `net_gateway_known_macs` seeds the
    # trusted-gateway-MAC allowlist (router swap / failover pair); empty =>
    # auto-learn the first-seen gateway MAC, persisted across restarts.
    net_gateway_monitor: bool
    net_gateway_known_macs: set[str]
    # Health-check ladder (defensive-inventory #12) -- extra operator-configured
    # targets on top of the fixed gateway + public-resolver checks. Empty by
    # default (no extra egress beyond the resolver checks themselves).
    health_extra_targets: tuple[str, ...]
    # Collectors-lote2 audit fix #1: the resolver legs (1.1.1.1/8.8.8.8/9.9.9.9)
    # are the ONLY part of `GET /api/health` that makes real public-internet
    # egress. Every other Wavr egress path is opt-in + surfaced in
    # `/api/status.features` -- this one wasn't, so a bare Docker HEALTHCHECK/
    # k8s liveness probe/uptime monitor hitting the route would silently ping
    # three US cloud providers. Default OFF (empty resolver dict -> severity
    # comes from gateway + extra targets only); set WAVR_HEALTH_RESOLVERS=1 to
    # opt in to the full 5-tier ladder.
    health_resolvers_enabled: bool
    # Standalone tools (A3) -- all opt-in, default OFF, surfaced in
    # /api/status.features. `net_wol` gates POST /api/wol (a LAN-local WoL
    # actuator, zero egress). `net_diagnostics` gates the ping/traceroute/dns
    # family (LAN/local). `net_speedtest` (also read by netutils.speedtest_
    # enabled) is the ONE sanctioned external egress -- gated additionally by a
    # per-invocation confirm=true and, for the IP-publishing M-Lab path, by
    # `speedtest_provider=ndt7` (default `cloudflare`, the lower-disclosure
    # option). The single WAVR_NET_SPEEDTEST flag alone can never publish the IP.
    net_wol: bool
    net_diagnostics: bool
    net_speedtest: bool
    speedtest_provider: str
    # ONVIF camera probe (A4.2) -- opt-in, default OFF. Gates POST /api/onvif/probe,
    # an ACTIVE WS-Discovery multicast probe + unicast SOAP calls that auto-discovers
    # LAN cameras and pre-fills their RTSP URL for the rung-2 add form. Same "active
    # probing is opt-in on top of opt-in" rule as `net_ssdp_location`: a strictly more
    # active probe than the passive collectors. SSRF-hard (the probe only ever contacts
    # LAN-IP-literal hosts) and it NEVER auto-adds a camera -- the user still confirms
    # via POST /api/cameras, and cameras always boot OFF.
    net_onvif_probe: bool
    # ONVIF PTZ actuator (A4.3) -- opt-in, default OFF. Gates the /api/ptz/* routes that
    # actively MOVE a stored camera (ContinuousMove/Stop/GotoPreset via ONVIF at host:2020).
    # PTZ is the first camera ACTUATOR in Wavr: default-off, require_local + master camera
    # kill-switch honoured, LAN-IP-only (SSRF-hard), creds read only from the stored rtsp_url
    # (never accepted over the PTZ API, never logged/echoed). No frame is ever read here.
    ptz: bool
    # A5.1 local-API hardening (defense-in-depth, DEFAULT-OFF / no-op when unset).
    # `local_token`: optional same-machine shared secret required on /api/* even on
    # loopback ("" = disabled = today's behavior; "auto" = generate+persist+print once).
    # Defends against OTHER local processes / a malicious http://127.0.0.1 page -- NOT a
    # hard boundary (a same-box process that can read the shell/file gets it).
    # `api_v1`: mount a /api/v1 alias of the identical routes (versioning, default OFF).
    local_token: str
    api_v1: bool
    # A5.2 ARP device blocking (WAVR_NET_BLOCKING) -- the roadmap's single active-LAN-
    # attack primitive. DEFAULT-OFF. Triple-gated at the route (flag + require_local +
    # per-call confirm), inventory-only target denylist, auto-expiry, MCP-excluded.
    net_blocking: bool


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
        # F3: seconds of consecutive frame-read failure before a camera is reported
        # unhealthy (drives the drift-detection health hook). Default 30s.
        cam_unhealthy_secs=float(os.getenv("WAVR_CAM_UNHEALTHY_SECS", "30")),
        mqtt_enabled=os.getenv("WAVR_MQTT_ENABLED", "").lower() in ("1", "true", "yes"),
        mqtt_host=os.getenv("WAVR_MQTT_HOST", "localhost"),
        mqtt_port=int(os.getenv("WAVR_MQTT_PORT", "1883")),
        mqtt_prefix=os.getenv("WAVR_MQTT_PREFIX", "wavr"),
        ha_discovery=os.getenv("WAVR_HA_DISCOVERY", "").lower() in ("1", "true", "yes"),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_model=os.getenv("WAVR_GEMINI_MODEL", "gemini-1.5-flash"),
        narrate_enabled=os.getenv("WAVR_NARRATE_ENABLED", "").lower() in ("1", "true", "yes"),
        # F1: default to a bare cwd-relative "house.json" (mirrors db_path="wavr.db"
        # above) so PUT /api/house works out-of-the-box instead of 409-ing for every
        # fresh install. The env override is preserved; an operator who explicitly sets
        # WAVR_HOUSE_MAP="" still gets the 409 (no path configured) branch. The real
        # home floor plan this creates is git-ignored (house.json) -- it must NEVER land
        # in this public AGPL repo (trap #1, same class as the wavr.db-wal PII leak).
        house_map=os.getenv("WAVR_HOUSE_MAP", "house.json"),
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
        # HA-import kill-switch (A4.1): default ON (only reachable once HA is
        # configured + require_local passes). Set WAVR_HA_IMPORT=0 to disable.
        ha_import=os.getenv("WAVR_HA_IMPORT", "1").strip().lower()
            in ("1", "true", "yes", "on"),
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
        # NetBIOS/SNMP active probes (opt-in, default OFF); scope defaults to
        # known-MAC-only (audit fix #4) -- explicit SCOPE=all is required to
        # widen to every ARP-discovered host (SCOPE=known is still accepted,
        # same as the known-only default it now names explicitly).
        net_netbios=os.getenv("WAVR_NET_NETBIOS", "").lower() in ("1", "true", "yes"),
        net_netbios_scope_known_only=os.getenv("WAVR_NET_NETBIOS_SCOPE", "").strip().lower() != "all",
        net_snmp=os.getenv("WAVR_NET_SNMP", "").lower() in ("1", "true", "yes"),
        net_snmp_community=os.getenv("WAVR_NET_SNMP_COMMUNITY", "public"),
        net_snmp_scope_known_only=os.getenv("WAVR_NET_SNMP_SCOPE", "").strip().lower() != "all",
        # DHCP fingerprint passive collector (opt-in, default OFF).
        net_dhcp_fp=os.getenv("WAVR_NET_DHCP_FP", "").lower() in ("1", "true", "yes"),
        # Reverse-DNS hostname resolution (opt-in, default OFF).
        net_hostnames=os.getenv("WAVR_NET_HOSTNAMES", "").lower() in ("1", "true", "yes"),
        # Per-device latency probe (WiFiman-style live ping) -- opt-in, default OFF.
        net_latency=os.getenv("WAVR_NET_LATENCY", "").lower() in ("1", "true", "yes"),
        # Rogue/multiple-DHCP-server detector (opt-in, default OFF).
        net_dhcp_monitor=os.getenv("WAVR_NET_DHCP_MONITOR", "").lower() in ("1", "true", "yes"),
        net_dhcp_probe=os.getenv("WAVR_NET_DHCP_PROBE", "").lower() in ("1", "true", "yes"),
        net_dhcp_known_servers={
            s.strip() for s in os.getenv("WAVR_NET_DHCP_KNOWN_SERVERS", "").split(",") if s.strip()
        },
        net_dhcp_interval=float(os.getenv("WAVR_NET_DHCP_INTERVAL", "30.0")),
        net_dhcp_alert_threshold=int(os.getenv("WAVR_NET_DHCP_ALERT_THRESHOLD", "2")),
        # Gateway-MAC-identity tracker: ON by default (zero-egress, on-box).
        net_gateway_monitor=os.getenv("WAVR_NET_GATEWAY_MONITOR", "1").strip().lower()
            in ("1", "true", "yes", "on"),
        net_gateway_known_macs={
            m.strip().replace("-", ":").lower()
            for m in os.getenv("WAVR_NET_GATEWAY_MACS", "").split(",") if m.strip()
        },
        # Health-check ladder: extra targets beyond gateway + public resolvers.
        health_extra_targets=tuple(
            s.strip() for s in os.getenv("WAVR_HEALTH_EXTRA_TARGETS", "").split(",") if s.strip()
        ),
        # Audit fix #1: the public-resolver egress leg is opt-in, default OFF.
        health_resolvers_enabled=os.getenv("WAVR_HEALTH_RESOLVERS", "").lower() in ("1", "true", "yes"),
        # Standalone tools (A3) -- opt-in, default OFF. `speedtest_provider`
        # defaults to the lower-disclosure `cloudflare`; only `ndt7` reaches the
        # IP-publishing M-Lab path, and only alongside confirm=true at the route.
        net_wol=os.getenv("WAVR_NET_WOL", "").lower() in ("1", "true", "yes", "on"),
        net_diagnostics=os.getenv("WAVR_NET_DIAGNOSTICS", "").lower() in ("1", "true", "yes", "on"),
        net_speedtest=os.getenv("WAVR_NET_SPEEDTEST", "").lower() in ("1", "true", "yes", "on"),
        speedtest_provider=(
            os.getenv("WAVR_SPEEDTEST_PROVIDER", "cloudflare").strip().lower()
            if os.getenv("WAVR_SPEEDTEST_PROVIDER", "cloudflare").strip().lower()
            in ("cloudflare", "ndt7") else "cloudflare"
        ),
        # ONVIF camera probe (A4.2) -- opt-in, default OFF (active probe on top of
        # the passive collectors).
        net_onvif_probe=os.getenv("WAVR_ONVIF_PROBE", "").lower() in ("1", "true", "yes", "on"),
        # ONVIF PTZ actuator (A4.3) -- opt-in, default OFF (first camera ACTUATOR).
        ptz=os.getenv("WAVR_PTZ", "").lower() in ("1", "true", "yes", "on"),
        # A5.1 local-API hardening -- default-off / no-op when unset.
        local_token=os.getenv("WAVR_LOCAL_TOKEN", "").strip(),
        api_v1=os.getenv("WAVR_API_V1", "").lower() in ("1", "true", "yes", "on"),
        # A5.2 ARP blocking -- default OFF (active-LAN-attack primitive, triple-gated).
        net_blocking=os.getenv("WAVR_NET_BLOCKING", "").lower() in ("1", "true", "yes", "on"),
    )
