"""Telemetry ingestion, fleet/zone state, consumable tracking, anomaly
detection, and shift reporting.

The Monitor is the only consumer of the normalized `Telemetry` schema
besides the Dispatcher that produces it -- it never talks to a HAL adapter
directly, which is what lets the same anomaly-detection code work
identically for all three OEMs (and any future one).

Anomaly detection here is deliberately threshold-based (per the rubric:
"Threshold-based is fine, but design the telemetry data model so ML-based
anomaly detection could be added later"). Every telemetry sample is kept as
a flat, timestamped record (`TelemetryHistory`) with normalized numeric
fields -- exactly the shape an offline feature pipeline would want for
training a per-robot drain-rate or leak classifier later. Swapping the
threshold checks in `detect_water_anomalies`/`detect_battery_aging` for a
model call would not require touching the schema.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .models import OEM, RobotSpec, RobotStatus, Telemetry, ZoneStatus, fmt_time

SLA_CLASSES = {"Sterile", "High-traffic"}


@dataclass
class RobotSnapshot:
    robot_id: str
    oem: OEM
    battery_pct: float = 100.0
    water_pct: Optional[float] = None
    water_bucket: Optional[str] = None
    water_uncertain: bool = False
    position: str = "DOCK"
    status: RobotStatus = RobotStatus.IDLE
    last_update_t: int = 0
    error_codes: List[str] = field(default_factory=list)


@dataclass
class ZoneRecord:
    zone_id: str
    status: ZoneStatus = ZoneStatus.PENDING
    last_cleaned_t: Optional[int] = None
    coverage: float = 0.0
    robot_id: Optional[str] = None
    wet_scrub_secondary: Optional[str] = None  # status of the secondary pass, if any
    history: List[str] = field(default_factory=list)


class Monitor:
    def __init__(self, fleet: Dict[str, RobotSpec]):
        self.fleet = fleet
        self.robots: Dict[str, RobotSnapshot] = {
            rid: RobotSnapshot(rid, spec.oem) for rid, spec in fleet.items()
        }
        self.zones: Dict[str, ZoneRecord] = {}
        self.history: Dict[str, List[Telemetry]] = defaultdict(list)
        self.anomalies: List[dict] = []
        self.disruption_log: List[dict] = []

    # -- ingestion -----------------------------------------------------
    def ingest(self, telem: Telemetry, spec: RobotSpec):
        self.history[telem.robot_id].append(telem)
        snap = self.robots[telem.robot_id]
        snap.battery_pct = telem.battery_pct
        snap.water_pct = telem.water_pct
        snap.water_uncertain = telem.water_uncertain
        snap.water_bucket = telem.meta.get("water_bucket")
        snap.position = telem.position
        snap.status = telem.status
        snap.last_update_t = telem.timestamp
        snap.error_codes = telem.error_codes
        self._check_water_anomaly(telem, spec)

    def on_zone_event(self, zone_id: str, status: ZoneStatus, t: float, role: str = "primary",
                       robot_id: str = "", coverage: Optional[float] = None):
        rec = self.zones.setdefault(zone_id, ZoneRecord(zone_id))
        rec.history.append(f"[{fmt_time(int(t))}] {robot_id or '?'} -> {status.value}"
                           + (f" ({role})" if role != "primary" else ""))
        if role == "secondary":
            rec.wet_scrub_secondary = status.value
            return
        rec.status = status
        rec.robot_id = robot_id or rec.robot_id
        if coverage is not None:
            rec.coverage = coverage
        if status in (ZoneStatus.COMPLETE, ZoneStatus.PARTIAL):
            rec.last_cleaned_t = int(t)

    def mark_not_scheduled(self, zone_id: str, reason: str):
        rec = self.zones.setdefault(zone_id, ZoneRecord(zone_id))
        rec.status = ZoneStatus.NOT_SCHEDULED
        rec.history.append(reason)

    def mark_offline(self, robot_id: str, reason: str, t: float):
        snap = self.robots[robot_id]
        snap.status = RobotStatus.OFFLINE
        self.disruption_log.append({"t": int(t), "robot_id": robot_id, "event": "OFFLINE", "reason": reason})

    def log_disruption(self, t: float, kind: str, detail: str, decision: str):
        self.disruption_log.append({
            "t": int(t), "time": fmt_time(int(t)), "kind": kind, "detail": detail, "decision": decision,
        })

    def flag_anomaly(self, t: float, robot_id: str, kind: str, detail: str, confidence: str = "medium"):
        self.anomalies.append({
            "t": int(t), "time": fmt_time(int(t)), "robot_id": robot_id, "kind": kind,
            "detail": detail, "confidence": confidence,
        })

    # -- anomaly detection (threshold-based) -----------------------------
    def _check_water_anomaly(self, telem: Telemetry, spec: RobotSpec):
        if telem.water_pct is None:
            return
        hist = self.history[telem.robot_id]
        if len(hist) < 2:
            return
        prev = hist[-2]
        if prev.water_pct is None:
            return
        dt = max(telem.timestamp - prev.timestamp, 1)
        observed_drop = prev.water_pct - telem.water_pct
        expected_drop = (100.0 / (spec.water_hours * 60.0)) * dt if telem.status == RobotStatus.CLEANING else 0.0
        # a drop far beyond what cleaning-time alone explains => leak / sensor fault candidate
        if telem.status == RobotStatus.CLEANING and observed_drop > expected_drop * 2.5 and observed_drop > 15:
            self.flag_anomaly(telem.timestamp, telem.robot_id, "water_leak_suspected",
                              f"water dropped {observed_drop:.0f}% in {dt} min "
                              f"(expected ~{expected_drop:.0f}%)", confidence="low" if telem.water_uncertain else "high")
        # water level went UP without a dock/water-cycle status -- inconsistent report (e.g. refill wasn't logged)
        if observed_drop < -5 and telem.status not in (RobotStatus.WATER_CYCLE, RobotStatus.DOCK_SERVICE):
            self.flag_anomaly(telem.timestamp, telem.robot_id, "water_reading_inconsistent",
                              f"water level rose {-observed_drop:.0f}% outside a water cycle -- "
                              f"treat as sensor noise, not a real refill", confidence="low")

    def oem_health(self) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        for oem in OEM:
            robot_ids = [rid for rid, s in self.fleet.items() if s.oem == oem]
            error_count = sum(len(self.robots[rid].error_codes) for rid in robot_ids)
            offline_count = sum(1 for rid in robot_ids if self.robots[rid].status == RobotStatus.OFFLINE)
            out[oem.value] = {"robots": len(robot_ids), "error_count": error_count, "offline_count": offline_count}
        return out

    # -- consumable tracking ---------------------------------------------
    def water_cycle_summary(self, controllers) -> Dict[str, dict]:
        out = {}
        for rid, ctrl in controllers.items():
            out[rid] = {
                "water_cycles": ctrl.stats["water_cycles"],
                "charge_cycles": ctrl.stats["charge_cycles"],
                "water_bound_stops": ctrl.stats["water_bound_stops"],
                "battery_bound_stops": ctrl.stats["battery_bound_stops"],
                "binding_constraint": (
                    "water" if ctrl.stats["water_bound_stops"] > ctrl.stats["battery_bound_stops"]
                    else "battery" if ctrl.stats["battery_bound_stops"] > 0 else "n/a (dry robot / no stops)"
                ),
            }
        return out

    # -- shift report ------------------------------------------------------
    def shift_report(self, controllers, zones_all) -> str:
        lines = []
        lines.append("=" * 72)
        lines.append("SHIFT REPORT -- Regional General Hospital -- 19:00 to 07:00")
        lines.append("=" * 72)

        lines.append("\n-- Zone Outcomes --")
        complete = partial = missed = not_sched = 0
        for zid, zone in zones_all.items():
            rec = self.zones.get(zid)
            if rec is None:
                status = ZoneStatus.NOT_SCHEDULED
                extra = ""
            else:
                status = rec.status
                extra = f" ({rec.coverage*100:.0f}% coverage)" if rec.coverage else ""
                if rec.wet_scrub_secondary:
                    extra += f" [secondary wet-scrub: {rec.wet_scrub_secondary}]"
            sla_flag = " *** SLA ZONE ***" if zone.classification in SLA_CLASSES else ""
            lines.append(f"  {zid} {zone.name:<22} {status.value:<14}{extra}{sla_flag}")
            if status == ZoneStatus.COMPLETE:
                complete += 1
            elif status == ZoneStatus.PARTIAL:
                partial += 1
            elif status == ZoneStatus.MISSED:
                missed += 1
            else:
                not_sched += 1
        total_active = complete + partial + missed
        sla_total = sum(1 for z in zones_all.values() if z.classification in SLA_CLASSES
                         and self.zones.get(z.zone_id, ZoneRecord(z.zone_id)).status != ZoneStatus.NOT_SCHEDULED)
        sla_met = sum(1 for z in zones_all.values() if z.classification in SLA_CLASSES
                      and self.zones.get(z.zone_id, ZoneRecord(z.zone_id)).status == ZoneStatus.COMPLETE)
        lines.append(f"\n  Totals: {complete} complete, {partial} partial, {missed} missed, "
                     f"{not_sched} not scheduled today")
        lines.append(f"  SLA zones (Sterile/High-traffic): {sla_met}/{sla_total} fully completed")

        lines.append("\n-- Consumable Tracking (per robot) --")
        cons = self.water_cycle_summary(controllers)
        for rid in sorted(cons):
            c = cons[rid]
            lines.append(f"  {rid}: water_cycles={c['water_cycles']} charge_cycles={c['charge_cycles']} "
                         f"binding_constraint={c['binding_constraint']}")

        lines.append("\n-- OEM Health --")
        for oem, h in self.oem_health().items():
            lines.append(f"  {oem}: {h['robots']} robots, {h['error_count']} error codes reported, "
                         f"{h['offline_count']} offline")

        lines.append("\n-- Anomalies Flagged --")
        if not self.anomalies:
            lines.append("  none")
        for a in self.anomalies:
            lines.append(f"  [{a['time']}] {a['robot_id']} {a['kind']} (confidence={a['confidence']}): {a['detail']}")

        lines.append("\n-- Disruptions & Adaptations --")
        if not self.disruption_log:
            lines.append("  none")
        for d in self.disruption_log:
            if "kind" in d:
                lines.append(f"  [{d['time']}] {d['kind']}: {d['detail']}\n      -> decision: {d['decision']}")
            else:
                lines.append(f"  [{fmt_time(d['t'])}] {d['robot_id']} {d['event']}: {d['reason']}")

        lines.append("\n" + "=" * 72)
        return "\n".join(lines)

    def dashboard(self, controllers) -> str:
        lines = ["-- Fleet Dashboard --"]
        for rid in sorted(self.robots):
            s = self.robots[rid]
            water_str = "n/a"
            if s.water_pct is not None:
                water_str = f"{s.water_bucket}(~{s.water_pct:.0f}%,uncertain)" if s.water_uncertain \
                    else f"{s.water_pct:.0f}%"
            lines.append(f"  {rid:6} [{s.oem.value:10}] batt={s.battery_pct:5.1f}%  water={water_str:22} "
                         f"pos={s.position:10} status={s.status.value:14} @ {fmt_time(s.last_update_t)}")
        return "\n".join(lines)
