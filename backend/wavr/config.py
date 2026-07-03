from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()  # reads ./.env (git-ignored) if present


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
    )
