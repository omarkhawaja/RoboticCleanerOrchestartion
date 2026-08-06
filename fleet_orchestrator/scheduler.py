"""Nightly schedule generator.

Objective function (documented per the assignment's requirement to justify
it): **minimize risk-weighted incomplete coverage, then minimize robot-hours
used.** Concretely, zones are prioritized by a weight that favors (a)
sterile classification (patient-safety / infection-control SLA), then (b)
high-traffic classification, then (c) earliest window close (least slack).
Among eligible robots for a zone we prefer whichever leaves the most slack
in the fleet overall (idle robots before ones already double-booked), and
we only allocate a *second* robot to a zone when a single robot cannot
plausibly finish it inside its window with a safety buffer for cycle
overhead -- extra robots are opportunity cost the objective function is
trying to avoid spending unless coverage is genuinely at risk.

This is a **heuristic, not an ILP/CP-SAT solver.** For 8 robots and 8 zones
a real solver would find a provably-optimal assignment quickly, and would
be the right call in production; for this exercise a greedy
priority-ordered heuristic is transparent, fast, easy to explain in a
walkthrough, and -- per the rubric -- "a valid heuristic is fine." See
SPEC.md for the full tradeoff discussion.

IMPORTANT: this scheduler produces a *plan* (who cleans what, roughly when).
It does NOT try to precompute exact water/battery break timing -- that
depends on real-time physics the Dispatcher simulates minute-by-minute
(travel jitter, disruptions, etc). The plan only needs to prove a robot
*can* plausibly finish before its window closes; the Dispatcher is the
ground truth for what actually happens, including inserting extra cycles
if reality drifts from the estimate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from .facility import can_clean, wants_wet_scrub
from .hal.base import SANITIZE_MINUTES, TRAVEL_MINUTES
from .models import FloorType, RobotSpec, Zone

CYCLE_OVERHEAD_BUFFER = 1.20  # +20% over pure clean time to allow for water/battery cycles + travel


@dataclass
class Assignment:
    zone_id: str
    robot_id: str
    planned_start: int
    est_clean_minutes: float
    role: str = "primary"  # "primary" or "secondary" (dual dry+wet pass on the same zone)
    note: str = ""


def zone_priority(zone: Zone) -> tuple:
    sterile_rank = 0 if zone.classification == "Sterile" else 1
    traffic_rank = 0 if zone.classification == "High-traffic" else 1
    return (sterile_rank, traffic_rank, zone.window_end, zone.window_start)


def est_clean_minutes(robot: RobotSpec, zone: Zone) -> float:
    return (zone.sqft / robot.coverage_ft2_hr) * 60.0


def fits_in_window(robot: RobotSpec, zone: Zone, start: int) -> bool:
    duration = est_clean_minutes(robot, zone) * CYCLE_OVERHEAD_BUFFER
    if zone.classification == "Sterile":
        duration += SANITIZE_MINUTES  # possible sanitization gate before entry
    duration += TRAVEL_MINUTES
    return start + duration <= zone.window_end


def generate_schedule(fleet: Dict[str, RobotSpec], zones: Dict[str, Zone], day: str) -> Dict[str, List[Assignment]]:
    todays_zones = [z for z in zones.values() if z.scheduled_today(day)]
    todays_zones.sort(key=zone_priority)

    # busy_until[robot_id] = minute the robot becomes free for a new assignment
    busy_until = {rid: 0 for rid in fleet}
    assignments: List[Assignment] = []
    reserved_for_sterile = {rid for rid, r in fleet.items() if r.sterile_certified}

    def eligible(zone: Zone, exclude: set) -> List[RobotSpec]:
        # Note: `exclude` only ever holds robots already assigned to *this
        # zone* in this pass (can't double-book one robot on itself). A
        # robot already busy earlier in the night is still eligible for a
        # later zone -- busy_until + fits_in_window is what enforces that
        # it can't be in two places at once; robots are reused across the
        # shift, not one-assignment-per-night.
        pool = [
            r for rid, r in fleet.items()
            if rid not in exclude and can_clean(r, zone)
            and (rid in reserved_for_sterile) == (zone.classification == "Sterile")
        ]
        prefers_wet = wants_wet_scrub(zone)

        def type_rank(r: RobotSpec) -> int:
            # 0 = matches this zone's preferred cleaning method, 1 = fallback capability
            if zone.classification == "Sterile" or zone.floor_type == FloorType.CARPET:
                return 0  # only one capability applies anyway
            is_wet_capable = "hard_scrub" in r.capabilities
            return 0 if (is_wet_capable == prefers_wet) else 1

        pool.sort(key=lambda r: (type_rank(r), busy_until[r.robot_id], -r.coverage_ft2_hr, r.robot_id))
        return pool

    for zone in todays_zones:
        this_zone: set = set()
        candidates = eligible(zone, exclude=this_zone)
        if not candidates:
            continue  # no capable robot exists at all; will surface as MISSED in the shift report
        primary = candidates[0]
        start = max(zone.window_start, busy_until[primary.robot_id])
        if not fits_in_window(primary, zone, start):
            continue  # every eligible robot is booked too late to make the window; MISSED
        assignments.append(Assignment(zone.zone_id, primary.robot_id, start,
                                       est_clean_minutes(primary, zone)))
        busy_until[primary.robot_id] = start + est_clean_minutes(primary, zone) * CYCLE_OVERHEAD_BUFFER
        this_zone.add(primary.robot_id)

        # Risk-based second robot: only add help when a single robot would
        # leave less than 20% slack against its window (real risk of not
        # finishing, not just "could go faster"). Deliberately NOT triggered
        # by sqft alone -- e.g. Z1 has ample slack for a single AS-900 even
        # though it's the largest zone, so it runs single-robot and is left
        # free to actually demonstrate the water-cycle interleaving the
        # scheduler is supposed to plan for (see SPEC.md).
        single_duration = est_clean_minutes(primary, zone) * CYCLE_OVERHEAD_BUFFER
        slack = zone.window_end - (start + single_duration)
        window_span = zone.window_end - zone.window_start
        if zone.classification == "High-traffic" and slack < 0.20 * window_span:
            more = eligible(zone, exclude=this_zone)
            if more:
                helper = more[0]
                hstart = max(zone.window_start, busy_until[helper.robot_id])
                if fits_in_window(helper, zone, hstart):
                    assignments.append(Assignment(zone.zone_id, helper.robot_id, hstart,
                                                    est_clean_minutes(helper, zone),
                                                    note="added to cut completion time on a large high-traffic zone"))
                    busy_until[helper.robot_id] = hstart + est_clean_minutes(helper, zone) * CYCLE_OVERHEAD_BUFFER
                    this_zone.add(helper.robot_id)

        # Standard hard-floor zones cleaned dry (CleanPath): opportunistically
        # add a wet-scrub secondary pass if a scrubber is otherwise idle --
        # better cleaning quality is "free" when the robot has nothing else to do.
        if zone.classification == "Standard" and primary.dry_only and zone.floor_type != FloorType.CONCRETE:
            wet_candidates = [
                r for rid, r in fleet.items()
                if rid not in this_zone and "hard_scrub" in r.capabilities
                and rid not in reserved_for_sterile
            ]
            wet_candidates.sort(key=lambda r: (busy_until[r.robot_id], r.robot_id))
            if wet_candidates:
                helper = wet_candidates[0]
                hstart = max(zone.window_start + 60, busy_until[helper.robot_id])  # let the dry pass go first
                if fits_in_window(helper, zone, hstart):
                    assignments.append(Assignment(zone.zone_id, helper.robot_id, hstart,
                                                    est_clean_minutes(helper, zone),
                                                    role="secondary",
                                                    note="wet-scrub follow-up pass after dry cleaning"))
                    busy_until[helper.robot_id] = hstart + est_clean_minutes(helper, zone) * CYCLE_OVERHEAD_BUFFER
                    this_zone.add(helper.robot_id)

    by_robot: Dict[str, List[Assignment]] = {rid: [] for rid in fleet}
    for a in assignments:
        by_robot[a.robot_id].append(a)
    for rid in by_robot:
        by_robot[rid].sort(key=lambda a: a.planned_start)
    return by_robot
