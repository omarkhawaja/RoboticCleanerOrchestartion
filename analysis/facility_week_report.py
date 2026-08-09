"""Runs the full facility (Mon-Sun) and produces two tables:

  1. THE FACILITY TABLE -- same zone/sqft/floor/classification/window
     columns as the assignment's own facility spec, plus three new
     columns: actual time taken to clean that zone, the allocated
     capacity implied by its cleaning window, and the delta between
     them (positive = finished with time to spare; the schedule's
     built-in tolerance for an unexpected delay on that specific zone).

  2. THE WEEKLY SUMMARY TABLE -- one row per day of the week actually
     simulated: facility-wide sqft-weighted cleaned %, time taken to
     clean everything scheduled that night, allocated shift capacity
     (720 min / 12h, fixed), and the resulting delta/tolerance.

No injected disruptions -- this is the baseline "how much slack does the
schedule actually have" view, same scoping as analysis/daily_tolerance_table.py.
Each weekday is run once (not swept across seeds) since the point here is
a concrete, readable table, not a statistical distribution.

Usage:
    python -m analysis.facility_week_report
"""
from __future__ import annotations

import random

from fleet_orchestrator import facility, replanner
from fleet_orchestrator.dispatcher import FleetDispatcher
from fleet_orchestrator.hal.registry import build_adapter
from fleet_orchestrator.models import fmt_time
from fleet_orchestrator.monitor import Monitor
from fleet_orchestrator.scheduler import generate_schedule

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SHIFT_CAPACITY_MIN = 720

# Cleaning-window text exactly as given in the facility spec (display only --
# the actual scheduling math uses facility.build_zones()'s window_start/end).
WINDOW_TEXT = {
    "Z1": "9:00 PM - 6:00 AM daily",
    "Z2": "3:00 AM - 5:00 AM daily",
    "Z3": "10:00 PM - 5:00 AM daily",
    "Z4": "7:00 PM - 11:00 PM, Mon/Wed/Fri",
    "Z5": "1:00 AM - 5:00 AM daily",
    "Z6": "8:00 PM - 6:00 AM daily",
    "Z7": "11:00 PM - 4:00 AM daily",
    "Z8": "Anytime, 2x/week (Tue/Sat)",
}

def _shift_minutes_of_line(line: str) -> int:
    """Recover sortable/usable shift-minutes from a '[HH:MM]' or
    '[HH:MM+1d]' history-log prefix -- inverse of models.fmt_time."""
    prefix = line[1:line.index("]")]
    next_day = prefix.endswith("+1d")
    hh, mm = prefix.replace("+1d", "").split(":")
    clock_min = int(hh) * 60 + int(mm)
    return clock_min + 300 if next_day else clock_min - 1140


