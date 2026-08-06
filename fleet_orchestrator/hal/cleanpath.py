"""CleanPath adapter: gRPC commands + WebSocket/protobuf telemetry stream.

Quirk handled here: drops the WebSocket on floor transitions, auto-reconnects
in ~15s. We simulate connection state explicitly; while disconnected,
status_query() returns the last-known-good telemetry with a `stale` flag
instead of raising -- the Dispatcher/Replanner decide what "stale for how
long" means (see replanner.py: WS_RECONNECT_GRACE_MIN), this adapter's job
is only to report connectivity honestly, not to make that judgment call.
"""
from __future__ import annotations

import random
import zlib

from ..models import RobotStatus, Telemetry
from .base import RobotAdapter

RECONNECT_MINUTES = 0.25  # ~15 sim-seconds, per the OEM profile


class CleanPathAdapter(RobotAdapter):
    def __init__(self, spec, rng: random.Random | None = None):
        super().__init__(spec)
        self._rng = rng or random.Random(zlib.crc32(spec.robot_id.encode()))
        self._connected = True
        self._reconnect_eta = 0.0
        self._last_good: Telemetry | None = None

    def simulate_ws_drop(self) -> None:
        """Force-inject the known floor-transition disconnect quirk."""
        self._connected = False
        self._reconnect_eta = self._t + RECONNECT_MINUTES

    def tick(self, minutes: float = 1.0) -> None:
        super().tick(minutes)
        if not self._connected and self._t >= self._reconnect_eta:
            self._connected = True

    def status_query(self) -> Telemetry:
        live = Telemetry(
            robot_id=self.spec.robot_id,
            timestamp=int(self._t),
            battery_pct=round(self.phys.battery_pct, 1),
            water_pct=None,  # CleanPath robots carry no water tank
            water_uncertain=False,
            position=self.phys.position,
            status=self.phys.status,
            error_codes=list(self.phys.error_codes),
            meta={"transport": "WebSocket/protobuf", "connected": True, "stale": False},
        )
        if self._connected:
            self._last_good = live
            return live
        # disconnected: report last-known-good, flagged stale, not a phantom "OFFLINE"
        if self._last_good is None:
            self._last_good = live
        stale = Telemetry(**{**self._last_good.__dict__})
        stale.meta = {**self._last_good.meta, "connected": False, "stale": True,
                       "reconnect_eta_min": round(self._reconnect_eta - self._t, 2)}
        return stale
