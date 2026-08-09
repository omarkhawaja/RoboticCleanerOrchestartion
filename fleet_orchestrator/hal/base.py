"""Hardware Abstraction Layer: base adapter + shared physical simulation.

Design intent (see SPEC.md for the full rationale):

  * `RobotAdapter` is the ONLY interface the rest of the system (Scheduler,
    Dispatcher, Monitor, Replanner) is allowed to touch. It exposes exactly
    five commands (start_mission, pause, resume, return_to_dock,
    status_query) and always returns the normalized `Telemetry` schema.
  * Every OEM quirk (GPS drift, WebSocket drops, coarse water buckets,
    polling cadence) is handled *inside* the adapter subclass. Nothing about
    MQTT/gRPC/HTTP-XML ever leaks past this file.
  * `PhysicalState` is the shared "ground truth" physics engine (battery
    drain, water drain, charging curve) that all three adapters wrap --
    OEMs don't have different physics, they have different *reporting* of
    the same physics. Sharing it here (rather than duplicating per-adapter)
    is what makes a 4th OEM adapter a ~40 line file.
  * Adding OEM #4: subclass RobotAdapter, implement _read_raw()/_send_raw()
    however that OEM's transport works, register it in hal/registry.py.
    Nothing else in the codebase changes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

from ..models import RobotSpec, RobotStatus, Telemetry

DOCK = "DOCK"
TRAVEL_MINUTES = 5           # baseline transition cost, per the assignment spec
TRAVEL_BATTERY_PCT = 2.0
WATER_CYCLE_MINUTES = 10.0
SANITIZE_MINUTES = 15.0

# Scrubbers drive faster when NOT scrubbing. Traction-drive auto-scrubbers
# (the AS-900/AS-900H's own product family included) run a "transport mode"
# with the scrub deck and squeegee raised for open travel, and cap their
# speed only while the deck is down and actively scrubbing -- the cap
# exists so the brushes get enough dwell time and the squeegee has time to
# fully dry the floor, not because the drivetrain can't go faster. A
# dock-service round trip (heading back for water/battery, and the return
# leg once serviced) is the clearest case of pure transport-mode travel:
# deck fully raised and stowed, a known/repeated route, nothing to clean
# along the way. We model that leg faster than the assignment's baseline
# 5-minute transition -- assumed here at a ~40% reduction (a mid-range
# figure for transport-vs-working speed caps on this class of machine;
# not vendor-specified for AS-900 exactly, so treated as an estimate, not
# a spec value). Battery cost per transition is left unchanged (still
# TRAVEL_BATTERY_PCT) since higher speed plausibly draws more current over
# the same-ish distance -- there's no data suggesting transport mode is
# more energy-efficient at the pack level, only faster. See SPEC.md for
# the reasoning and the before/after schedule-slack numbers.
TRAVEL_MINUTES_DOCK = 3.0    # dock <-> zone leg specifically (transport mode both ways)

# Charging is NOT linear. Real Li-ion packs charge in two phases: constant
# current (CC) up to ~90%, fast, then constant voltage (CV) trickle-taper
# for the last 10%, slow -- tapering current is *how* CV charging avoids
# overvoltage damage near full, and that taper is what makes the last 10%
# disproportionately slow. The assignment's "90 minutes, 0% -> 100%" figure
# is the total, but that time is not spent evenly across the curve: getting
# to 90% takes ~1/3 of the total (30 min), the last 10% takes the other
# ~2/3 (60 min) -- so the CC phase runs ~18x faster (%/min) than the CV
# phase. See SPEC.md for the dispatch-policy implication (redeploy at 90%,
# don't wait out the CV tail) and hal/base.py's charge_pct_after /
# charge_minutes_to_target for the piecewise math.
CHARGE_FULL_MINUTES = 90.0     # 0% -> 100%, total, for reference/back-compat
CHARGE_CC_LIMIT_PCT = 90.0     # boundary between the fast (CC) and slow (CV) phases
CHARGE_CC_MINUTES = 30.0       # time to go 0% -> 90% (the fast phase)
CHARGE_CV_MINUTES = 60.0       # time to go 90% -> 100% (the slow taper)
CHARGE_CC_RATE = CHARGE_CC_LIMIT_PCT / CHARGE_CC_MINUTES              # %/min, ~3.0
CHARGE_CV_RATE = (100.0 - CHARGE_CC_LIMIT_PCT) / CHARGE_CV_MINUTES    # %/min, ~0.167

# Dispatch policy: redeploy once the fast CC phase is done rather than
# waiting out the slow CV taper for the last 10%. See SPEC.md #N.
CHARGE_DISPATCH_TARGET_PCT = 90.0


def charge_pct_after(start_pct: float, minutes: float) -> float:
    """Battery % after charging for `minutes` from `start_pct`, following
    the piecewise CC/CV curve. Used by PhysicalState.advance() so a single
    tick that straddles the 90% boundary still charges at the right rate
    on each side of it, not one rate blended across both."""
    pct = start_pct
    remaining = minutes
    if pct < CHARGE_CC_LIMIT_PCT and remaining > 0:
        cc_room_min = (CHARGE_CC_LIMIT_PCT - pct) / CHARGE_CC_RATE
        if remaining <= cc_room_min:
            return min(100.0, pct + CHARGE_CC_RATE * remaining)
        pct = CHARGE_CC_LIMIT_PCT
        remaining -= cc_room_min
    if remaining > 0:
        pct = min(100.0, pct + CHARGE_CV_RATE * remaining)
    return pct


def charge_minutes_to_target(start_pct: float, target_pct: float) -> float:
    """Minutes of charging needed to go from `start_pct` to `target_pct`
    along the same piecewise CC/CV curve. Inverse of charge_pct_after,
    used by the Dispatcher to size a dock-service stop."""
    if target_pct <= start_pct:
        return 0.0
    cc_start, cc_end = min(start_pct, CHARGE_CC_LIMIT_PCT), min(target_pct, CHARGE_CC_LIMIT_PCT)
    cc_time = max(0.0, cc_end - cc_start) / CHARGE_CC_RATE
    cv_start, cv_end = max(start_pct, CHARGE_CC_LIMIT_PCT), max(target_pct, CHARGE_CC_LIMIT_PCT)
    cv_time = max(0.0, cv_end - cv_start) / CHARGE_CV_RATE
    return cc_time + cv_time


@dataclass
class PhysicalState:
    """Ground-truth physics for one robot. Adapters read/mutate this; the
    orchestrator never sees it directly, only the Telemetry each adapter
    derives from it."""
    spec: RobotSpec
    battery_pct: float = 100.0
    water_pct: Optional[float] = None
    position: str = DOCK
    status: RobotStatus = RobotStatus.IDLE
    error_codes: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.spec.dry_only:
            self.water_pct = 100.0

    @property
    def battery_drain_per_min(self) -> float:
        return 100.0 / (self.spec.battery_hours * 60.0)

    @property
    def water_drain_per_min(self) -> float:
        if self.spec.water_hours is None:
            return 0.0
        return 100.0 / (self.spec.water_hours * 60.0)

    def advance(self, minutes: float) -> None:
        """Advance ground-truth physics by `minutes` given current status.
        Called every simulated minute regardless of whether the OEM is
        actually reporting telemetry right now (e.g. offline mission,
        WebSocket drop) -- the robot keeps physically operating."""
        if self.status in (RobotStatus.CLEANING, RobotStatus.OFFLINE_MISSION):
            self.battery_pct = max(0.0, self.battery_pct - self.battery_drain_per_min * minutes)
            if self.water_pct is not None:
                self.water_pct = max(0.0, self.water_pct - self.water_drain_per_min * minutes)
        elif self.status == RobotStatus.EN_ROUTE:
            pass  # travel cost is a flat debit applied once at transition (see Dispatcher), not per-minute
        elif self.status == RobotStatus.CHARGING:
            self.battery_pct = charge_pct_after(self.battery_pct, minutes)
        elif self.status == RobotStatus.WATER_CYCLE:
            if self.water_pct is not None:
                self.water_pct = min(100.0, self.water_pct + (100.0 / WATER_CYCLE_MINUTES) * minutes)
        elif self.status == RobotStatus.DOCK_SERVICE:
            # charging and water refill happen concurrently at the dock --
            # "a water stop may be combined with a battery top-up ... but
            # they are independent constraints" (each proceeds at its own rate)
            self.battery_pct = charge_pct_after(self.battery_pct, minutes)
            if self.water_pct is not None:
                self.water_pct = min(100.0, self.water_pct + (100.0 / WATER_CYCLE_MINUTES) * minutes)
        # IDLE / SANITIZING / ESCORT_WAIT / OFFLINE_MISSION(cleaning handled by
        # caller setting status=CLEANING even while offline) / ERROR / OFFLINE:
        # no resource change from this method.


class RobotAdapter(ABC):
    """Unified command + telemetry interface. One instance per physical robot."""

    def __init__(self, spec: RobotSpec):
        self.spec = spec
        self.phys = PhysicalState(spec=spec)
        self._t = 0  # sim clock, minutes since shift start

    # ---- unified command interface -----------------------------------
    def start_mission(self, zone_id: str) -> None:
        self.phys.position = zone_id
        self.phys.status = RobotStatus.CLEANING
        self.phys.error_codes = []

    def pause(self) -> None:
        if self.phys.status == RobotStatus.CLEANING:
            self.phys.status = RobotStatus.IDLE

    def resume(self) -> None:
        if self.phys.status == RobotStatus.IDLE and self.phys.position != DOCK:
            self.phys.status = RobotStatus.CLEANING

    def return_to_dock(self) -> None:
        self.phys.status = RobotStatus.EN_ROUTE
        self.phys.position = DOCK

    @abstractmethod
    def status_query(self) -> Telemetry:
        """Return the best-known normalized telemetry snapshot. Each OEM
        subclass decides how fresh that snapshot is allowed to be."""
        raise NotImplementedError

    # ---- simulation heartbeat ------------------------------------------
    def tick(self, minutes: float = 1.0) -> None:
        """Advance the simulated robot by one clock step. Subclasses may
        override to layer OEM-specific quirks (dropouts, drift) on top of
        the shared physics."""
        self._t += minutes
        self.phys.advance(minutes)

    def set_internal_status(self, status: RobotStatus) -> None:
        """Dock-side / dispatcher-orchestrated state transitions (charging,
        water cycle, sanitizing, escort wait, offline mission). These are
        NOT part of the 5-command OEM surface -- a real robot doesn't take a
        gRPC/REST "start charging" call, the dock does it automatically once
        docked. Only the Dispatcher should call this; Scheduler/Monitor/
        Replanner never touch adapters directly at all."""
        self.phys.status = status

    def force_error(self, code: str) -> None:
        self.phys.status = RobotStatus.ERROR
        if code not in self.phys.error_codes:
            self.phys.error_codes.append(code)
