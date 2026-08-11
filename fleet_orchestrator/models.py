"""Core data model shared by every layer of the orchestrator.

Nothing in here knows about MQTT, gRPC, XML polling, or any other OEM
transport detail -- that is the entire point of the HAL. This module is the
"common telemetry schema" and the domain objects (Zone, RobotSpec) the
scheduler/dispatcher/monitor reason about.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class OEM(str, Enum):
    AUTOSCRUB = "AutoScrub"
    CLEANPATH = "CleanPath"
    FLOORBOT = "FloorBot"


class RobotStatus(str, Enum):
    IDLE = "IDLE"
    EN_ROUTE = "EN_ROUTE"
    CLEANING = "CLEANING"
    CHARGING = "CHARGING"
    WATER_CYCLE = "WATER_CYCLE"
    DOCK_SERVICE = "DOCK_SERVICE"  # charging AND water refill concurrently
    SANITIZING = "SANITIZING"
    ESCORT_WAIT = "ESCORT_WAIT"
    OFFLINE_MISSION = "OFFLINE_MISSION"   # executing autonomously, no telemetry
    ERROR = "ERROR"
    OFFLINE = "OFFLINE"                    # hard fault, out of service


class FloorType(str, Enum):
    HARD = "Hard"
    CARPET = "Carpet"
    MIXED = "Mixed"
    CONCRETE = "Concrete"


class ZoneStatus(str, Enum):
    PENDING = "PENDING"
    SCHEDULED = "SCHEDULED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    MISSED = "MISSED"
    MISSED_ESCALATED = "MISSED_ESCALATED"  # missed AND escalated to a human (e.g. no sterile backup)
    NOT_SCHEDULED = "NOT_SCHEDULED"  # e.g. Z4 on a Tuesday


@dataclass
class Zone:
    zone_id: str
    name: str
    sqft: int
    floor_type: FloorType
    classification: str          # High-traffic | Sterile | Standard
    window_start: int            # minutes since shift start (19:00 = 0)
    window_end: int
    wifi: bool = True
    days: Optional[List[str]] = None   # None => every day

    def scheduled_today(self, day: str) -> bool:
        return self.days is None or day in self.days


@dataclass
class RobotSpec:
    robot_id: str
    oem: OEM
    model: str
    coverage_ft2_hr: float
    battery_hours: float
    water_hours: Optional[float]     # None => no water tank (dry-only)
    capabilities: List[str]
    sterile_certified: bool = False

    @property
    def dry_only(self) -> bool:
        return self.water_hours is None


@dataclass
class Telemetry:
    """The common schema every HAL adapter must normalize into."""
    robot_id: str
    timestamp: int
    battery_pct: float
    water_pct: Optional[float]       # None for dry robots
    water_uncertain: bool            # True => value is an estimate (e.g. FloorBot bucket)
    position: str
    status: RobotStatus
    error_codes: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)  # OEM-specific extras, never required by callers

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["status"] = self.status.value
        return d


def fmt_time(t) -> str:
    """t = minutes since 19:00 shift start -> 'HH:MM' (+1d if past midnight)."""
    t = int(t)
    total = (19 * 60 + t) % (24 * 60)
    day_offset = (19 * 60 + t) // (24 * 60)
    h, m = divmod(total, 60)
    suffix = "" if day_offset == 0 else "+1d"
    return f"{h:02d}:{m:02d}{suffix}"
