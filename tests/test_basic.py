"""Sanity tests for the HAL, scheduler, and disruption handling.

Run with: python -m pytest tests/ -v   (or python -m unittest tests.test_basic)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator import facility, replanner
from fleet_orchestrator.dispatcher import FleetDispatcher
from fleet_orchestrator.hal.base import (
    CHARGE_CC_LIMIT_PCT,
    CHARGE_DISPATCH_TARGET_PCT,
    TRAVEL_MINUTES,
    TRAVEL_MINUTES_DOCK,
    charge_minutes_to_target,
    charge_pct_after,
)
from fleet_orchestrator.hal.floorbot import bucket_pct_bounds, bucket_to_minutes_range, pct_to_bucket
from fleet_orchestrator.hal.registry import build_adapter
from fleet_orchestrator.models import OEM, RobotStatus, Telemetry
from fleet_orchestrator.monitor import Monitor
from fleet_orchestrator.profile import ProfileStore, extract_trip_segments, minutes_remaining
from fleet_orchestrator.scenario import build_shift
from fleet_orchestrator.scheduler import Assignment, generate_schedule
import random


class TestHALNormalization(unittest.TestCase):
    """Every adapter must expose the same Telemetry schema regardless of
    the underlying OEM transport."""

    def test_all_adapters_return_common_schema(self):
        fleet = facility.build_fleet()
        for rid, spec in fleet.items():
            adapter = build_adapter(spec)
            telem = adapter.status_query()
            self.assertEqual(telem.robot_id, rid)
            self.assertIn(telem.status, list(RobotStatus))
            self.assertIsInstance(telem.error_codes, list)
            if spec.dry_only:
                self.assertIsNone(telem.water_pct)
            else:
                self.assertIsNotNone(telem.water_pct)

    def test_floorbot_reports_uncertain_water(self):
        spec = facility.build_fleet()["R-006"]
        adapter = build_adapter(spec)
        telem = adapter.status_query()
        self.assertTrue(telem.water_uncertain)
        self.assertIn(telem.meta["water_bucket"], ("empty", "low", "med", "high"))

    def test_autoscrub_and_cleanpath_report_certain_water_or_none(self):
        fleet = facility.build_fleet()
        auto = build_adapter(fleet["R-001"])
        self.assertFalse(auto.status_query().water_uncertain)
        clean = build_adapter(fleet["R-005"])
        self.assertIsNone(clean.status_query().water_pct)

    def test_bucket_conversion_monotonic(self):
        self.assertEqual(pct_to_bucket(2), "empty")
        self.assertEqual(pct_to_bucket(50), "med")
        self.assertEqual(pct_to_bucket(90), "high")
        lo, hi = bucket_to_minutes_range("low", 1.5)
        self.assertLess(lo, hi)

    def test_cleanpath_ws_drop_and_reconnect(self):
        spec = facility.build_fleet()["R-005"]
        adapter = build_adapter(spec)
        adapter.simulate_ws_drop()
        telem = adapter.status_query()
        self.assertFalse(telem.meta["connected"])
        for _ in range(1):  # 0.25 min reconnect < 1 tick
            adapter.tick(1.0)
        telem2 = adapter.status_query()
        self.assertTrue(telem2.meta["connected"])

    def test_fourth_oem_is_a_pure_addition(self):
        """Adding a new OEM should not require touching scheduler/dispatcher/
        monitor -- only hal/registry.py's lookup table."""
        from fleet_orchestrator.hal import registry
        self.assertEqual(set(registry._ADAPTERS.keys()),
                         {OEM.AUTOSCRUB, OEM.CLEANPATH, OEM.FLOORBOT})


