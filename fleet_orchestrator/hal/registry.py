"""Factory: RobotSpec.oem -> concrete adapter class.

This is the single place that knows all OEM adapters exist. Adding a 4th
OEM means: write fleet_orchestrator/hal/newoem.py subclassing RobotAdapter,
add one line here. The scheduler/dispatcher/monitor never import an OEM
adapter directly -- they only ever call build_adapter().
"""
from __future__ import annotations

from ..models import OEM, RobotSpec
from .autoscrub import AutoScrubAdapter
from .base import RobotAdapter
from .cleanpath import CleanPathAdapter
from .floorbot import FloorBotAdapter

_ADAPTERS = {
    OEM.AUTOSCRUB: AutoScrubAdapter,
    OEM.CLEANPATH: CleanPathAdapter,
    OEM.FLOORBOT: FloorBotAdapter,
}


def build_adapter(spec: RobotSpec) -> RobotAdapter:
    try:
        cls = _ADAPTERS[spec.oem]
    except KeyError:
        raise ValueError(f"No HAL adapter registered for OEM {spec.oem!r}")
    return cls(spec)
