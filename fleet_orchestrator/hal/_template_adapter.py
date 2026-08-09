"""Copy-paste starting point for a 4th OEM adapter.

NOT wired into hal/registry.py or models.OEM -- this file is intentionally
inert. To actually add an OEM (e.g. for a live walkthrough, or a real 4th
vendor):

  1. Add the OEM to `models.OEM` (one enum line).
  2. Copy this file to hal/<oem_name>.py, rename the class, fill in the
     NotImplementedError bodies below with that OEM's real quirks.
  3. Register it in hal/registry.py's `_ADAPTERS` dict (one line).
  4. Add a RobotSpec for it in facility.py's fleet roster.

That's the whole change. Nothing in scheduler.py, dispatcher.py, or
monitor.py should need to be touched -- if it does, something about this
adapter is leaking OEM-specific detail past the HAL boundary, which is
exactly the failure mode the registry/factory pattern exists to prevent.
See SPEC.md for the full architectural rationale and
tests/test_basic.py::test_fourth_oem_is_a_pure_addition, which asserts
the registry's key set rather than anything about scheduler internals so
a future change can't quietly reintroduce an `if oem == "X"` branch
upstream of the HAL.
"""
from __future__ import annotations

from ..models import RobotStatus, Telemetry
from .base import RobotAdapter


class TemplateAdapter(RobotAdapter):
    """Rename this class and this file. Replace every NotImplementedError
    and every # TODO with this OEM's actual behavior."""

    def __init__(self, spec, rng=None):
        super().__init__(spec)
        # TODO: any per-instance state this OEM's quirk needs -- a
        # connection-state flag (like CleanPath's WebSocket drop), a
        # smoothing buffer (like AutoScrub's GPS drift), a forced-override
        # hook for injected-anomaly scenarios (like FloorBot's
        # forced_bucket_override). Not every OEM needs one of these; only
        # add state here if this OEM actually has a quirk to track.

    # ---- override tick() ONLY if this OEM has a quirk that needs to act
    # ---- every simulated minute (a dropout timer, a drift sample, a
    # ---- usage-time accrual for a derived estimate). If not, delete this
    # ---- method entirely and inherit RobotAdapter.tick() as-is -- it
    # ---- already advances the shared physics correctly.
    def tick(self, minutes: float = 1.0) -> None:
        super().tick(minutes)  # always call super() first: advances PhysicalState
        raise NotImplementedError(
            "TODO: this OEM's per-tick quirk behavior, or delete this "
            "override entirely if there isn't one"
        )

    def status_query(self) -> Telemetry:
        """The one method every adapter MUST implement. Translate whatever
        this OEM's real transport gives you (REST/MQTT, gRPC/WebSocket,
        HTTP-poll/XML, or something new -- LoRaWAN, a proprietary binary
        frame, whatever) into the shared Telemetry schema. This is where
        the entire "normalize telemetry into a common schema" requirement
        lives for this OEM -- nothing downstream should ever need to know
        this OEM's raw format."""
        raise NotImplementedError(
            "TODO: build and return a Telemetry(...) from self.phys, "
            "shaped exactly like every other adapter's, e.g.:\n"
            "\n"
            "return Telemetry(\n"
            "    robot_id=self.spec.robot_id,\n"
            "    timestamp=int(self._t),\n"
            "    battery_pct=round(self.phys.battery_pct, 1),\n"
            "    water_pct=...,          # None if this OEM's robots are dry-only\n"
            "    water_uncertain=...,    # True if this is a derived/coarse estimate, not a direct reading\n"
            "    position=self.phys.position,\n"
            "    status=self.phys.status,\n"
            "    error_codes=list(self.phys.error_codes),\n"
            "    meta={'transport': '<this OEM's real transport>', ...},  # anything OEM-specific\n"
            "                                                              # goes in meta, never as a new\n"
            "                                                              # top-level Telemetry field --\n"
            "                                                              # that would leak OEM detail into\n"
            "                                                              # the shared schema every other\n"
            "                                                              # adapter has to fill in too.\n"
            ")"
        )

    # ---- override start_mission / pause / resume / return_to_dock ONLY
    # ---- if this OEM's command semantics genuinely differ from the base
    # ---- implementation (e.g. a command that must be acknowledged before
    # ---- the physical state actually changes, unlike the other three
    # ---- OEMs' fire-and-forget commands). Most new OEMs won't need to
    # ---- touch these -- delete whichever you don't override.

    # ---- Quirk-handling checklist (pick what applies, delete the rest):
    #
    #   - Noisy/imprecise sensor (like AutoScrub's GPS drift): smooth it
    #     with a rolling buffer in tick(), expose the smoothed value plus
    #     a raw/confidence flag in meta -- never let raw noise leak upstream.
    #
    #   - Intermittent connectivity (like CleanPath's WebSocket drop):
    #     track a connected/stale flag, keep returning the last-known-good
    #     Telemetry with stale=True while disconnected rather than raising
    #     or returning None -- callers should never have to handle "no
    #     telemetry available" as a special case.
    #
    #   - Coarse/discrete sensor (like FloorBot's water buckets): expose
    #     water_uncertain=True and put the real granularity (a bucket, a
    #     range, a confidence level) in meta. Consider whether a
    #     usage-time model (see hal/floorbot.py) can be fused with the
    #     coarse reading for a better derived estimate -- but keep
    #     water_uncertain=True regardless; a fused estimate is still an
    #     estimate, not a sensor reading.
    #
    #   - Polling vs. push cadence: if this OEM is polled rather than
    #     pushed (like FloorBot's legacy HTTP), document the poll interval
    #     as a class constant even if it doesn't change behavior at this
    #     simulator's 1-minute tick resolution -- it's real information
    #     about the OEM that a future higher-resolution simulation would need.
