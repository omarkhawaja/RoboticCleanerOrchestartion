"""Static definitions for Regional General Hospital and the 8-robot fleet.

Times are stored as minutes-since-19:00 (see models.fmt_time). This is the
only file that hardcodes the assignment's numbers -- everything downstream
(scheduler, dispatcher, monitor) is generic over whatever roster/zone list
it is handed.
"""
from __future__ import annotations

from typing import Dict, List

from .models import FloorType, OEM, RobotSpec, Zone

H = 60  # one hour in minutes, for readability below


def build_zones() -> Dict[str, Zone]:
    zones = [
        Zone("Z1", "Main Lobby", 20_000, FloorType.HARD, "High-traffic", 2 * H, 11 * H),
        Zone("Z2", "ED Hallways", 3_800, FloorType.HARD, "Sterile", 8 * H, 10 * H),
        Zone("Z3", "Cafeteria", 2_600, FloorType.MIXED, "Standard", 3 * H, 10 * H),
        Zone("Z4", "Admin Wing", 5_100, FloorType.CARPET, "Standard", 0 * H, 4 * H, days=["Mon", "Wed", "Fri"]),
        Zone("Z5", "Patient Halls (2F)", 6_400, FloorType.HARD, "Sterile", 6 * H, 10 * H),
        Zone("Z6", "Outpatient Wing", 4_800, FloorType.HARD, "Standard", 1 * H, 11 * H),
        Zone("Z7", "Radiology Suite", 2_200, FloorType.HARD, "Sterile", 4 * H, 9 * H),
        Zone("Z8", "Parking Garage L1", 12_000, FloorType.CONCRETE, "Standard", 0, 12 * H,
             wifi=False, days=["Tue", "Sat"]),
    ]
    return {z.zone_id: z for z in zones}


# Capability tags used by the scheduler's eligibility check (facility.can_clean).
#   hard_scrub        -- wet scrub, hard floor, non-sterile
#   sterile_scrub      -- wet scrub, sterile-classified zones (AS-900H only)
#   carpet_vacuum      -- carpet, dry
#   multi_surface_dry  -- hard/mixed/concrete floors, dry mop/vacuum only

def build_fleet() -> Dict[str, RobotSpec]:
    robots = [
        RobotSpec("R-001", OEM.AUTOSCRUB, "AS-900", 8_000, 4.0, 1.5, ["hard_scrub"]),
        RobotSpec("R-002", OEM.AUTOSCRUB, "AS-900", 8_000, 4.0, 1.5, ["hard_scrub"]),
        RobotSpec("R-003", OEM.AUTOSCRUB, "AS-900H", 4_500, 3.0, 1.5,
                  ["hard_scrub", "sterile_scrub"], sterile_certified=True),
        RobotSpec("R-004", OEM.CLEANPATH, "CP-V2", 5_000, 3.5, None, ["carpet_vacuum"]),
        RobotSpec("R-005", OEM.CLEANPATH, "CP-X1", 6_000, 3.0, None, ["multi_surface_dry"]),
        RobotSpec("R-006", OEM.FLOORBOT, "FB-200", 7_000, 3.5, 1.5, ["hard_scrub"]),
        RobotSpec("R-007", OEM.CLEANPATH, "CP-X1", 6_000, 3.0, None, ["multi_surface_dry"]),
        RobotSpec("R-008", OEM.FLOORBOT, "FB-200", 7_000, 3.5, 1.5, ["hard_scrub"]),
    ]
    return {r.robot_id: r for r in robots}


def can_clean(robot: RobotSpec, zone: Zone) -> bool:
    """Capability eligibility -- floor type, sterile cert, wet vs dry."""
    if zone.classification == "Sterile":
        return robot.sterile_certified and "sterile_scrub" in robot.capabilities
    if zone.floor_type == FloorType.CARPET:
        return "carpet_vacuum" in robot.capabilities
    if zone.floor_type in (FloorType.HARD, FloorType.MIXED, FloorType.CONCRETE):
        return "hard_scrub" in robot.capabilities or "multi_surface_dry" in robot.capabilities
    return False


def wants_wet_scrub(zone: Zone) -> bool:
    """Zones where a wet scrub is the preferred primary clean, not just a
    bonus pass: concrete (grime/oil, e.g. a parking garage) and high-traffic
    hard floor. Routine Standard hard/mixed floors are fine with a dry
    pass as primary (CleanPath) with wet scrub only as an opportunistic
    secondary -- see scheduler.generate_schedule."""
    return zone.floor_type == FloorType.CONCRETE or zone.classification == "High-traffic"