class TestScheduler(unittest.TestCase):
    def test_sterile_zones_only_go_to_certified_robot(self):
        fleet = facility.build_fleet()
        zones = facility.build_zones()
        schedule = generate_schedule(fleet, zones, "Tue")
        for rid, tasks in schedule.items():
            for a in tasks:
                zone = zones[a.zone_id]
                if zone.classification == "Sterile":
                    self.assertEqual(rid, "R-003", f"{rid} must not be assigned {a.zone_id}")

    def test_carpet_zone_skipped_on_tuesday(self):
        fleet = facility.build_fleet()
        zones = facility.build_zones()
        schedule = generate_schedule(fleet, zones, "Tue")
        assigned_zone_ids = {a.zone_id for tasks in schedule.values() for a in tasks}
        self.assertNotIn("Z4", assigned_zone_ids)

    def test_carpet_zone_scheduled_on_wednesday(self):
        fleet = facility.build_fleet()
        zones = facility.build_zones()
        schedule = generate_schedule(fleet, zones, "Wed")
        assigned = {a.zone_id: rid for rid, tasks in schedule.items() for a in tasks if a.zone_id == "Z4"}
        self.assertIn("Z4", assigned)
        self.assertEqual(assigned["Z4"], "R-004")

    def test_dry_only_robot_never_assigned_sterile(self):
        fleet = facility.build_fleet()
        zones = facility.build_zones()
        schedule = generate_schedule(fleet, zones, "Tue")
        for rid, tasks in schedule.items():
            spec = fleet[rid]
            if spec.dry_only:
                for a in tasks:
                    self.assertNotEqual(zones[a.zone_id].classification, "Sterile")


class TestSanitization(unittest.TestCase):
    """Dock is a non-sterile area, so a sterile-certified robot's very
    first zone of the night must trigger a sanitization cycle if that
    first zone is sterile -- not skip it just because there's no "previous
    zone" yet. (Regression: last_classification used to seed as None, and
    needs_sanitization() had a `last_classification is not None` guard
    that silently skipped the very first transition of the shift.)"""

    def test_sanitization_fires_before_first_sterile_zone_of_the_night(self):
        fleet = facility.build_fleet()
        zones = facility.build_zones()
        sterile_zone = zones["Z7"]
        schedule = {"R-003": [Assignment("Z7", "R-003", 0, 30.0)]}
        monitor = Monitor({"R-003": fleet["R-003"]})
        rng = random.Random(1)
        adapters = {"R-003": build_adapter(fleet["R-003"])}
        dispatcher = FleetDispatcher({"R-003": fleet["R-003"]}, adapters, schedule,
                                     {"Z7": sterile_zone}, monitor, replanner, rng)
        ctrl = dispatcher.controller("R-003")
        for _ in range(30):
            dispatcher.step()
            if ctrl.phase == "SANITIZE":
                return
        self.fail("R-003 never sanitized before its first (sterile) zone of the shift")


class TestDualConstraint(unittest.TestCase):
    """A scrubber with limited water must not clean past its tank -- the
    dispatcher should insert a water cycle, not silently let water go negative."""

    def test_water_never_goes_negative_and_cycle_is_recorded(self):
        fleet = {"R-001": facility.build_fleet()["R-001"]}
        zones = {"Z1": facility.build_zones()["Z1"]}
        # Force a long single-robot clean far exceeding the 90-min tank.
        schedule = {"R-001": [Assignment("Z1", "R-001", 0, 500.0)]}
        monitor = Monitor(fleet)
        rng = random.Random(1)
        adapters = {"R-001": build_adapter(fleet["R-001"])}
        dispatcher = FleetDispatcher(fleet, adapters, schedule, zones, monitor, replanner, rng)
        min_water_seen = 100.0
        for _ in range(400):
            dispatcher.step()
            telem = dispatcher.controller("R-001").adapter.status_query()
            min_water_seen = min(min_water_seen, telem.water_pct)
        self.assertGreaterEqual(min_water_seen, 0.0)
        self.assertGreater(dispatcher.controller("R-001").stats["water_cycles"], 0)


