"""State persistence across restarts.

Scope note (see SPEC.md "Persistence granularity" for the full tradeoff):
this orchestrator is a nightly batch scheduler, so the realistic restart
scenario is "process restarts between shifts," not "process restarts mid-
tick." We persist at shift-boundary granularity: fleet state, the night's
schedule, the full telemetry history, and the shift report are written to
disk when a shift ends and can be reloaded (e.g. to answer "what happened
last Tuesday" or to seed tomorrow's aging/consumable trend analysis)
without re-running the simulation. Mid-shift crash recovery would extend
this same JSON schema to checkpoint every N ticks -- nothing about the
schema changes, only the write frequency.
"""
from __future__ import annotations

import json
import os
from typing import Dict

from .models import Telemetry


def save_shift_state(path: str, *, day: str, monitor, controllers, schedule) -> None:
    payload = {
        "day": day,
        "robots": {
            rid: {
                "oem": snap.oem.value,
                "battery_pct": snap.battery_pct,
                "water_pct": snap.water_pct,
                "water_bucket": snap.water_bucket,
                "position": snap.position,
                "status": snap.status.value,
                "last_update_t": snap.last_update_t,
                "error_codes": snap.error_codes,
            }
            for rid, snap in monitor.robots.items()
        },
        "zones": {
            zid: {
                "status": rec.status.value,
                "last_cleaned_t": rec.last_cleaned_t,
                "coverage": rec.coverage,
                "robot_id": rec.robot_id,
                "wet_scrub_secondary": rec.wet_scrub_secondary,
            }
            for zid, rec in monitor.zones.items()
        },
        "consumables": monitor.water_cycle_summary(controllers),
        "anomalies": monitor.anomalies,
        "disruption_log": monitor.disruption_log,
        "schedule": {
            rid: [
                {"zone_id": a.zone_id, "planned_start": a.planned_start,
                 "est_clean_minutes": a.est_clean_minutes, "role": a.role, "note": a.note}
                for a in assignments
            ]
            for rid, assignments in schedule.items()
        },
        "telemetry_history": {
            rid: [t.to_dict() for t in samples[-50:]]  # tail sample per robot; full history is large/append-only
            for rid, samples in monitor.history.items()
        },
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def load_shift_state(path: str) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
