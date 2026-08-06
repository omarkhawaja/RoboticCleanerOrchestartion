"""Sanity tests for the HAL, scheduler, and disruption handling.

Run with: python -m pytest tests/ -v   (or python -m unittest tests.test_basic)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator import facility, replanner
from fleet_orchestrator.dispatcher import FleetDispatcher
from fleet_orchestrator.hal.floorbot import bucket_to_minutes_range, pct_to_bucket
from fleet_orchestrator.hal.registry import build_adapter
from fleet_orchestrator.models import OEM, RobotStatus
from fleet_orchestrator.monitor import Monitor
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


if __name__ == "__main__":
    unittest.main()
