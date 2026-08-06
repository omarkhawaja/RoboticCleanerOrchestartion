"""Scripted Tuesday-night scenario matching the assignment's sample
simulation timeline, including the ad-hoc Z1 request (7b) and the R-003 /
R-005 disruptions from section 3.

Disruptions are injected at scripted sim-minutes for a repeatable demo (see
SPEC.md "Scripted vs. emergent disruptions"). Two of the five required
disruptions -- R-001's water-empty stop and R-008's routine dock visits --
already emerge organically from the physics simulation with zero scripting;
the remaining three (equipment fault, sensor anomaly, connectivity drop)
plus the escort delay and ad-hoc request are the kind of external/uncertain
events a real system would receive from an event feed, so they're injected
explicitly here at the times given in the brief.
"""
from __future__ import annotations

import random

from . import facility, replanner
from .dispatcher import FleetDispatcher
from .hal.registry import build_adapter
from .models import fmt_time
from .monitor import Monitor
from .scheduler import generate_schedule

DAY = "Tue"


def build_shift(seed: int = 42):
    rng = random.Random(seed)
    zones = facility.build_zones()
    fleet = facility.build_fleet()
    schedule = generate_schedule(fleet, zones, DAY)

    monitor = Monitor(fleet)
    for zid, zone in zones.items():
        if not zone.scheduled_today(DAY):
            monitor.mark_not_scheduled(zid, f"{zid} not scheduled on {DAY} (cleaning days: {zone.days})")

    adapters = {rid: build_adapter(spec) for rid, spec in fleet.items()}
    dispatcher = FleetDispatcher(fleet, adapters, schedule, zones, monitor, replanner, rng)
    return dispatcher, monitor, schedule


def run_tuesday_night(verbose: bool = True):
    dispatcher, monitor, schedule = build_shift()
    narrative = []

    def note(msg: str):
        narrative.append(msg)
        if verbose:
            print(msg)

    note("=" * 72)
    note("7:00 PM -- Orchestrator generates tonight's schedule (Tuesday)")
    note("=" * 72)
    for rid in sorted(schedule):
        for a in schedule[rid]:
            note(f"  {rid:6} -> {a.zone_id} [{a.role:9}] planned_start={fmt_time(a.planned_start):8} "
                 f"est={a.est_clean_minutes:.0f} min  {a.note}")
    idle = [rid for rid, tasks in schedule.items() if not tasks]
    if idle:
        note(f"  idle tonight (no eligible zone): {', '.join(idle)}")

    # Pre-register the Z5 escort delay (security informs the orchestrator
    # ahead of the 1:00 AM window that the escort will be unavailable).
    replanner.handle_escort_delay(dispatcher, monitor, "R-003", "Z5", 25.0, t=360)

    # The brief's disruption timestamps assume every robot is still mid-clean
    # at 2:20 AM; in this dynamic schedule the CleanPath fleet finishes its
    # zones well before then. Rather than fake a robot into being busy at a
    # clock time it wouldn't realistically still be working, trigger the
    # WebSocket-drop disruption during whichever CleanPath robot's actual
    # cleaning window it falls in -- same mechanism the brief is testing,
    # honest sim time instead of a hardcoded timestamp that would land on an
    # idle robot. See SPEC.md "Scripted vs. emergent disruptions".
    cp_primary = None
    for rid in ("R-005", "R-007", "R-004"):
        primaries = [a for a in schedule.get(rid, []) if a.role == "primary"]
        if primaries:
            cp_primary = (rid, primaries[0])
            break
    ws_robot, ws_zone_t = (cp_primary[0], cp_primary[1].planned_start + 5) if cp_primary else ("R-005", 440)

    dispatcher.run_until(int(ws_zone_t))
    note(f"\n--- {fmt_time(int(ws_zone_t))}: {ws_robot} WebSocket drop in its current zone ---")
    grace = replanner.handle_ws_drop(dispatcher, monitor, ws_robot, t=ws_zone_t)
    dispatcher.run_until(int(ws_zone_t) + int(grace) + 1)
    replanner.check_ws_reconnect(dispatcher, monitor, ws_robot, t=ws_zone_t + grace)

    dispatcher.run_until(210)
    note("\n--- 10:30 PM: R-008 reports water 'low' only ~20 min after refilling ---")
    replanner.handle_water_anomaly(dispatcher, monitor, "R-008", t=210)

    dispatcher.run_until(330)
    note("\n--- 12:30 AM: AD-HOC REQUEST -- facility manager wants Z1 cleaned 2:00-3:00 AM ---")
    full, achievable = replanner.handle_adhoc_request(dispatcher, monitor, "Z1", t=330, window_end_t=390)
    note(f"    result: {'FULL' if full else 'PARTIAL'} coverage achievable (~{achievable:.0f} ft^2)")

    dispatcher.run_until(435)
    note("\n--- 2:15 AM: R-003 (AS-900H) sensor fault, only sterile-certified robot goes offline ---")
    replanner.handle_robot_failure(dispatcher, monitor, "R-003", t=435)

    dispatcher.run_until(720)
    note("\n" + "=" * 72)
    note("7:00 AM -- shift ends")
    report = monitor.shift_report(dispatcher.controllers, dispatcher.zones)
    note(report)

    return dispatcher, monitor, "\n".join(narrative)


if __name__ == "__main__":
    run_tuesday_night()
