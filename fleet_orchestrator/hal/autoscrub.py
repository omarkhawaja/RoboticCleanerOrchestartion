"""AutoScrub adapter: REST commands + MQTT/JSON telemetry every 30s.

Quirk handled here (per the OEM profile): GPS drift of +-2m indoors. We
simulate this by jittering the reported position with noise and smoothing
it with a short moving average before it's normalized -- callers never see
raw drift, only a stable position plus a confidence flag in `meta`.
"""
from __future__ import annotations

import random
import zlib
from collections import deque

from ..models import RobotStatus, Telemetry
from .base import RobotAdapter


class AutoScrubAdapter(RobotAdapter):
    MQTT_INTERVAL_MIN = 0.5  # telemetry pushed every 30 sim-seconds

    def __init__(self, spec, rng: random.Random | None = None):
        super().__init__(spec)
        self._rng = rng or random.Random(zlib.crc32(spec.robot_id.encode()))
        self._position_history: deque[float] = deque(maxlen=3)  # drift smoothing window (meters offset)

    def tick(self, minutes: float = 1.0) -> None:
        super().tick(minutes)
        # simulate one MQTT sample's worth of GPS jitter landing this tick
        self._position_history.append(self._rng.uniform(-2.0, 2.0))

    def status_query(self) -> Telemetry:
        drift_samples = list(self._position_history) or [0.0]
        smoothed_drift_m = sum(drift_samples) / len(drift_samples)
        return Telemetry(
            robot_id=self.spec.robot_id,
            timestamp=int(self._t),
            battery_pct=round(self.phys.battery_pct, 1),
            water_pct=None if self.phys.water_pct is None else round(self.phys.water_pct, 1),
            water_uncertain=False,  # AutoScrub reports water % directly, no uncertainty
            position=self.phys.position,
            status=self.phys.status,
            error_codes=list(self.phys.error_codes),
            meta={
                "transport": "MQTT/JSON",
                "gps_drift_smoothed_m": round(smoothed_drift_m, 2),
                "gps_raw_drift_flagged": abs(smoothed_drift_m) > 1.5,
            },
        )
