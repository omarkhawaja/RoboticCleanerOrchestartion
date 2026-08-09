"""Per-robot consumable consumption profiling.

Turns raw per-tick telemetry into normalized, comparable TRIP SEGMENTS --
one per continuous stretch of active use (CLEANING/OFFLINE_MISSION) -- so
a 45-minute trip and a 90-minute trip can be compared on equal footing:
percent consumed per 60 ACTIVE minutes, not a raw total, and not wall-clock
trip length (which is inflated by travel/escort/dock time that consumes
nothing). That normalization is the whole point of this module -- "60
minutes of vacuuming today vs. 60 minutes of vacuuming yesterday."

Two consumers:
  - Tier 1 (ETA in monitor.py): prefers a robot's own recently-observed
    rate over the OEM's nominal spec rate, once enough trip history exists
    to trust it -- so the ETA self-corrects as a robot's real consumption
    drifts from its nameplate spec.
  - Tier 2 (anomaly detection): compares a robot against its OWN earlier
    baseline (self-comparison -- catches a slow drift over many shifts,
    e.g. an aging battery or a slowly-worsening leak) and against
    same-OEM/same-model PEERS (catches a single bad unit that would look
    "normal" against its own short history but is clearly off next to
    robots doing the identical job).

Persisted as flat JSON (same pattern as persistence.py) so the store
accumulates across shifts -- "is this robot degrading" is structurally
impossible to answer from a single night, it needs multi-night history.
"""
from __future__ import annotations

import json
import os
import statistics
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

from .models import RobotStatus, Telemetry

TRIP_NORMALIZE_MINUTES = 60.0   # the common yardstick: "% used per 60 active minutes"
ACTIVE_STATUSES = (RobotStatus.CLEANING, RobotStatus.OFFLINE_MISSION)


@dataclass
class TripSegment:
    robot_id: str
    oem: str
    model: str
    shift_label: str          # which simulated shift this came from, e.g. "Tue#3"
    start_t: int
    end_t: int
    active_minutes: float
    battery_used_pct: float
    battery_rate_per_60min: float
    water_used_pct: Optional[float]
    water_rate_per_60min: Optional[float]


def extract_trip_segments(history: List[Telemetry], robot_id: str, oem: str, model: str,
                           shift_label: str = "") -> List[TripSegment]:
    """Walk one robot's chronological telemetry and cut it into continuous
    active-use segments. Each segment is exactly the span the robot was
    actually consuming a resource -- not travel, not escort wait, not a
    dock stop -- which is what makes the resulting rate comparable across
    trips of very different total length."""
    segments: List[TripSegment] = []
    seg_start_i: Optional[int] = None
    for i, telem in enumerate(history):
        active = telem.status in ACTIVE_STATUSES
        prev_active = history[i - 1].status in ACTIVE_STATUSES if i > 0 else False
        if active and not prev_active:
            seg_start_i = i
        ended = prev_active and not active
        trailing_open_segment = active and i == len(history) - 1
        if seg_start_i is not None and (ended or trailing_open_segment):
            # end the segment at the LAST active sample (i-1 when the
            # transition just happened), not the first non-active one --
            # the sample at `i` reflects a minute where nothing was
            # consumed, and including it overstates active_minutes by one.
            end_i = i - 1 if ended else i
            seg = _build_segment(history, seg_start_i, end_i, robot_id, oem, model, shift_label)
            if seg is not None:
                segments.append(seg)
            if ended:
                seg_start_i = None
    return segments


def _build_segment(history, start_i, end_i, robot_id, oem, model, shift_label) -> Optional[TripSegment]:
    start, end = history[start_i], history[end_i]
    active_minutes = end.timestamp - start.timestamp
    if active_minutes <= 0:
        return None
    battery_used = max(0.0, start.battery_pct - end.battery_pct)
    water_used = None
    if start.water_pct is not None and end.water_pct is not None:
        water_used = max(0.0, start.water_pct - end.water_pct)
    scale = TRIP_NORMALIZE_MINUTES / active_minutes
    return TripSegment(
        robot_id=robot_id, oem=oem, model=model, shift_label=shift_label,
        start_t=start.timestamp, end_t=end.timestamp, active_minutes=round(active_minutes, 1),
        battery_used_pct=round(battery_used, 2), battery_rate_per_60min=round(battery_used * scale, 2),
        water_used_pct=round(water_used, 2) if water_used is not None else None,
        water_rate_per_60min=round(water_used * scale, 2) if water_used is not None else None,
    )


def minutes_remaining(current_pct: float, threshold_pct: float, rate_per_60min: Optional[float]) -> Optional[float]:
    """Given a normalized rate (% per 60 active minutes), how many more
    ACTIVE minutes until `threshold_pct` is reached -- i.e. "estimated
    time to next stop" assuming the robot keeps actively cleaning
    uninterrupted from here. Doesn't try to account for travel/escort
    gaps between now and then; those aren't knowable in advance."""
    if rate_per_60min is None or rate_per_60min <= 0:
        return None
    usable_pct = max(0.0, current_pct - threshold_pct)
    return round(usable_pct * (TRIP_NORMALIZE_MINUTES / rate_per_60min), 1)


