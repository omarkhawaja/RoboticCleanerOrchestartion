"""Empirical study: across many shifts, is battery or water the more
frequent binding constraint -- or does it depend on the robot?

This does NOT run the scripted disruption timeline (scenario.py) -- that
would confound the measurement (e.g. disabling R-003 mid-shift truncates
its stats). Instead it runs the plain scheduler + organic dispatcher
physics, varying:
  - day of week (changes which zones are active: Z4 only Mon/Wed/Fri,
    Z8 only Tue/Sat -- this is the dominant source of real scenario
    variety, since the scheduler itself is deterministic)
  - random seed (jitters the post-11PM security escort wait, which
    changes exactly how much slack a robot has before a window closes
    and can tip a zone from "just finishes" to "needs one more cycle")

Usage:
    python -m analysis.binding_constraint_study
"""
from __future__ import annotations

import random
import statistics
from collections import defaultdict

from fleet_orchestrator import facility, replanner
from fleet_orchestrator.dispatcher import FleetDispatcher
from fleet_orchestrator.hal.registry import build_adapter
from fleet_orchestrator.monitor import Monitor
from fleet_orchestrator.scheduler import generate_schedule

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SEEDS_PER_DAY = 20


def run_one(day: str, seed: int):
    fleet = facility.build_fleet()
    zones = facility.build_zones()
    schedule = generate_schedule(fleet, zones, day)
    monitor = Monitor(fleet)
    adapters = {rid: build_adapter(spec) for rid, spec in fleet.items()}
    rng = random.Random(seed)
    dispatcher = FleetDispatcher(fleet, adapters, schedule, zones, monitor, replanner, rng)
    dispatcher.run_until(720)
    return dispatcher, monitor


def main():
    n_runs = 0
    totals = defaultdict(int)
    per_robot = defaultdict(lambda: defaultdict(int))
    per_oem = defaultdict(lambda: defaultdict(int))
    per_day = defaultdict(lambda: defaultdict(int))
    zone_status_counts = defaultdict(lambda: defaultdict(int))
    dock_visits_per_shift = []
    minutes_lost_per_shift = []

    for day in DAYS:
        for seed in range(SEEDS_PER_DAY):
            dispatcher, monitor = run_one(day, seed)
            n_runs += 1
            shift_dock_visits = 0
            shift_minutes_lost = 0.0
            for rid, ctrl in dispatcher.controllers.items():
                oem = ctrl.spec.oem.value
                s = ctrl.stats
                for k in ("water_bound_stops", "battery_bound_stops", "water_cycles", "charge_cycles"):
                    totals[k] += s[k]
                    per_robot[rid][k] += s[k]
                    per_oem[oem][k] += s[k]
                    per_day[day][k] += s[k]
                shift_dock_visits += s["water_bound_stops"] + s["battery_bound_stops"]
                # rough cost accounting: a water cycle is a flat 10 min stop;
                # a charge cycle's duration varies, so this undercounts charge
                # time on purpose -- it isolates "does the robot have to
                # detour for water" as its own line item, not lump it into
                # a number battery-driven charging would dominate anyway.
                shift_minutes_lost += s["water_cycles"] * 10
            dock_visits_per_shift.append(shift_dock_visits)
            minutes_lost_per_shift.append(shift_minutes_lost)
            for zid, rec in monitor.zones.items():
                zone_status_counts[zid][rec.status.value] += 1

    print(f"Simulated {n_runs} shifts ({len(DAYS)} days x {SEEDS_PER_DAY} seeds each)\n")

    print("=" * 78)
    print("FLEET-WIDE: WHAT ACTUALLY TRIGGERS A DOCK RETURN")
    print("=" * 78)
    w, b = totals["water_bound_stops"], totals["battery_bound_stops"]
    tot = w + b or 1
    print(f"  water-bound stops:   {w:5d}  ({100*w/tot:5.1f}%)")
    print(f"  battery-bound stops: {b:5d}  ({100*b/tot:5.1f}%)")
    print(f"  total water cycles run:  {totals['water_cycles']}")
    print(f"  total charge cycles run: {totals['charge_cycles']}")
    print(f"  avg dock returns / shift: {statistics.mean(dock_visits_per_shift):.2f} "
          f"(stdev {statistics.pstdev(dock_visits_per_shift):.2f})")

    print("\n" + "=" * 78)
    print("BY OEM (wet robots only -- CleanPath is dry-only, always 0 water stops)")
    print("=" * 78)
    print(f"  {'OEM':<12}{'water-bound':>14}{'battery-bound':>16}{'water cycles':>15}{'charge cycles':>15}")
    for oem, d in per_oem.items():
        print(f"  {oem:<12}{d['water_bound_stops']:>14}{d['battery_bound_stops']:>16}"
              f"{d['water_cycles']:>15}{d['charge_cycles']:>15}")

    print("\n" + "=" * 78)
    print("BY ROBOT (totals across all 140 simulated shifts)")
    print("=" * 78)
    print(f"  {'Robot':<8}{'water-bound':>14}{'battery-bound':>16}{'binding constraint':>22}")
    for rid in sorted(per_robot):
        d = per_robot[rid]
        w_, b_ = d["water_bound_stops"], d["battery_bound_stops"]
        if w_ == 0 and b_ == 0:
            verdict = "n/a (dry / never binds)"
        elif w_ > b_:
            verdict = f"WATER ({100*w_/(w_+b_):.0f}% of stops)"
        elif b_ > w_:
            verdict = f"BATTERY ({100*b_/(w_+b_):.0f}% of stops)"
        else:
            verdict = "tied"
        print(f"  {rid:<8}{w_:>14}{b_:>16}{verdict:>22}")

    print("\n" + "=" * 78)
    print("BY DAY OF WEEK (zone mix changes: Z4 carpet Mon/Wed/Fri, Z8 garage Tue/Sat)")
    print("=" * 78)
    print(f"  {'Day':<6}{'water-bound':>14}{'battery-bound':>16}")
    for day in DAYS:
        d = per_day[day]
        print(f"  {day:<6}{d['water_bound_stops']:>14}{d['battery_bound_stops']:>16}")

    print("\n" + "=" * 78)
    print("ZONE COMPLETION ACROSS ALL SHIFTS (coverage side of the tradeoff)")
    print("=" * 78)
    print(f"  {'Zone':<6}{'complete':>10}{'partial':>10}{'missed':>10}{'n/a today':>12}")
    for zid in sorted(zone_status_counts):
        d = zone_status_counts[zid]
        print(f"  {zid:<6}{d.get('COMPLETE',0):>10}{d.get('PARTIAL',0):>10}"
              f"{d.get('MISSED',0):>10}{d.get('NOT_SCHEDULED',0):>12}")


if __name__ == "__main__":
    main()
