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


def load_config() -> Config:
    return Config(
        db_path=os.getenv("WAVR_DB", "wavr.db"),
        sim_interval=float(os.getenv("WAVR_SIM_INTERVAL", "1.0")),
        fusion_threshold=float(os.getenv("WAVR_FUSION_THRESHOLD", "0.5")),
    )