class ProfileStore:
    """Accumulates trip segments across shifts -- the "database" this is
    modeling, backed by flat JSON rather than a real database (see
    SPEC.md for why that's an intentionally scoped-down choice here)."""

    def __init__(self):
        self.segments: List[TripSegment] = []

    def record_shift(self, monitor, fleet: dict, shift_label: str = "") -> None:
        for rid, spec in fleet.items():
            hist = monitor.history.get(rid, [])
            self.segments.extend(extract_trip_segments(hist, rid, spec.oem.value, spec.model, shift_label))

    def robot_segments(self, robot_id: str) -> List[TripSegment]:
        return [s for s in self.segments if s.robot_id == robot_id]

    def peer_segments(self, oem: str, model: str, exclude_robot_id: Optional[str] = None) -> List[TripSegment]:
        return [s for s in self.segments if s.oem == oem and s.model == model and s.robot_id != exclude_robot_id]

    def _rates(self, segs: List[TripSegment], resource: str) -> List[float]:
        attr = f"{resource}_rate_per_60min"
        return [v for s in segs if (v := getattr(s, attr)) is not None]

    def learned_rate(self, robot_id: str, resource: str, window: int = 5) -> Optional[float]:
        """Mean of a robot's own most recent `window` trip segments'
        normalized rate -- the empirical rate to prefer over the OEM's
        nominal spec once there's enough history to trust it."""
        rates = self._rates(self.robot_segments(robot_id), resource)
        if not rates:
            return None
        return round(statistics.mean(rates[-window:]), 3)

    def flag_degradation(self, robot_id: str, resource: str, baseline_n: int = 3, recent_n: int = 3,
                          ratio_threshold: float = 1.25, min_total: int = 6) -> Optional[dict]:
        """Self-comparison: is this robot's RECENT normalized rate
        meaningfully worse than its own EARLIEST recorded baseline? An
        aging battery and a developing leak both show up identically here
        -- the same active-minutes now costs more % than it used to, for
        this exact robot, against itself. No peer or spec value needed."""
        rates = self._rates(self.robot_segments(robot_id), resource)
        if len(rates) < min_total:
            return None
        baseline = statistics.mean(rates[:baseline_n])
        recent = statistics.mean(rates[-recent_n:])
        if baseline <= 0:
            return None
        ratio = recent / baseline
        if ratio < ratio_threshold:
            return None
        return {
            "robot_id": robot_id, "resource": resource,
            "baseline_rate_per_60min": round(baseline, 2), "recent_rate_per_60min": round(recent, 2),
            "ratio": round(ratio, 2), "n_trips": len(rates),
        }

    def flag_peer_outlier(self, robot_id: str, oem: str, model: str, resource: str,
                           z_threshold: float = 1.5, ratio_fallback: float = 1.3,
                           min_peer_trips: int = 4, min_own_trips: int = 3) -> Optional[dict]:
        """Cross-robot comparison: same OEM + same model, is THIS unit's
        rate an outlier against its siblings' pooled rate? Catches a
        single bad unit that looks fine against its own short history but
        is clearly off next to robots doing the identical job. Falls back
        to a flat ratio check if the peer group has ~zero variance (a
        z-score is meaningless when the denominator is 0)."""
        own_rates = self._rates(self.robot_segments(robot_id), resource)
        peer_rates = self._rates(self.peer_segments(oem, model, exclude_robot_id=robot_id), resource)
        if len(own_rates) < min_own_trips or len(peer_rates) < min_peer_trips:
            return None
        own_mean = statistics.mean(own_rates)
        peer_mean = statistics.mean(peer_rates)
        peer_stdev = statistics.pstdev(peer_rates)
        if peer_stdev > 0.01:
            z = (own_mean - peer_mean) / peer_stdev
            flagged = z >= z_threshold
        else:
            z = None
            flagged = peer_mean > 0 and (own_mean / peer_mean) >= ratio_fallback
        if not flagged:
            return None
        return {
            "robot_id": robot_id, "resource": resource, "own_rate_per_60min": round(own_mean, 2),
            "peer_rate_per_60min": round(peer_mean, 2), "z_score": round(z, 2) if z is not None else None,
            "n_own_trips": len(own_rates), "n_peer_trips": len(peer_rates),
        }

    # -- persistence --------------------------------------------------------
    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump([asdict(s) for s in self.segments], f, indent=2)

    def load(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.segments = [TripSegment(**d) for d in data]


def render_profile_report(store: ProfileStore, fleet: dict) -> str:
    """Text block for the shift report: learned rate + degradation/peer
    flags per robot, per resource. Most single-shift runs will show
    "insufficient history" for everything -- that's correct and expected;
    these checks need several shifts of accumulated data to mean anything,
    which is the whole reason ProfileStore persists across runs instead of
    resetting every shift."""
    lines = ["-- Consumable Profile (learned, across shifts) --"]
    for rid in sorted(fleet):
        spec = fleet[rid]
        resources = ["battery"] + (["water"] if spec.water_hours is not None else [])
        for resource in resources:
            n = len(store._rates(store.robot_segments(rid), resource))
            learned = store.learned_rate(rid, resource)
            learned_str = f"{learned:.2f}%/60min" if learned is not None else "n/a"
            degr = store.flag_degradation(rid, resource)
            peer = store.flag_peer_outlier(rid, resource=resource, oem=spec.oem.value, model=spec.model)
            flags = []
            if degr:
                flags.append(f"DEGRADING (own baseline {degr['baseline_rate_per_60min']} -> "
                             f"{degr['recent_rate_per_60min']}, {degr['ratio']}x)")
            if peer:
                z_str = f"z={peer['z_score']}" if peer["z_score"] is not None else "ratio-based"
                flags.append(f"PEER OUTLIER (own {peer['own_rate_per_60min']} vs peer "
                             f"{peer['peer_rate_per_60min']}, {z_str})")
            flag_str = " | ".join(flags) if flags else "no flags"
            lines.append(f"  {rid} {resource:<8} n_trips={n:<3} learned_rate={learned_str:<14} {flag_str}")
    return "\n".join(lines)
