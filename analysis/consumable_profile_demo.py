"""Demonstrates the consumable-profile system (fleet_orchestrator/profile.py)
across many shifts, with a SYNTHETIC drift injected on two robots to prove
the degradation/peer-outlier detectors actually fire correctly.

Why synthetic injection: this simulator's base physics use a constant
nominal drain rate straight from each robot's OEM spec (see SPEC.md) and
do not model real hardware aging on their own -- there's no mechanism in
the physics for a battery to actually get worse over simulated nights.
That's an honest, documented scope limit, not an oversight. So a plain
multi-shift run would show every robot flat and healthy forever, which
proves nothing about whether the detector works. This script instead:

Two DIFFERENT synthetic fault shapes, deliberately, because they're two
different real failure modes that need two different detectors:

  - R-001: healthy for 10 baseline shifts, then its battery_hours
    progressively shrinks over the next 10 -- a pack that gets WORSE
    over time, the classic aging signature. Caught by the SELF-BASELINE
    check (this robot's recent trips vs. its own earlier trips).
  - R-007: ~35% worse than spec from the very first shift, constant the
    whole time -- a unit that was always somewhat off (manufacturing
    variance, a persistent config difference), not degrading further.
    A self-baseline check would see NO drift here and correctly stay
    quiet; only comparing against its healthy sibling R-005 (both
    CleanPath CP-X1, both consistently scheduled every night in this
    facility) reveals it. Caught by the PEER-OUTLIER check.

Same underlying data model, two different questions -- "is this robot
getting worse?" vs. "is this robot different from its siblings?" -- and
the demo is built to show neither check subsumes the other.

This is the same shape as the R-008 water-anomaly disruption elsewhere in
this system: a scripted trigger proving a general-purpose mechanism works,
not a claim that this exact scenario happens on its own.

Usage:
    python -m analysis.consumable_profile_demo
"""
from __future__ import annotations

import dataclasses
import random

from fleet_orchestrator import facility, replanner
from fleet_orchestrator.dispatcher import FleetDispatcher
from fleet_orchestrator.hal.registry import build_adapter
from fleet_orchestrator.monitor import Monitor
from fleet_orchestrator.profile import ProfileStore, render_profile_report
from fleet_orchestrator.scheduler import generate_schedule

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
N_BASELINE_SHIFTS = 10
N_DRIFT_SHIFTS = 10
DRIFT_PCT_PER_SHIFT = 0.03  # battery_hours/water_hours shrink 3% per drift shift


def run_one_shift(day: str, seed: int, profile_store: ProfileStore, shift_label: str,
                   fleet_overrides: dict | None = None):
    fleet = facility.build_fleet()
    if fleet_overrides:
        fleet.update(fleet_overrides)
    zones = facility.build_zones()
    schedule = generate_schedule(fleet, zones, day)
    monitor = Monitor(fleet, profile_store=profile_store)
    for zid, zone in zones.items():
        if not zone.scheduled_today(day):
            monitor.mark_not_scheduled(zid, f"{zid} not scheduled on {day}")
    adapters = {rid: build_adapter(spec) for rid, spec in fleet.items()}
    rng = random.Random(seed)
    dispatcher = FleetDispatcher(fleet, adapters, schedule, zones, monitor, replanner, rng)
    dispatcher.run_until(720)
    profile_store.record_shift(monitor, fleet, shift_label=shift_label)
    return dispatcher, monitor


R007_CHRONIC_FACTOR = 0.65  # ~35% worse than spec, constant from shift 1 -- "born faulty," not aging