class TestChargingCurve(unittest.TestCase):
    """Charging follows a two-phase CC/CV curve (fast to 90%, slow taper to
    100%), and the Dispatcher's dock-service policy targets 90%, not 100%."""

    def test_cc_phase_reaches_90_in_30_minutes(self):
        self.assertAlmostEqual(charge_pct_after(0.0, 30.0), CHARGE_CC_LIMIT_PCT, places=6)

    def test_cv_phase_reaches_100_in_60_more_minutes(self):
        pct = charge_pct_after(CHARGE_CC_LIMIT_PCT, 60.0)
        self.assertAlmostEqual(pct, 100.0, places=6)

    def test_full_charge_is_still_90_minutes_total(self):
        self.assertAlmostEqual(charge_pct_after(0.0, 90.0), 100.0, places=6)

    def test_cv_phase_is_much_slower_than_cc_phase(self):
        # same 10 percentage points, compare time cost on each side of 90%
        cc_10pts = charge_minutes_to_target(0.0, 10.0)
        cv_10pts = charge_minutes_to_target(90.0, 100.0)
        self.assertGreater(cv_10pts, 5 * cc_10pts)  # CV is ~18x slower, assert a loose bound

    def test_single_tick_straddling_90_percent_uses_both_rates(self):
        # start 2 min of CC room short of 90%, advance 5 min -> crosses into CV
        start = CHARGE_CC_LIMIT_PCT - 2 * (CHARGE_CC_LIMIT_PCT / 30.0)  # 2 min of CC room
        pct = charge_pct_after(start, 5.0)
        naive_cc_only = start + 5.0 * (CHARGE_CC_LIMIT_PCT / 30.0)
        self.assertLess(pct, naive_cc_only)  # slower once past 90%, not still at the CC rate

    def test_dispatcher_redeploys_at_90_not_100(self):
        fleet = {"R-005": facility.build_fleet()["R-005"]}  # dry robot: charge is the only constraint
        zones = {"Z1": facility.build_zones()["Z1"]}
        schedule = {"R-005": [Assignment("Z1", "R-005", 0, 500.0)]}
        monitor = Monitor(fleet)
        rng = random.Random(1)
        adapters = {"R-005": build_adapter(fleet["R-005"])}
        dispatcher = FleetDispatcher(fleet, adapters, schedule, zones, monitor, replanner, rng)
        saw_dock_service = False
        redeploy_battery = None
        for _ in range(500):
            dispatcher.step()
            ctrl = dispatcher.controller("R-005")
            if ctrl.phase == "DOCK_SERVICE":
                saw_dock_service = True
            elif saw_dock_service and ctrl.phase != "DOCK_SERVICE" and redeploy_battery is None:
                redeploy_battery = ctrl.adapter.status_query().battery_pct
                break
        self.assertTrue(saw_dock_service)
        self.assertIsNotNone(redeploy_battery)
        # left the dock around 90% (minus the flat 2% travel debit already
        # applied by the time this reads), not chased up toward 100% --
        # under the old target-100% policy this would read ~98%.
        self.assertGreaterEqual(redeploy_battery, 85.0)
        self.assertLess(redeploy_battery, 95.0)


class TestTransportModeTravel(unittest.TestCase):
    """Dock-service legs (zone->dock, dock->zone) run at transport-mode
    speed, faster than the baseline zone-to-zone transition cost."""

    def test_dock_leg_is_faster_than_baseline(self):
        self.assertLess(TRAVEL_MINUTES_DOCK, TRAVEL_MINUTES)

    def test_dock_return_uses_the_fast_leg(self):
        fleet = {"R-001": facility.build_fleet()["R-001"]}
        zones = {"Z1": facility.build_zones()["Z1"]}
        schedule = {"R-001": [Assignment("Z1", "R-001", 0, 500.0)]}
        monitor = Monitor(fleet)
        rng = random.Random(1)
        adapters = {"R-001": build_adapter(fleet["R-001"])}
        dispatcher = FleetDispatcher(fleet, adapters, schedule, zones, monitor, replanner, rng)
        ctrl = dispatcher.controller("R-001")
        for _ in range(200):
            dispatcher.step()
            if ctrl.travel_destination == "DOCK" and ctrl.phase == "TRAVEL":
                self.assertEqual(ctrl.phase_timer, TRAVEL_MINUTES_DOCK)
                return
        self.fail("robot never returned to dock within 200 ticks")


