"""Real-time dispatch: a per-minute discrete-event simulation that drives
every robot's finite-state machine through its assigned tasks, inserting
battery/water/sanitization/escort breaks organically as the physics (and
telemetry uncertainty) demand -- this is where the dual-constraint
scheduling promise actually gets kept, not in the static plan.

Each RobotController only ever talks to its robot through the HAL
(`RobotAdapter`). It never imports an OEM-specific module and never reaches
into `PhysicalState` except via `set_internal_status`, which is the one
dock-side exception documented in hal/base.py.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .hal.base import (
    DOCK,
    RobotAdapter,
    SANITIZE_MINUTES,
    TRAVEL_BATTERY_PCT,
    TRAVEL_MINUTES,
    TRAVEL_MINUTES_DOCK,
    WATER_CYCLE_MINUTES,
    CHARGE_DISPATCH_TARGET_PCT,
    charge_minutes_to_target,
)
from .models import RobotSpec, RobotStatus, Telemetry, Zone, ZoneStatus, fmt_time
from .scheduler import Assignment

BATTERY_RETURN_PCT = 12.0     # 10% safety floor + 2% headroom, ADDED to the tracked trip-cost
                               # estimate below -- see RobotController.battery_used_since_dock
# Precise-sensor (AutoScrub) water return floor. Water does NOT drain during
# the EN_ROUTE trip back to the dock (see PhysicalState.advance -- travel is
# a flat one-time debit, not a per-minute rate, and it's battery-only), so
# there's no "reserve enough to make the trip" need the way there is for
# battery. The only real constraint is this system's 1-minute tick
# resolution: `wants_return_now()` is checked once per simulated minute,
# right after that minute's drain, so the floor must be at least one tick's
# worth of drain or a robot could be asked to clean through a tick that
# empties the tank mid-pass. All wet robots share a 1.5h tank -> ~1.11%/min
# drain, so 1.5% is ~1 tick of margin above zero plus a small safety
# cushion for floating-point rounding -- deliberately NOT the old 4.0%,
# which forfeited ~3.6 min of usable cleaning time per water-bound stop
# (out of ~320 stops/140 shifts measured in analysis/binding_constraint_
# study.py) for no corresponding safety benefit. See SPEC.md #22.
WATER_RETURN_PCT = 1.5
ESCORT_ZONE_HOUR_START = 240  # 11:00 PM in shift-minutes -> escort required after this


class RobotController:
    def __init__(self, spec: RobotSpec, adapter: RobotAdapter, tasks: List[Assignment],
                 zones: Dict[str, Zone], rng: random.Random, monitor=None,
                 zone_progress: Optional[Dict[str, float]] = None):
        self.spec = spec
        self.adapter = adapter
        self.tasks = list(tasks)
        self.task_idx = 0
        self.zones = zones
        self.rng = rng
        self.monitor = monitor
        # Shared across every controller (same dict object) so that N robots
        # cleaning the same zone concurrently draw down ONE remaining-sqft
        # pool instead of each independently believing it owns the whole
        # zone -- otherwise multi-robot assignment wouldn't actually cut
        # completion time, it'd just have robots redundantly re-clean.
        self.zone_progress: Dict[str, float] = zone_progress if zone_progress is not None else {}

        self.current: Optional[Assignment] = None
        self.phase = "IDLE"
        self.phase_timer = 0.0
        # Dock is non-sterile (it's a maintenance area, not a patient-care
        # zone), so a robot's very first zone of the night is a real
        # non-sterile->sterile transition if that first zone is sterile --
        # not "no transition happened yet." Seeding this as "Standard"
        # rather than None is what makes the sanitization cycle actually
        # fire before a sterile-certified robot's first sterile zone of the
        # shift, matching the assignment's own sample timeline ("11:00 PM
        # -- R-003 begins sterile zones. Sanitization cycle before Z7").
        self.last_classification: str = "Standard"
        self.resume_zone: Optional[str] = None  # zone to travel back to after a dock stop
        self.pending_binding_reason: Optional[str] = None

        # Running estimate of "how much battery it would cost to get back to
        # the dock right now" -- tracked as the battery actually spent on
        # travel legs since the robot last left a real dock-service stop,
        # not a hardcoded flat constant. Reset to 0 on arrival at the dock
        # (_arrive_at_dock), accumulated by TRAVEL_BATTERY_PCT on every
        # travel leg since (_begin_travel). See wants_return_now() and
        # SPEC.md "Battery return threshold" for the assumption this
        # encodes.
        self.battery_used_since_dock: float = 0.0

        self.disabled = False
        self.escort_override: Dict[str, float] = {}
        self.offline_active = False
        self.offline_buffer: List[Telemetry] = []
        self.offline_return_t: Optional[float] = None
        self.travel_destination = "ZONE"  # "ZONE" or "DOCK", set before entering TRAVEL phase

        self.stats = {"water_cycles": 0, "charge_cycles": 0, "battery_bound_stops": 0,
                       "water_bound_stops": 0, "travel_events": 0}
        self.events: List[str] = []  # human-readable log for the shift narrative

    # -- helpers ----------------------------------------------------------
    def _log(self, t: float, msg: str):
        self.events.append(f"[{fmt_time(int(t))}] {self.spec.robot_id}: {msg}")

    def _next_task(self, t: float) -> Optional[Assignment]:
        while self.task_idx < len(self.tasks):
            a = self.tasks[self.task_idx]
            self.task_idx += 1
            zone = self.zones[a.zone_id]
            if t > zone.window_end:
                self._log(t, f"skipped {a.zone_id} ({a.role}) -- window already closed")
                continue
            return a
        return None

    def wants_return_now(self, telem: Telemetry, zone: Zone) -> Optional[str]:
        """Returns 'battery' or 'water' if the robot must head to the dock
        now, else None.

        Battery policy: the trigger is NOT a flat percentage. It's "however
        much battery it would cost to get back to the dock from here, plus
        a 10% safety floor plus a 2% headroom cushion" --
        `self.battery_used_since_dock + BATTERY_RETURN_PCT`. The trip-cost
        term is measured, not assumed: it's the battery actually spent on
        travel legs since the robot's last real dock-service stop (see
        `_begin_travel` / `_arrive_at_dock`), so a robot that has hopped
        zone-to-zone several times without a dock visit correctly reserves
        more margin than one that just left the dock for its first zone of
        the run. Checked every simulated minute (this system's tick
        resolution), same cadence as every other telemetry check.

        Water-uncertainty policy: for FloorBot's coarse bucket reporting we
        act on the *conservative* edge (trigger as soon as the bucket reads
        'low', not 'empty') rather than the midpoint estimate -- see
        SPEC.md 'FloorBot water uncertainty' for the aggressive-vs-
        conservative tradeoff. For AutoScrub's precise sensor, the floor is
        `WATER_RETURN_PCT` (1.5%) -- just above one tick's worth of drain,
        unlike the battery floor there's no travel-back reserve needed
        (water doesn't drain in transit), see WATER_RETURN_PCT's own
        comment and SPEC.md #22."""
        if telem.battery_pct <= self.battery_used_since_dock + BATTERY_RETURN_PCT:
            return "battery"
        if telem.water_pct is not None:
            if telem.water_uncertain:
                bucket = telem.meta.get("water_bucket")
                if bucket in ("low", "empty"):
                    return "water"
            elif telem.water_pct <= WATER_RETURN_PCT:
                return "water"
        return None

    def needs_sanitization(self, zone: Zone) -> bool:
        if not self.spec.sterile_certified:
            return False
        was_sterile = self.last_classification == "Sterile"
        is_sterile = zone.classification == "Sterile"
        return was_sterile != is_sterile

    def needs_escort_wait(self, t: float, zone: Zone) -> float:
        if zone.zone_id in self.escort_override:
            return self.escort_override.pop(zone.zone_id)
        if t >= ESCORT_ZONE_HOUR_START:
            return round(self.rng.uniform(0, 10), 1)
        return 0.0

    # -- main FSM step, called once per simulated minute -------------------
    def step(self, t: float, on_zone_event: Callable):
        if self.disabled:
            return
        adapter = self.adapter
        adapter.tick(1.0)  # ground-truth physics always advances, offline or not

        if self.phase == "IDLE":
            if self.current is None:
                nxt = self._next_task(t)
                if nxt is None:
                    return
                self.current = nxt
                zone = self.zones[nxt.zone_id]
                if zone.zone_id not in self.zone_progress:
                    self.zone_progress[zone.zone_id] = float(zone.sqft)
                on_zone_event(nxt.zone_id, ZoneStatus.SCHEDULED, t, role=nxt.role, robot_id=self.spec.robot_id)
            zone = self.zones[self.current.zone_id]
            if t < self.current.planned_start:
                return
            if self.needs_sanitization(zone):
                adapter.set_internal_status(RobotStatus.SANITIZING)
                self.phase, self.phase_timer = "SANITIZE", SANITIZE_MINUTES
                self._log(t, f"sanitization cycle before entering {zone.zone_id}")
                return
            self._begin_travel(t, zone.zone_id)
            return

        if self.phase == "TRAVEL":
            self.phase_timer -= 1
            if self.phase_timer <= 0:
                if self.travel_destination == "DOCK":
                    self._arrive_at_dock(t, on_zone_event)
                else:
                    self._arrive(t, on_zone_event)
            return

        if self.phase == "SANITIZE":
            self.phase_timer -= 1
            if self.phase_timer <= 0:
                zone = self.zones[self.current.zone_id]
                self._begin_travel(t, zone.zone_id)
            return

        if self.phase == "ESCORT_WAIT":
            self.phase_timer -= 1
            if self.phase_timer <= 0:
                self._begin_clean(t)
            return

        if self.phase == "CLEAN":
            self._step_clean(t, on_zone_event)
            return

        if self.phase == "DOCK_SERVICE":
            self.phase_timer -= 1
            if self.phase_timer <= 0:
                adapter.set_internal_status(RobotStatus.IDLE)
                if self.resume_zone is not None:
                    # dock -> zone leg of a service round trip: transport
                    # mode, deck still raised until cleaning resumes -- fast.
                    self._begin_travel(t, self.resume_zone, fast=True)
                    self.resume_zone = None
                else:
                    self.current = None
                    self.phase = "IDLE"
            return

    # -- phase transitions --------------------------------------------------
    def _begin_travel(self, t: float, target_zone: str, fast: bool = False):
        self.adapter.phys.position = "IN_TRANSIT"
        self.adapter.set_internal_status(RobotStatus.EN_ROUTE)
        self.adapter.phys.battery_pct = max(0.0, self.adapter.phys.battery_pct - TRAVEL_BATTERY_PCT)
        # This leg moves the robot further from the dock (or is the first
        # leg out after one) -- accrue it into the return-trip cost estimate
        # used by wants_return_now(). See battery_used_since_dock docstring.
        self.battery_used_since_dock += TRAVEL_BATTERY_PCT
        self.phase, self.phase_timer = "TRAVEL", (TRAVEL_MINUTES_DOCK if fast else TRAVEL_MINUTES)
        self.travel_destination = "ZONE"
        self.stats["travel_events"] += 1

    def _arrive(self, t: float, on_zone_event: Callable):
        zone = self.zones[self.current.zone_id]
        if not zone.wifi and not self.offline_active:
            self._start_offline_mission(t, zone)
        wait = self.needs_escort_wait(t, zone)
        if wait > 0:
            self.adapter.set_internal_status(RobotStatus.ESCORT_WAIT)
            self.phase, self.phase_timer = "ESCORT_WAIT", wait
            self._log(t, f"security escort wait {wait:.1f} min at {zone.zone_id}")
            return
        self._begin_clean(t)

    def _begin_clean(self, t: float):
        zone = self.zones[self.current.zone_id]
        self.adapter.start_mission(zone.zone_id)
        if self.offline_active:
            self.adapter.phys.status = RobotStatus.OFFLINE_MISSION
        self.phase = "CLEAN"
        self.last_classification = zone.classification

    def _step_clean(self, t: float, on_zone_event: Callable):
        zone = self.zones[self.current.zone_id]
        telem = self.adapter.status_query()
        if self.offline_active:
            self.offline_buffer.append(telem)
        else:
            on_zone_event(zone.zone_id, ZoneStatus.IN_PROGRESS, t, role=self.current.role,
                          robot_id=self.spec.robot_id)

        # window closed before finishing
        if t >= zone.window_end:
            done_frac = 1.0 - (self.zone_progress.get(zone.zone_id, zone.sqft) / zone.sqft)
            status = ZoneStatus.PARTIAL if done_frac > 0.02 else ZoneStatus.MISSED
            self._log(t, f"{zone.zone_id} window closed at {done_frac*100:.0f}% coverage -> {status.value}")
            on_zone_event(zone.zone_id, status, t, role=self.current.role,
                          robot_id=self.spec.robot_id, coverage=done_frac)
            self._finish_task(t)
            return

        binding = self.wants_return_now(telem, zone)
        if binding:
            self._begin_dock_return(t, zone, binding)
            return

        # clean this minute's worth of square footage -- shared pool, see zone_progress note above
        remaining = self.zone_progress.get(zone.zone_id, zone.sqft) - self.spec.coverage_ft2_hr / 60.0
        self.zone_progress[zone.zone_id] = max(0.0, remaining)
        if remaining <= 0:
            self._log(t, f"{zone.zone_id} complete ({self.current.role})")
            on_zone_event(zone.zone_id, ZoneStatus.COMPLETE, t, role=self.current.role,
                          robot_id=self.spec.robot_id, coverage=1.0)
            if self.offline_active:
                self._end_offline_mission(t)
            self._finish_task(t)

    def _begin_dock_return(self, t: float, zone: Zone, binding: str):
        self._log(t, f"returning to dock from {zone.zone_id} -- binding constraint: {binding}")
        self.stats[f"{binding}_bound_stops"] += 1
        self.resume_zone = zone.zone_id
        if self.offline_active:
            self._end_offline_mission(t)  # back in WiFi range at the dock; re-enters offline mode on return trip
        self.adapter.set_internal_status(RobotStatus.EN_ROUTE)
        self.adapter.phys.position = "IN_TRANSIT"
        self.adapter.phys.battery_pct = max(0.0, self.adapter.phys.battery_pct - TRAVEL_BATTERY_PCT)
        # zone -> dock leg: transport mode, deck/squeegee raised for the run
        # back -- see TRAVEL_MINUTES_DOCK in hal/base.py.
        self.phase, self.phase_timer = "TRAVEL", TRAVEL_MINUTES_DOCK
        self.travel_destination = "DOCK"
        self.pending_binding_reason = binding

    def _arrive_at_dock(self, t: float, on_zone_event: Callable):
        self.adapter.phys.position = DOCK
        # Physically back at the dock -- the next leg out starts a fresh
        # "distance from home" count for the battery-return estimate.
        self.battery_used_since_dock = 0.0
        battery = self.adapter.phys.battery_pct
        water = self.adapter.phys.water_pct
        # Redeploy at CHARGE_DISPATCH_TARGET_PCT (90%), not 100%: charging
        # follows a CC/CV curve (see hal/base.py), so the last 10% -- the CV
        # taper -- costs roughly 2x the time the first 90% did. Waiting out
        # that taper is a bad trade against 78 percentage points of usable
        # battery (90% down to the BATTERY_RETURN_PCT=12% floor) being
        # plenty for another cleaning run, and freeing the robot up sooner
        # buys back schedule slack for exactly the kind of disruption this
        # system needs to absorb. See SPEC.md for the before/after numbers.
        need_charge = battery < CHARGE_DISPATCH_TARGET_PCT - 2.0
        need_water = water is not None and water < 90.0
        charge_min = charge_minutes_to_target(battery, CHARGE_DISPATCH_TARGET_PCT) if need_charge else 0.0
        water_min = WATER_CYCLE_MINUTES if need_water else 0.0
        duration = max(charge_min, water_min, 1.0)
        self.adapter.set_internal_status(RobotStatus.DOCK_SERVICE)
        self.phase, self.phase_timer = "DOCK_SERVICE", duration
        if need_water:
            self.stats["water_cycles"] += 1
        if need_charge:
            self.stats["charge_cycles"] += 1
        self._log(t, f"dock service started ({'charge+water' if need_charge and need_water else ('charge' if need_charge else 'water')}, {duration:.0f} min)")

    def _finish_task(self, t: float):
        self.adapter.set_internal_status(RobotStatus.IDLE)
        self.adapter.phys.position = DOCK
        self.current = None
        self.phase = "IDLE"

    # -- offline garage mission (Z8, no WiFi) -------------------------------
    def _start_offline_mission(self, t: float, zone: Zone):
        self.offline_active = True
        self.offline_buffer = []
        expected_min = (zone.sqft / self.spec.coverage_ft2_hr) * 60.0
        self.offline_return_t = t + expected_min
        msg = (f"OFFLINE MISSION handoff at {zone.zone_id}: preloaded waypoints + coverage "
               f"plan for {zone.sqft} ft^2, abort thresholds battery<=~{BATTERY_RETURN_PCT:.0f}%+trip-cost margin / "
               f"water<=low, expected return ~{fmt_time(int(self.offline_return_t))}")
        self._log(t, msg)
        if self.monitor:
            self.monitor.log_disruption(t, "OFFLINE MISSION", f"{self.spec.robot_id} entering {zone.zone_id} (no WiFi)", msg)

    def _end_offline_mission(self, t: float):
        msg = (f"{self.spec.robot_id} reconnects, reconciling {len(self.offline_buffer)} "
               f"buffered telemetry samples from offline mission")
        self._log(t, msg)
        if self.monitor:
            self.monitor.log_disruption(t, "OFFLINE RECONCILE", f"{self.spec.robot_id} reconnected", msg)
            # "Reconciling" has to actually mean something: feed the
            # buffered samples into Monitor now, in their original
            # chronological order, so this stretch of work isn't silently
            # lost from telemetry history -- anomaly detection and trip
            # profiling both read Monitor.history, and an offline-mission
            # robot (FloorBot on garage duty, most likely candidate for
            # exactly the leak-detection this system is trying to do) would
            # otherwise never accumulate any consumption profile at all,
            # since no ingest() happens while offline_active is True. Any
            # anomaly found here is correctly timestamped to when it
            # actually happened, even though it's only detected now --
            # that's the honest story for a robot with no live link.
            for buffered in self.offline_buffer:
                self.monitor.ingest(buffered, self.spec)
        self.offline_active = False


class FleetDispatcher:
    """Owns every RobotController and steps the whole fleet minute-by-minute.
    This is also the injection point for disruptions: scenario code (or a
    live event feed, in a real deployment) calls `inject_*` methods at a
    given sim-minute; the Replanner decides what to do and the Dispatcher
    carries it out (disabling a robot, forcing a WS drop, overriding an
    escort delay, re-routing zones)."""

    def __init__(self, fleet: Dict[str, RobotSpec], adapters: Dict[str, RobotAdapter],
                 schedule: Dict[str, List[Assignment]], zones: Dict[str, Zone],
                 monitor, replanner, rng: random.Random):
        self.fleet = fleet
        self.adapters = adapters
        self.zones = zones
        self.monitor = monitor
        self.replanner = replanner
        self.rng = rng
        self.zone_progress: Dict[str, float] = {}  # shared remaining-sqft pool, one entry per active zone
        self.controllers: Dict[str, RobotController] = {
            rid: RobotController(fleet[rid], adapters[rid], schedule.get(rid, []), zones, rng,
                                  monitor=monitor, zone_progress=self.zone_progress)
            for rid in fleet
        }
        self.t = 0
        self.disruption_log: List[str] = []

    def _on_zone_event(self, zone_id: str, status: ZoneStatus, t: float, role: str = "primary",
                        robot_id: str = "", coverage: float = None):
        self.monitor.on_zone_event(zone_id, status, t, role=role, robot_id=robot_id, coverage=coverage)

    def step(self):
        for ctrl in self.controllers.values():
            ctrl.step(self.t, self._on_zone_event)
            if ctrl.disabled or ctrl.offline_active:
                continue  # disabled: out of service. offline: no live telemetry, buffered instead.
            telem = ctrl.adapter.status_query()
            self.monitor.ingest(telem, ctrl.spec)
        self.t += 1

    def run_until(self, t_end: int):
        while self.t < t_end:
            self.step()

    def controller(self, robot_id: str) -> RobotController:
        return self.controllers[robot_id]