def run_day(day: str, seed: int = 42):
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
    zones_static = facility.build_zones()

    print("=" * 100)
    print("RUNNING THE FULL WEEK (Mon-Sun), baseline -- no injected disruptions")
    print("=" * 100)
    day_results = {}
    for day in WEEKDAYS:
        dispatcher, monitor, zones = run_day(day)
        day_results[day] = (dispatcher, monitor, zones)
        print(f"  {day}: schedule generated, shift simulated (19:00-07:00)")

    # -- Table 1: the facility table, exact columns from the spec + 3 new ones --
    print("\n" + "=" * 100)
    print("FACILITY -- Regional General Hospital (with simulated time-taken / allocated / delta)")
    print("=" * 100)
    header = (f"{'Zone':<5}{'Sq Ft':>8}  {'Floor':<9}{'Class':<13}{'Cleaning Window':<28}"
              f"{'Time Taken':>12}{'Allocated':>11}{'Delta':>10}")
    print(header)
    print("-" * len(header))

    zone_order = ["Z1", "Z2", "Z3", "Z4", "Z5", "Z6", "Z7", "Z8"]
    for zid in zone_order:
        z = zones_static[zid]
        allocated_h = (z.window_end - z.window_start) / 60.0

        # representative day: the first day of the week this zone is
        # actually scheduled on (daily zones just use Monday for consistency)
        rep_day = next((d for d in WEEKDAYS if z.scheduled_today(d)), None)
        time_taken_str = "n/a"
        delta_str = "n/a"
        if rep_day:
            _, monitor, _ = day_results[rep_day]
            rec = monitor.zones.get(zid)
            if rec and rec.history and rec.last_cleaned_t is not None:
                start_t = _shift_minutes_of_line(rec.history[0])
                finish_t = rec.last_cleaned_t
                taken_h = (finish_t - start_t) / 60.0
                delta_h = allocated_h - taken_h
                time_taken_str = f"{taken_h:.2f}h ({rep_day})"
                delta_str = f"+{delta_h:.2f}h" if delta_h >= 0 else f"{delta_h:.2f}h"

        print(f"{zid:<5}{z.sqft:>8,}  {z.floor_type.value:<9}"
              f"{z.classification:<13}{WINDOW_TEXT[zid]:<28}{time_taken_str:>12}{allocated_h:>9.2f}h{delta_str:>10}")

    print("\n  'Time Taken' = actual wall-clock span from first robot activity on that zone to its")
    print("  COMPLETE event, on the first day of the week it's scheduled. 'Allocated' = the cleaning")
    print("  window's own duration. 'Delta' = allocated - taken; positive means the schedule finished")
    print("  that zone with time to spare inside its own window (tolerance for a delay on THAT zone).")

    # -- Table 2: weekly per-day summary --
    print("\n" + "=" * 100)
    print("WEEKLY SUMMARY -- facility-wide, per day")
    print("=" * 100)
    print("  'Allocated (shift)' is the fixed 19:00-07:00 operating window from the assignment's own")
    print("  'simulate one night shift' spec -- a given constant, correctly the same every night, NOT")
    print("  derived from zone data. 'Zone-Window Demand' IS derived from zone data and DOES vary by")
    print("  day -- the sum of every scheduled zone's own window duration that night (Z8's 12h 'anytime'")
    print("  window alone is why Tue/Sat run higher than the rest).\n")
    header2 = (f"{'Day':<6}{'Zones today':>13}{'Cleaned %':>12}{'Time Taken':>14}"
               f"{'Allocated (shift)':>19}{'Zone-Window Demand':>21}{'Delta':>10}")
    print(header2)
    print("-" * len(header2))
    for day in WEEKDAYS:
        _, monitor, zones = day_results[day]
        scheduled_today = [z for z in zones.values() if z.scheduled_today(day)]
        total_sqft = sum(z.sqft for z in scheduled_today)
        cleaned_sqft = sum(z.sqft * (monitor.zones[z.zone_id].coverage if z.zone_id in monitor.zones else 0.0)
                           for z in scheduled_today)
        cleaned_pct = 100.0 * cleaned_sqft / total_sqft if total_sqft else 0.0
        finish_times = [monitor.zones[z.zone_id].last_cleaned_t for z in scheduled_today
                        if z.zone_id in monitor.zones and monitor.zones[z.zone_id].last_cleaned_t is not None]
        finish_min = max(finish_times) if finish_times else 0
        allocated_h = SHIFT_CAPACITY_MIN / 60.0
        window_demand_h = sum((z.window_end - z.window_start) / 60.0 for z in scheduled_today)
        taken_h = finish_min / 60.0
        delta_h = allocated_h - taken_h
        taken_str = f"{taken_h:.2f}h ({fmt_time(finish_min)})"
        delta_str = f"+{delta_h:.2f}h" if delta_h >= 0 else f"{delta_h:.2f}h"
        print(f"{day:<6}{len(scheduled_today):>13}{cleaned_pct:>11.1f}%{taken_str:>19}"
              f"{allocated_h:>16.2f}h{window_demand_h:>18.2f}h{delta_str:>10}")

    print("\n  'Cleaned %' is sqft-weighted across every zone scheduled that day (Z4 excluded Tue/Thu/")
    print("  Sat/Sun, Z8 excluded Mon/Wed/Thu/Fri/Sun -- per the facility's own cleaning-day pattern).")


if __name__ == "__main__":
    main()