class TestFloorBotWaterEstimator(unittest.TestCase):
    """Usage-time model fused with the coarse bucket reading -- a
    continuous estimate clamped to what the bucket actually supports."""

    def test_fresh_robot_reports_full_water_no_drift(self):
        spec = facility.build_fleet()["R-006"]
        adapter = build_adapter(spec)
        telem = adapter.status_query()
        self.assertAlmostEqual(telem.water_pct, 100.0, delta=0.5)
        self.assertEqual(telem.meta["water_model_bucket_drift_pct"], 0.0)

    def test_usage_model_decreases_while_cleaning(self):
        spec = facility.build_fleet()["R-006"]
        adapter = build_adapter(spec)
        adapter.phys.status = RobotStatus.CLEANING
        adapter.phys.position = "Z1"
        for _ in range(20):
            adapter.tick(1.0)
        telem = adapter.status_query()
        self.assertLess(telem.water_pct, 100.0)
        self.assertLess(telem.meta["water_usage_model_pct"], 100.0)

    def test_fused_estimate_never_leaves_the_bucket_range(self):
        spec = facility.build_fleet()["R-006"]
        adapter = build_adapter(spec)
        adapter.phys.status = RobotStatus.CLEANING
        adapter.phys.position = "Z1"
        for _ in range(90):  # a full tank's worth of active cleaning
            adapter.tick(1.0)
            telem = adapter.status_query()
            lo, hi = bucket_pct_bounds(telem.meta["water_bucket"])
            self.assertGreaterEqual(telem.water_pct, lo - 0.01)
            self.assertLessEqual(telem.water_pct, hi + 0.01)

    def test_usage_clock_resets_once_tank_is_physically_full(self):
        spec = facility.build_fleet()["R-006"]
        adapter = build_adapter(spec)
        adapter.phys.status = RobotStatus.CLEANING
        adapter.phys.position = "Z1"
        for _ in range(30):
            adapter.tick(1.0)
        self.assertGreater(adapter._active_minutes_since_refill, 0.0)
        # simulate a completed refill: tank physically back to 100%
        adapter.phys.status = RobotStatus.DOCK_SERVICE
        adapter.phys.water_pct = 100.0
        adapter.tick(1.0)
        self.assertEqual(adapter._active_minutes_since_refill, 0.0)

    def test_monitor_flags_large_model_bucket_divergence(self):
        fleet = {"R-006": facility.build_fleet()["R-006"]}
        monitor = Monitor(fleet)
        spec = fleet["R-006"]
        adapter = build_adapter(spec)
        adapter.phys.status = RobotStatus.CLEANING
        adapter.phys.position = "Z1"
        monitor.ingest(adapter.status_query(), spec)  # baseline sample
        adapter.forced_bucket_override = "low"        # sensor says far less than usage implies
        monitor.ingest(adapter.status_query(), spec)
        kinds = [a["kind"] for a in monitor.anomalies]
        self.assertIn("water_model_bucket_divergence", kinds)

    def test_monitor_does_not_flag_divergence_mid_refill(self):
        fleet = {"R-006": facility.build_fleet()["R-006"]}
        monitor = Monitor(fleet)
        spec = fleet["R-006"]
        adapter = build_adapter(spec)
        adapter.phys.status = RobotStatus.CLEANING
        adapter.phys.position = "Z1"
        for _ in range(60):  # drain most of the tank
            adapter.tick(1.0)
        monitor.ingest(adapter.status_query(), spec)
        adapter.phys.status = RobotStatus.DOCK_SERVICE  # now mid-refill: bucket will jump ahead of the model
        adapter.tick(5.0)
        monitor.ingest(adapter.status_query(), spec)
        kinds = [a["kind"] for a in monitor.anomalies]
        self.assertNotIn("water_model_bucket_divergence", kinds)


class TestReplanner(unittest.TestCase):
    def test_robot_failure_escalates_when_no_backup(self):
        fleet = facility.build_fleet()
        zones = facility.build_zones()
        schedule = generate_schedule(fleet, zones, "Tue")
        monitor = Monitor(fleet)
        adapters = {rid: build_adapter(spec) for rid, spec in fleet.items()}
        rng = random.Random(1)
        dispatcher = FleetDispatcher(fleet, adapters, schedule, zones, monitor, replanner, rng)
        decision = replanner.handle_robot_failure(dispatcher, monitor, "R-003", t=100)
        self.assertIn("NO other sterile-certified robot", decision)
        self.assertTrue(dispatcher.controller("R-003").disabled)

    def test_ws_drop_reconnects_within_grace_period(self):
        fleet = facility.build_fleet()
        zones = facility.build_zones()
        schedule = generate_schedule(fleet, zones, "Tue")
        monitor = Monitor(fleet)
        adapters = {rid: build_adapter(spec) for rid, spec in fleet.items()}
        rng = random.Random(1)
        dispatcher = FleetDispatcher(fleet, adapters, schedule, zones, monitor, replanner, rng)
        grace = replanner.handle_ws_drop(dispatcher, monitor, "R-005", t=50)
        for _ in range(int(grace) + 2):
            dispatcher.step()
        connected = replanner.check_ws_reconnect(dispatcher, monitor, "R-005", t=50 + grace)
        self.assertTrue(connected)
        self.assertFalse(dispatcher.controller("R-005").disabled)

    def test_ws_drop_escalates_if_never_reconnects(self):
        fleet = facility.build_fleet()
        zones = facility.build_zones()
        schedule = generate_schedule(fleet, zones, "Tue")
        monitor = Monitor(fleet)
        adapters = {rid: build_adapter(spec) for rid, spec in fleet.items()}
        rng = random.Random(1)
        dispatcher = FleetDispatcher(fleet, adapters, schedule, zones, monitor, replanner, rng)
        ctrl = dispatcher.controller("R-005")
        ctrl.adapter.simulate_ws_drop()
        ctrl.adapter._connected = False
        ctrl.adapter._reconnect_eta = 10_000  # never, within this test
        connected = replanner.check_ws_reconnect(dispatcher, monitor, "R-005", t=50)
        self.assertFalse(connected)
        self.assertTrue(ctrl.disabled)

    def test_adhoc_request_pulls_enough_robots_for_full_coverage(self):
        fleet = facility.build_fleet()
        zones = facility.build_zones()
        schedule = generate_schedule(fleet, zones, "Tue")
        monitor = Monitor(fleet)
        adapters = {rid: build_adapter(spec) for rid, spec in fleet.items()}
        rng = random.Random(1)
        dispatcher = FleetDispatcher(fleet, adapters, schedule, zones, monitor, replanner, rng)
        full, achievable = replanner.handle_adhoc_request(dispatcher, monitor, "Z1", t=0, window_end_t=60)
        self.assertGreater(achievable, 0)


