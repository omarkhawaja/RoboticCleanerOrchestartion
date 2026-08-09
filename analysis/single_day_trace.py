"""One full day (Tuesday), no injected disruptions -- the "pure" baseline:
how many dock trips does each robot make, does every zone finish inside
its window, and how long does the whole facility take start-to-finish.

Usage:
    python -m analysis.single_day_trace
"""
from __future__ import annotations

import random

from fleet_orchestrator import facility, replanner
from fleet_orchestrator.dispatcher import FleetDispatcher
from fleet_orchestrator.hal.registry import build_adapter
from fleet_orchestrator.models import fmt_time
from fleet_orchestrator.monitor import Monitor
from fleet_orchestrator.scheduler import generate_schedule

DAY = "Tue"
SEED = 42


def main():
    fleet = facility.build_fleet()
    zones = facility.build_zones()
    schedule = generate_schedule(fleet, zones, DAY)
    monitor = Monitor(fleet)
    for zid, zone in zones.items():
        if not zone.scheduled_today(DAY):
            monitor.mark_not_scheduled(zid, f"{zid} not scheduled on {DAY}")
    adapters = {rid: build_adapter(spec) for rid, spec in fleet.items()}
    rng = random.Random(SEED)
    dispatcher = FleetDispatcher(fleet, adapters, schedule, zones, monitor, replanner, rng)

    print("=" * 78)
    print("COVERAGE RATES -- where the ft^2/min numbers come from")
    print("=" * 78)
    print("  These are NOT measured or inferred by the simulator. They are the")
    print("  OEM spec sheet numbers straight from the assignment's Fleet Roster")
    print("  table (facility.py), converted from ft^2/hr to ft^2/min by /60 at")
    print("  the point of use (dispatcher.py: `self.spec.coverage_ft2_hr/60`).")
    print("  It's a constant linear rate -- no slowdown for turns, obstacles,")
    print("  or overlap loss. See SPEC.md #12 for that simplification.\n")
    print(f"  {'Robot':<8}{'Model':<10}{'ft^2/hr':>10}{'ft^2/min':>11}{'sec/ft^2':>11}")
    for rid, spec in fleet.items():
        print(f"  {rid:<8}{spec.model:<10}{spec.coverage_ft2_hr:>10.0f}"
              f"{spec.coverage_ft2_hr/60:>11.1f}{3600/spec.coverage_ft2_hr:>11.3f}")

    dispatcher.run_until(720)

    print("\n" + "=" * 78)
    print(f"FACILITY SUMMARY -- {DAY}, seed={SEED}")
    print("=" * 78)
    complete = [z for z in zones.values() if z.scheduled_today(DAY)]
    finish_times = []
    for zid, zone in zones.items():
        if not zone.scheduled_today(DAY):
            continue
        rec = monitor.zones.get(zid)
        status = rec.status.value if rec else "NEVER STARTED"
        finish = fmt_time(rec.last_cleaned_t) if rec and rec.last_cleaned_t is not None else "--"
        cov = f"{rec.coverage*100:.0f}%" if rec else "0%"
        print(f"  {zid} {zone.name:<20} status={status:<10} coverage={cov:<6} finished={finish}")
        if rec and rec.last_cleaned_t is not None:
            finish_times.append(rec.last_cleaned_t)

    all_done = all(monitor.zones.get(z.zone_id) and monitor.zones[z.zone_id].status.value == "COMPLETE"
                   for z in complete)
    print(f"\n  All {len(complete)} scheduled zones fully covered? {'YES' if all_done else 'NO'}")
    if finish_times:
        print(f"  Last zone finished at: {fmt_time(max(finish_times))} "
              f"({max(finish_times)} min into the shift, shift budget is 720 min / 12h)")
        print(f"  Facility fully covered with {720-max(finish_times):.0f} min of the 12h window to spare")

    print("\n" + "=" * 78)
    print("TRIP COUNT PER ROBOT (a 'trip' = one dock visit, water and/or charge)")
    print("=" * 78)
    total_trips = 0
    print(f"  {'Robot':<8}{'OEM':<12}{'water trips':>13}{'charge trips':>14}{'total dock visits':>19}")
    for rid, ctrl in dispatcher.controllers.items():
        s = ctrl.stats
        visits = max(s['water_cycles'], s['charge_cycles'])  # a combined stop still counts once
        # a dock visit happens once per forced return; water/charge cycles can
        # co-occur in the same visit (see dispatcher._arrive_at_dock), so the
        # true visit count is bound by whichever cycle-type fired more often,
        # not the sum of the two.
        total_trips += visits
        print(f"  {rid:<8}{ctrl.spec.oem.value:<12}{s['water_cycles']:>13}{s['charge_cycles']:>14}{visits:>19}")
    print(f"\n  Total dock trips across the whole fleet, whole shift: {total_trips}")

    print("\n" + "=" * 78)
    print("FULL EVENT TRACE (every robot, one line per state transition, true chronological order)")
    print("=" * 78)
    all_events = []
    for rid, ctrl in dispatcher.controllers.items():
        all_events.extend(ctrl.events)
    all_events.sort(key=_shift_minutes_of_line)
    for e in all_events:
        print(" ", e)


def _shift_minutes_of_line(line: str) -> int:
    """Recover sortable shift-minutes from a '[HH:MM]' or '[HH:MM+1d]' prefix
    -- inverse of models.fmt_time. Needed because plain string sort puts
    '00:27+1d' (well after midnight) before '19:05' (early evening)."""
    prefix = line[1:line.index("]")]
    next_day = prefix.endswith("+1d")
    hh, mm = prefix.replace("+1d", "").split(":")
    clock_min = int(hh) * 60 + int(mm)
    return clock_min + 300 if next_day else clock_min - 1140


if __name__ == "__main__":
    main()