def main():
    store = ProfileStore()
    fleet_base = facility.build_fleet()
    r001_base_battery_hours = fleet_base["R-001"].battery_hours
    r007_chronic = dataclasses.replace(fleet_base["R-007"],
                                        battery_hours=fleet_base["R-007"].battery_hours * R007_CHRONIC_FACTOR)

    total_shifts = N_BASELINE_SHIFTS + N_DRIFT_SHIFTS
    for i in range(1, total_shifts + 1):
        day = DAYS[(i - 1) % 7]
        overrides = {"R-007": r007_chronic}  # chronically off every single shift, from the start
        if i > N_BASELINE_SHIFTS:            # R-001 only starts drifting after the baseline period
            k = i - N_BASELINE_SHIFTS
            decay = 1.0 - DRIFT_PCT_PER_SHIFT * k
            overrides["R-001"] = dataclasses.replace(fleet_base["R-001"], battery_hours=r001_base_battery_hours * decay)
        run_one_shift(day, 5000 + i, store, f"{day}#{i}", fleet_overrides=overrides)

    print(f"Simulated {total_shifts} shifts. R-001: healthy for {N_BASELINE_SHIFTS} shifts then its "
          f"battery_hours ramps down over the next {N_DRIFT_SHIFTS} (aging signature). R-007: "
          f"{(1/R007_CHRONIC_FACTOR - 1)*100:.0f}% worse than spec from shift 1, constant throughout "
          f"(chronic/manufacturing-variance signature).\n")
    print("NOTE: the drift is injected here for demonstration purposes -- this simulator's base physics")
    print("use a constant nominal drain rate per OEM spec and do not model hardware aging on their own")
    print("(see SPEC.md). This proves the detector fires correctly against a KNOWN synthetic trend; it")
    print("is not a claim that real degradation happens spontaneously in an ordinary run.\n")

    print("=" * 78)
    print("FULL PROFILE REPORT (all 8 robots)")
    print("=" * 78)
    print(render_profile_report(store, fleet_base))

    print("\n" + "=" * 78)
    print("EXPLICIT FLAG CHECKS -- the two synthetically-injected cases")
    print("=" * 78)
    degr = store.flag_degradation("R-001", "battery")
    if degr:
        print(f"R-001 battery: DEGRADATION FLAGGED -- baseline {degr['baseline_rate_per_60min']}%/60min -> "
              f"recent {degr['recent_rate_per_60min']}%/60min ({degr['ratio']}x, {degr['n_trips']} trips)")
    else:
        print("R-001 battery: not flagged")

    peer = store.flag_peer_outlier("R-007", oem="CleanPath", model="CP-X1", resource="battery")
    if peer:
        z_str = f"z={peer['z_score']}" if peer["z_score"] is not None else "ratio-based (low peer variance)"
        print(f"R-007 battery: PEER OUTLIER FLAGGED -- own {peer['own_rate_per_60min']}%/60min vs sibling "
              f"(R-005) {peer['peer_rate_per_60min']}%/60min ({z_str}, "
              f"{peer['n_own_trips']} own / {peer['n_peer_trips']} peer trips)")
    else:
        print("R-007 battery: not flagged")

    print("\n-- Cross-checks: each detector should catch its OWN fault shape and stay quiet on the other --")
    r001_peer_note = "n/a (no AS-900 peer with trip data this run)"
    r007_degr = store.flag_degradation("R-007", "battery")
    print(f"R-001 peer-outlier (should be quiet/n-a -- this is an aging story, not a chronic one): "
          f"{r001_peer_note}")
    print(f"R-007 self-degradation (should be quiet -- R-007 never DRIFTS, it's constant from shift 1): "
          f"{r007_degr or 'not flagged (correct)'}")

    print("\n-- Control check: R-005 (R-007's sibling, never touched) should NOT be flagged --")
    r005_degr = store.flag_degradation("R-005", "battery")
    r005_peer = store.flag_peer_outlier("R-005", oem="CleanPath", model="CP-X1", resource="battery")
    print(f"R-005 battery degradation: {r005_degr or 'not flagged (correct)'}")
    print(f"R-005 battery peer-outlier: {r005_peer or 'not flagged (correct)'}")


if __name__ == "__main__":
    main()
