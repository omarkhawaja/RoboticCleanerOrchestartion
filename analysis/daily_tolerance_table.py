"""Day-by-day tolerance table: for each simulated day, how long did it
actually take to clean the whole facility vs. how much time was allocated
(the 19:00-07:00 / 720-minute shift window), and what's the delta -- the
schedule margin available to absorb an unexpected delay or disruption
that day.

Simulates N consecutive calendar days (default 28 = 4 full weeks, so
every weekday's zone mix -- including the Mon/Wed/Fri carpet zone and the
Tue/Sat garage zone -- gets repeated coverage), cycling real days of the
week with a distinct random seed per day (so escort-wait jitter varies
day to day, same as it would in reality). No injected disruptions -- this
measures baseline operational tolerance, the number you'd actually plan
an SLA or a maintenance window around, not a disruption-day outlier.

Usage:
    python -m analysis.daily_tolerance_table [n_days]
"""
from __future__ import annotations

import random
import statistics
import sys

from fleet_orchestrator import facility, replanner
from fleet_orchestrator.dispatcher import FleetDispatcher
from fleet_orchestrator.hal.registry import build_adapter
from fleet_orchestrator.models import fmt_time
from fleet_orchestrator.monitor import Monitor
from fleet_orchestrator.scheduler import generate_schedule

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SHIFT_CAPACITY_MIN = 720  # 19:00 -> 07:00, 12h, the allocated cleaning window


def run_day(day: str, seed: int):
    fleet = facility.build_fleet()
    zones = facility.build_zones()
    schedule = generate_schedule(fleet, zones, day)
    monitor = Monitor(fleet)
    for zid, zone in zones.items():
        if not zone.scheduled_today(day):
            monitor.mark_not_scheduled(zid, f"{zid} not scheduled on {day}")
    adapters = {rid: build_adapter(spec) for rid, spec in fleet.items()}
    rng = random.Random(seed)
    dispatcher = FleetDispatcher(fleet, adapters, schedule, zones, monitor, replanner, rng)
    dispatcher.run_until(SHIFT_CAPACITY_MIN)
    return dispatcher, monitor, zones


def main():
    n_days = int(sys.argv[1]) if len(sys.argv) > 1 else 28
    rows = []

    for day_idx in range(1, n_days + 1):
        weekday = WEEKDAYS[(day_idx - 1) % 7]
        seed = 1000 + day_idx  # distinct per calendar day
        dispatcher, monitor, zones = run_day(weekday, seed)

        scheduled_today = [z for z in zones.values() if z.scheduled_today(weekday)]
        finish_times = [rec.last_cleaned_t for rec in monitor.zones.values() if rec.last_cleaned_t is not None]
        finish_min = max(finish_times) if finish_times else 0
        tolerance_min = SHIFT_CAPACITY_MIN - finish_min

        complete = sum(1 for z in scheduled_today
                       if monitor.zones.get(z.zone_id) and monitor.zones[z.zone_id].status.value == "COMPLETE")
        dock_stops = sum(c.stats["water_bound_stops"] + c.stats["battery_bound_stops"]
                          for c in dispatcher.controllers.values())

        rows.append({
            "day_idx": day_idx, "weekday": weekday, "zones_today": len(scheduled_today),
            "zones_complete": complete, "finish_min": finish_min, "tolerance_min": tolerance_min,
            "dock_stops": dock_stops,
        })

    # -- table --
    print(f"Simulated {n_days} consecutive calendar days (no injected disruptions -- baseline operational tolerance)\n")
    header = (f"{'Day':>3} {'Wkday':<5} {'Zones':>6} {'Finished cleaning':>20} {'Capacity':>10} "
              f"{'Tolerance/Delta':>18} {'Dock':>5} {'All':>5}")
    print(header)
    print("-" * len(header))
    for r in rows:
        finish_clock = fmt_time(r["finish_min"])
        tol_h = r["tolerance_min"] / 60.0
        all_done = "YES" if r["zones_complete"] == r["zones_today"] else "NO"
        print(f"{r['day_idx']:>3} {r['weekday']:<5} {r['zones_complete']:>3}/{r['zones_today']:<2} "
              f"{finish_clock:>14} ({r['finish_min']:>3}m) {SHIFT_CAPACITY_MIN:>6}m "
              f"{r['tolerance_min']:>7}m ({tol_h:>4.1f}h) {r['dock_stops']:>5} {all_done:>5}")

    # -- summary --
    tolerances = [r["tolerance_min"] for r in rows]
    finishes = [r["finish_min"] for r in rows]
    print("\n" + "=" * len(header))
    print("SUMMARY")
    print("=" * len(header))
    print(f"  days simulated:            {n_days}")
    print(f"  days with full coverage:   {sum(1 for r in rows if r['zones_complete'] == r['zones_today'])}/{n_days}")
    print(f"  mean time to clean facility: {statistics.mean(finishes):.1f} min ({statistics.mean(finishes)/60:.2f} h)")
    print(f"  mean allocated capacity:     {SHIFT_CAPACITY_MIN} min (12.00 h) -- fixed, the 19:00-07:00 window")
    print(f"  mean tolerance/delta:        {statistics.mean(tolerances):.1f} min ({statistics.mean(tolerances)/60:.2f} h)")
    print(f"  median tolerance/delta:      {statistics.median(tolerances):.1f} min")
    print(f"  min tolerance observed:      {min(tolerances):.1f} min ({min(tolerances)/60:.2f} h)  <- tightest day, plan around this")
    print(f"  max tolerance observed:      {max(tolerances):.1f} min ({max(tolerances)/60:.2f} h)")
    print(f"  stdev:                       {statistics.pstdev(tolerances):.1f} min")


if __name__ == "__main__":
    main()