def _telem(t, battery_pct, water_pct, status):
    return Telemetry(robot_id="R-X", timestamp=t, battery_pct=battery_pct, water_pct=water_pct,
                     water_uncertain=False, position="Z1", status=status, error_codes=[], meta={})


class TestConsumableProfile(unittest.TestCase):
    """Trip segmentation, learned rates, and the two anomaly checks
    (self-baseline degradation, same-OEM/model peer outlier) -- all
    exercised against hand-built synthetic histories so degradation and
    leaks can be proven to fire correctly without depending on this
    simulator's physics actually modeling aging (it deliberately doesn't;
    see SPEC.md)."""

    def test_segmentation_normalizes_by_active_minutes_not_wall_clock(self):
        # 30 active minutes, with travel/idle time on either side that
        # must NOT count toward the rate.
        history = [
            _telem(0, 100.0, 100.0, RobotStatus.EN_ROUTE),
            _telem(1, 98.0, 100.0, RobotStatus.CLEANING),   # active starts
            _telem(31, 68.0, 70.0, RobotStatus.CLEANING),   # 30 active min later
            _telem(32, 68.0, 70.0, RobotStatus.EN_ROUTE),   # active ends
            _telem(40, 68.0, 70.0, RobotStatus.IDLE),
        ]
        segs = extract_trip_segments(history, "R-X", "AutoScrub", "AS-900")
        self.assertEqual(len(segs), 1)
        seg = segs[0]
        self.assertEqual(seg.active_minutes, 30)
        # 30% battery used over 30 active min -> 60%/60min, not diluted by the 40 wall-clock min
        self.assertAlmostEqual(seg.battery_rate_per_60min, 60.0, delta=0.5)

    def test_learned_rate_averages_recent_window(self):
        store = ProfileStore()
        for i, rate in enumerate([20.0, 22.0, 24.0, 26.0, 28.0, 100.0]):  # last one outside window=5
            store.segments.append(_seg("R-001", "AutoScrub", "AS-900", battery_rate=rate, shift=i))
        learned = store.learned_rate("R-001", "battery", window=5)
        self.assertAlmostEqual(learned, statistics_mean([22.0, 24.0, 26.0, 28.0, 100.0]))

    def test_flag_degradation_fires_on_synthetic_aging_drift(self):
        store = ProfileStore()
        # baseline ~25%/60min for the first 3 trips, drifting up to ~35%/60min for the last 3
        rates = [25.0, 25.0, 25.0, 30.0, 33.0, 35.0]
        for i, r in enumerate(rates):
            store.segments.append(_seg("R-001", "AutoScrub", "AS-900", battery_rate=r, shift=i))
        flag = store.flag_degradation("R-001", "battery")
        self.assertIsNotNone(flag)
        self.assertGreaterEqual(flag["ratio"], 1.25)

    def test_flag_degradation_does_not_fire_on_stable_rate(self):
        store = ProfileStore()
        for i in range(8):
            store.segments.append(_seg("R-001", "AutoScrub", "AS-900", battery_rate=25.0 + (i % 2), shift=i))
        self.assertIsNone(store.flag_degradation("R-001", "battery"))

    def test_flag_degradation_needs_minimum_history(self):
        store = ProfileStore()
        for i in range(3):  # below min_total=6
            store.segments.append(_seg("R-001", "AutoScrub", "AS-900", battery_rate=25.0 + i * 20, shift=i))
        self.assertIsNone(store.flag_degradation("R-001", "battery"))

    def test_flag_peer_outlier_fires_for_a_bad_unit_among_healthy_siblings(self):
        store = ProfileStore()
        # R-006 and R-007 (siblings, same OEM+model) run a healthy ~66%/60min water rate
        for i in range(6):
            store.segments.append(_seg("R-006", "FloorBot", "FB-200", water_rate=66.0 + (i % 2), shift=i))
            store.segments.append(_seg("R-007", "FloorBot", "FB-200", water_rate=66.0 + (i % 2), shift=i))
        # R-008 (same OEM+model) is leaking: notably higher water rate every trip
        for i in range(6):
            store.segments.append(_seg("R-008", "FloorBot", "FB-200", water_rate=95.0, shift=i))
        flag = store.flag_peer_outlier("R-008", oem="FloorBot", model="FB-200", resource="water")
        self.assertIsNotNone(flag)
        self.assertGreater(flag["own_rate_per_60min"], flag["peer_rate_per_60min"])

    def test_flag_peer_outlier_does_not_fire_for_a_normal_unit(self):
        store = ProfileStore()
        for i in range(6):
            store.segments.append(_seg("R-006", "FloorBot", "FB-200", water_rate=66.0 + (i % 2), shift=i))
            store.segments.append(_seg("R-007", "FloorBot", "FB-200", water_rate=65.0 + (i % 2), shift=i))
            store.segments.append(_seg("R-008", "FloorBot", "FB-200", water_rate=66.5 + (i % 2), shift=i))
        self.assertIsNone(store.flag_peer_outlier("R-008", oem="FloorBot", model="FB-200", resource="water"))

    def test_minutes_remaining_basic_math(self):
        # 50% usable headroom at a rate of 25%/60min -> 120 active minutes left
        self.assertAlmostEqual(minutes_remaining(current_pct=60.0, threshold_pct=10.0, rate_per_60min=25.0), 120.0)
        self.assertIsNone(minutes_remaining(current_pct=60.0, threshold_pct=10.0, rate_per_60min=None))

    def test_eta_present_in_monitor_snapshot(self):
        fleet = facility.build_fleet()
        monitor = Monitor(fleet)
        telem = _telem(10, 80.0, 90.0, RobotStatus.CLEANING)
        telem.robot_id = "R-001"
        monitor.ingest(telem, fleet["R-001"])
        snap = monitor.robots["R-001"]
        self.assertIsNotNone(snap.eta_battery_min)
        self.assertEqual(snap.eta_battery_source, "spec")

    def test_offline_mission_telemetry_is_reconciled_not_lost(self):
        """Regression: buffered telemetry from an offline-mission robot
        (e.g. R-008/R-006 at Z8) used to be counted and logged but never
        actually fed into Monitor -- meaning an offline robot could never
        accumulate a consumption profile at all. Now it's ingested on
        reconnect, in original chronological order."""
        dispatcher, monitor, schedule = build_shift()
        dispatcher.run_until(720)
        offline_zone_robot = None
        for rid, tasks in schedule.items():
            if any(dispatcher.zones[a.zone_id].wifi is False for a in tasks):
                offline_zone_robot = rid
                break
        self.assertIsNotNone(offline_zone_robot, "no robot assigned to the no-WiFi zone this schedule")
        hist = monitor.history[offline_zone_robot]
        active_samples = [t for t in hist if t.status == RobotStatus.OFFLINE_MISSION]
        self.assertGreater(len(active_samples), 0,
                           f"{offline_zone_robot}'s offline-mission telemetry was never reconciled into Monitor")


def _seg(robot_id, oem, model, battery_rate=None, water_rate=None, shift=0):
    from fleet_orchestrator.profile import TripSegment
    return TripSegment(
        robot_id=robot_id, oem=oem, model=model, shift_label=f"shift{shift}",
        start_t=shift * 1000, end_t=shift * 1000 + 60, active_minutes=60.0,
        battery_used_pct=battery_rate or 0.0, battery_rate_per_60min=battery_rate or 0.0,
        water_used_pct=water_rate, water_rate_per_60min=water_rate,
    )


def statistics_mean(vals):
    import statistics
    return statistics.mean(vals)


if __name__ == "__main__":
    unittest.main()
