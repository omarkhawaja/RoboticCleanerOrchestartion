"""FloorBot adapter: legacy HTTP polling (orchestrator polls every 60s) +
XML telemetry body. Reports water as coarse buckets (high/med/low/empty)
instead of a percentage.

Quirk handled here: the imprecise water signal. We keep the *true* water_pct
in PhysicalState (ground truth for the simulator, standing in for what the
robot's own sensor is doing internally) but the adapter only ever exposes
what a real FB-200 would report: a coarse bucket, plus a derived estimate
built from the orchestrator's OWN knowledge, not the robot's.

That derived estimate is a **usage-time model, calibrated by the bucket**,
not just the bucket's midpoint. The orchestrator already knows exactly how
long this robot has been actively cleaning since its last confirmed refill
(same continuous signal AutoScrub/CleanPath get for free from a precise
sensor) and the OEM's rated water_hours -- exactly the "roughly how long
it's been on" reasoning used elsewhere in this system, applied here to
turn a 4-value discrete signal into a continuously-updating one between
readings. The bucket still gates the estimate (clamped to the bucket's
known range every time a fresh reading comes in), so this is a *fusion* of
two signals -- a fast-drifting time-based model and a slow, coarse, but
"real" sensor checkpoint -- not a replacement of one by the other, and it's
still exposed with `water_uncertain=True`. See SPEC.md for the reasoning,
including why this doesn't change the conservative return-trigger policy
in dispatcher.py (that still keys off the bucket alone, deliberately).
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


def bucket_pct_bounds(bucket: str):
    """The bucket's own percentage range (not minutes) -- used to clamp the
    usage-time model estimate so it can never wander further than what the
    robot's actual coarse sensor reading supports."""
    for name, lo, hi in BUCKETS:
        if name == bucket:
            return lo, min(hi, 100.0)
    return 0.0, 100.0


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
        self._active_minutes_since_refill = 0.0

    def tick(self, minutes: float = 1.0) -> None:
        super().tick(minutes)
        status = self.phys.status
        # Accrue active-cleaning time for the usage-time water model. Reset
        # is tied directly to the physical tank being (essentially) full,
        # not to a status-transition heuristic -- a status-based reset has
        # a one-tick ordering lag against when the true tank actually tops
        # out mid-refill, which produced a spurious divergence flag right
        # at the refill/travel boundary. Tying it to the physics instead
        # means the usage clock re-zeroes exactly when there's actually
        # nothing to account for yet, no matter what phase the dispatcher
        # is in at that instant.
        if status in (RobotStatus.CLEANING, RobotStatus.OFFLINE_MISSION):
            self._active_minutes_since_refill += minutes
        elif self.phys.water_pct is not None and self.phys.water_pct >= 99.5:
            self._active_minutes_since_refill = 0.0

    def status_query(self) -> Telemetry:
        water_pct = self.phys.water_pct
        bucket = None
        water_uncertain = False
        minutes_range = None
        fused_pct = None
        usage_model_pct = None
        drift_pct = None
        if water_pct is not None:
            bucket = self.forced_bucket_override or pct_to_bucket(water_pct)
            water_uncertain = True
            minutes_range = bucket_to_minutes_range(bucket, self.spec.water_hours)

            total_min = self.spec.water_hours * 60.0
            usage_model_pct = max(0.0, 100.0 * (1.0 - self._active_minutes_since_refill / total_min))
            lo_pct, hi_pct = bucket_pct_bounds(bucket)
            fused_pct = min(max(usage_model_pct, lo_pct), hi_pct)  # snap to what the sensor actually confirms
            drift_pct = round(usage_model_pct - fused_pct, 1)

        return Telemetry(
            robot_id=self.spec.robot_id,
            timestamp=int(self._t),
            battery_pct=round(self.phys.battery_pct, 1),
            water_pct=round(fused_pct, 1) if fused_pct is not None else None,
            water_uncertain=water_uncertain,
            position=self.phys.position,
            status=self.phys.status,
            error_codes=list(self.phys.error_codes),
            meta={
                "transport": "HTTP-poll/XML",
                "water_bucket": bucket,
                "water_minutes_remaining_est": minutes_range,
                "water_usage_model_pct": round(usage_model_pct, 1) if usage_model_pct is not None else None,
                "water_model_bucket_drift_pct": drift_pct,
                "true_water_pct_simulator_only": round(water_pct, 1) if water_pct is not None else None,
            },
        )
