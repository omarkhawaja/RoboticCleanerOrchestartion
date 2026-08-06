"""FloorBot adapter: legacy HTTP polling (orchestrator polls every 60s) +
XML telemetry body. Reports water as coarse buckets (high/med/low/empty)
instead of a percentage.

Quirk handled here: the imprecise water signal. We keep the *true* water_pct
in PhysicalState (ground truth for the simulator) but the adapter only ever
exposes the bucket a real FB-200 would report, then derives an estimated
minutes-remaining range from that bucket with `water_uncertain=True` --
callers must treat it as a range, not a point estimate. That uncertainty is
a first-class part of the normalized schema, not swept under the rug.
"""
from __future__ import annotations

from ..models import RobotStatus, Telemetry
from .base import RobotAdapter

# bucket -> (pct_lower, pct_upper) boundaries against the true tank level
BUCKETS = [
    ("empty", 0.0, 8.0),
    ("low", 8.0, 33.0),
    ("med", 33.0, 66.0),
    ("high", 66.0, 100.01),
]


def pct_to_bucket(pct: float) -> str:
    for name, lo, hi in BUCKETS:
        if lo <= pct < hi:
            return name
    return "high"


def bucket_to_minutes_range(bucket: str, water_hours: float):
    total_min = water_hours * 60.0
    ranges = {
        "empty": (0, int(0.08 * total_min)),
        "low": (int(0.08 * total_min), int(0.33 * total_min)),
        "med": (int(0.33 * total_min), int(0.66 * total_min)),
        "high": (int(0.66 * total_min), int(total_min)),
    }
    return ranges[bucket]


class FloorBotAdapter(RobotAdapter):
    POLL_INTERVAL_MIN = 1.0  # orchestrator must poll ~every 60s; matches our tick resolution

    def __init__(self, spec, rng=None):
        super().__init__(spec)
        self.forced_bucket_override: str | None = None  # for injected anomaly scenarios

    def status_query(self) -> Telemetry:
        water_pct = self.phys.water_pct
        bucket = None
        water_uncertain = False
        minutes_range = None
        if water_pct is not None:
            bucket = self.forced_bucket_override or pct_to_bucket(water_pct)
            water_uncertain = True
            minutes_range = bucket_to_minutes_range(bucket, self.spec.water_hours)

        return Telemetry(
            robot_id=self.spec.robot_id,
            timestamp=int(self._t),
            battery_pct=round(self.phys.battery_pct, 1),
            water_pct=water_pct_estimate(minutes_range, self.spec.water_hours),
            water_uncertain=water_uncertain,
            position=self.phys.position,
            status=self.phys.status,
            error_codes=list(self.phys.error_codes),
            meta={
                "transport": "HTTP-poll/XML",
                "water_bucket": bucket,
                "water_minutes_remaining_est": minutes_range,
                "true_water_pct_simulator_only": round(water_pct, 1) if water_pct is not None else None,
            },
        )


def water_pct_estimate(minutes_range, water_hours) -> float:
    """Midpoint-of-bucket estimate, exposed as `water_pct` for convenience
    (e.g. dashboards) but ALWAYS paired with water_uncertain=True so callers
    know it's a derived estimate, not a sensor reading."""
    if minutes_range is None:
        return None
    total_min = water_hours * 60.0
    mid_min = (minutes_range[0] + minutes_range[1]) / 2.0
    return round(100.0 * mid_min / total_min, 1)
