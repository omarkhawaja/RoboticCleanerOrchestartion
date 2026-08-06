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
TRAVEL_MINUTES = 5
TRAVEL_BATTERY_PCT = 2.0
WATER_CYCLE_MINUTES = 10.0
SANITIZE_MINUTES = 15.0
CHARGE_FULL_MINUTES = 90.0  # 0% -> 100%


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
            self.battery_pct = min(100.0, self.battery_pct + (100.0 / CHARGE_FULL_MINUTES) * minutes)
        elif self.status == RobotStatus.WATER_CYCLE:
            if self.water_pct is not None:
                self.water_pct = min(100.0, self.water_pct + (100.0 / WATER_CYCLE_MINUTES) * minutes)
        elif self.status == RobotStatus.DOCK_SERVICE:
            # charging and water refill happen concurrently at the dock --
            # "a water stop may be combined with a battery top-up ... but
            # they are independent constraints" (each proceeds at its own rate)
            self.battery_pct = min(100.0, self.battery_pct + (100.0 / CHARGE_FULL_MINUTES) * minutes)
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
