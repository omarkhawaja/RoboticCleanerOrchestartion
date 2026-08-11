# Design Decisions, Assumptions & Tradeoffs

Deliberate choices made in this system, with rationale and the alternative
each one cost.

## 1. Objective function
Minimize risk-weighted incomplete coverage first, then robot-hours. Zones
rank Sterile > High-traffic > Standard, then earliest window close. A second
robot is added only when one robot would leave under 20% slack on its
window. Rejected alternatives: "maximize coverage" ignores that a missed
sterile OR corridor is categorically worse than a missed closet; "minimize
total time" would over-provision easy zones and starve tight ones.

## 2. Water, not battery, is the binding constraint
`analysis/binding_constraint_study.py` (140 simulated shifts) found 320
dock returns, all water-triggered, zero battery-triggered — every wet robot
carries a 90-min tank against a 180-240 min battery. If re-speccing
hardware, a bigger tank beats a bigger battery.

## 3. Charging is two-phase (CC/CV) — redeploy at 90%, not 100%
Real Li-ion charges fast to ~90% (CC) then tapers hard for the last 10%
(CV) — ~30 min / ~60 min of a 90-min full charge. `hal/base.py` models this
as a real piecewise curve, and dispatch redeploys robots at 90%
(`CHARGE_DISPATCH_TARGET_PCT`) instead of waiting for 100%, since the CV
tail is mostly wasted dock time. Measured: +24% mean fleet slack per shift,
no coverage cost. 90% was picked because it's the curve's own inflection
point, not tuned further to keep it a single, explainable constant.

## 4. Dock trips drive faster than zone-to-zone travel
Scrubbers have a "transport mode" (deck up, fast) distinct from "scrubbing
mode" (deck down, capped for cleaning quality). Dock round trips are pure
transport mode, so `TRAVEL_MINUTES_DOCK` = 5min / 1.5 ≈ 3 min, applied only
to zone↔dock legs. Ordinary zone-to-zone travel keeps the assignment's
given 5-minute flat cost. Small but free win: +3.8 min further slack,
stacked on #3.

## 5. Heuristic scheduler, not ILP/CP-SAT
8 robots × 8 zones is small enough for a solver to be optimal in
milliseconds, but a greedy priority-ordered heuristic
(`scheduler.generate_schedule`) is a 40-line function a reviewer can walk
through, and the scheduler only needs to prove a plan is *plausible* — the
Dispatcher (see #6) is the real source of truth for timing. Tradeoff:
leaves some value on the table; wouldn't scale past low-hundreds of zones.

## 6. Scheduler plans; Dispatcher decides when cycles actually happen
The scheduler produces a rough plan with a 20% buffer as a feasibility
check; it does not precompute exact break timing. The Dispatcher simulates
physics minute-by-minute and inserts water/battery breaks when telemetry
says so. This makes the system robust to real disruptions (escort delays,
dropped connections) that a fully precomputed schedule would be stale
against. Cost: the scheduler's 20% buffer is an approximation and can
occasionally accept a zone that later runs `PARTIAL`.

## 7. Shared per-zone progress pool for multi-robot zones
Two robots on one zone draw from one shared remaining-sqft counter, not two
independent copies — an earlier bug had each robot silently redo the whole
zone. Fixed via a dict keyed by zone id, shared across `RobotController`
instances.

## 8. FloorBot water: conservative trigger
FloorBot's 4-bucket reading ("low" ≈ 8-33% remaining) is coarse. Chosen
policy: treat "low" as the return trigger, not "empty" — costs some
cleaning time but avoids ever running the tank dry (a real damage risk, not
just a missed deadline).

## 9. FloorBot's bucket, sharpened by a usage-time model — not replaced
`hal/floorbot.py` tracks active cleaning minutes since last refill and
derives a continuous water-% estimate, but clamps it every reading to the
range the robot's own bucket still supports — fusion, not replacement, so
the model can never drift more than one bucket-width from ground truth. The
gap between the raw model and the clamped value (`water_model_bucket_drift_pct`)
is itself a leak/sensor-fault signal, feeding `Monitor`'s anomaly check.
Deliberately unchanged: the actual return-trigger (#8) still keys off the
raw bucket, since a fused estimate shouldn't make a safety-relevant
decision more aggressive.

## 10. Scripted vs. emergent disruptions
Two of five required disruptions emerge organically from simulated physics
(R-001 running its tank dry, R-008's routine cycles); the other three
(sensor fault, WebSocket drop, escort delay) are injected at scripted
sim-time by `scenario.py`, standing in for an external event feed. Handler
logic in `replanner.py` is generic (robot/zone/time, not demo-specific) and
is also exercised directly in tests, independent of the scripted timeline.

## 11. R-001 vs. R-006 to the garage (Z8)
The assignment brief conflicts with itself (bullet list says R-001, the
detailed timeline says R-006/FloorBot). Assumption: the more specific
timeline wins, so the scheduler prefers hard-scrub-capable robots for Z8,
which in practice assigns a FloorBot.

## 12. No LLM component
Every decision (scheduling, dispatch, anomaly detection, disruption
handling) is deterministic. None of the required decisions are language
tasks, and determinism matters for reproducible tests/demos. An LLM would
plausibly earn a place only in a presentation layer (free-text shift
summaries, escalation messages) — not implemented, out of scope.

## 13. No ML for scheduling
Fleet/zone count is too small to benefit from a learned model, and there's
no multi-shift training data. The one place ML is designed for but not
built is anomaly detection (#15).

## 14. Consumable profiling: trip-normalized rates, degradation & peer-outlier detection
`profile.py` cuts telemetry into active-use segments and reports a
normalized rate (% used per 60 active minutes) so trips of different
lengths are comparable. Two independent checks: **self-baseline**
(`flag_degradation`, this robot vs. its own early history — catches slow
drift like an aging battery) and **peer-outlier** (`flag_peer_outlier`,
this robot vs. same-OEM/model peers via z-score — catches a chronically
off unit). Validated with two distinct synthetic fault shapes in
`analysis/consumable_profile_demo.py`, each caught by only the intended
check. `Monitor` prefers a robot's own learned rate over the OEM spec once
available, feeding `minutes_remaining()` / ETA. Persisted as flat JSON
(`ProfileStore`), matching the same scoped-down persistence choice as #16.

## 15. Anomaly detection: threshold-based, ML-shaped data model
`Monitor.history` stores flat, timestamped, normalized telemetry — the
shape a future feature pipeline would want. Current checks are
threshold-based (bucket drift, degradation ratio, z-score); swapping in a
learned model would change function bodies, not the schema.

## 16. Persistence granularity: shift-boundary, not mid-tick
This is a nightly batch scheduler, so the realistic restart case is
between shifts, not mid-tick at 2 AM. State (fleet, zone outcomes,
consumables, anomalies, disruption log) is saved once per shift
(`persistence.save_shift_state`, reloadable via `main.py --load`).
Tradeoff: a mid-shift crash loses that night's progress; true
checkpointing wasn't justified by scope.

## 17. Robot simulator fidelity
Zone-to-zone travel is a flat cost (5 min, 2% battery) per spec, applied
once per transition, except dock-service legs (faster, see #4). Charging
follows the CC/CV curve (#3). Water refill is a flat 10-minute cycle
regardless of starting level (spec says "dump + refill," not proportional).
Security escort delay is `random.uniform(0,10)` min after 23:00, seeded for
reproducibility, with the Z5 disruption forcing 25 min for that one event.

## 18. Bug: first sterile zone of the night skipped sanitization
`last_classification` was seeded `None` with a guard that treated "no
previous zone" as "no transition," so a sterile-certified robot's very
first zone silently skipped its pre-cycle sanitization. Fixed by seeding
`last_classification = "Standard"` (dock is non-sterile) and dropping the
now-unneeded guard. Caught by re-running the system against the
assignment's own sample timeline line-by-line, not just checking the final
report.

## 19. Facility data verified against spec, not just asserted
Every zone's sqft, floor type, classification, window, day-restriction, and
WiFi flag is locked in by a dedicated test
(`TestFacilitySpecMatchesImage`). The weekly report's flat 12h "Allocated"
column is correct (the shift window is a fixed constant per the
assignment), but was hiding real day-to-day variation — a new **Zone-Window
Demand** column now surfaces that (41h Mon/Wed/Fri, 49h Tue/Sat, 37h
Thu/Sun), tested to confirm it actually varies while shift capacity stays
constant.

## 20. Intentionally not handled
No production UI (CLI + JSON only), no real OEM API integration (all three
simulated), not every edge case. Notably:
- Multi-night history / battery aging: addressed via #14's `ProfileStore`.
- Scheduler doesn't rebalance robot wear across nights yet, though
  `ProfileStore` now has the data a wear-balancing pass would need.
- Freight-elevator transit time (+3 min between floors) isn't modeled
  separately from flat travel cost — would require tracking which zones
  share a floor.

## 21. Battery return threshold: a tracked trip-cost estimate, not a flat constant
The battery return-to-dock trigger is not a fixed percentage. `wants_return_now()`
(`dispatcher.py`) compares live battery against
`battery_used_since_dock + BATTERY_RETURN_PCT` — a 10% safety floor plus a
2% headroom cushion, *plus* a running estimate of what the trip back would
actually cost. That estimate (`RobotController.battery_used_since_dock`) is
measured, not assumed: it accumulates `TRAVEL_BATTERY_PCT` on every travel
leg the robot takes since its last real dock-service stop, and resets to 0
on arrival at the dock. **Assumption:** a robot that has hopped between
several zones without returning to the dock needs roughly as much battery
to get back as it has spent getting that far out — a standard "distance
traveled ≈ distance to return" heuristic used when there's no real
facility path graph to compute an exact route cost from. In this
simulator's flat per-transition travel-cost model, the *actual* trip home
is always a single fixed-cost hop (`TRAVEL_BATTERY_PCT`, same as any other
transition — see #17), so this tracked estimate is deliberately more
conservative than what the physics alone requires for the common
single-hop case; it only earns its keep once a robot has made multiple
hops away from the dock in a row, where a flat constant would otherwise
under-reserve. Verified against the existing 140-shift study: no change
to the water-vs-battery split (#2, still 100% water-bound) or fleet-wide
schedule slack, since normal shifts rarely chain enough zone-to-zone hops
to make the estimate diverge meaningfully from the old flat 12%.

## 22. Precise-sensor water return floor: 1.5%, not 4% — and not 0%
`WATER_RETURN_PCT` (`dispatcher.py`) is the return trigger for robots with
a precise water sensor (AutoScrub). The first implementation copied the
same margin pattern as the battery floor and set this to 4.0%, but that
copy doesn't hold up: battery's floor needs a reserve for the trip back to
the dock (see #21) because battery keeps draining while EN_ROUTE; water
does not drain in transit at all (`PhysicalState.advance` only drains
water during `CLEANING`/`OFFLINE_MISSION`, never `EN_ROUTE`), so there is
no equivalent "reserve enough to make it home" need. **The only real
constraint is the simulator's 1-minute tick resolution** — `wants_return_now()`
is evaluated once per simulated minute, right after that minute's drain,
so the floor has to be at least one tick's worth of drain, or a robot
could be told to keep cleaning through a tick that would empty the tank
partway through it. All wet robots share a 1.5-hour tank (~1.11%/min
drain), so **1.5%** was chosen as roughly one tick of margin above zero
plus a small cushion for floating-point rounding — enough to guarantee
the tank is never asked to run a full tick on fumes, without holding back
meaningfully more than that. At the old 4.0% floor, robots were forfeiting
~3.6 minutes of legitimately usable cleaning time on every water-bound
stop for no corresponding safety benefit — measured against
`analysis/binding_constraint_study.py`'s 320 water-bound stops across 140
shifts, that's real reclaimed cleaning time, not a rounding error. FloorBot's
own return trigger is unaffected by this — it's still bucket-based (`#8`),
since its coarse sensor has no precise percentage to apply a tick-based
floor to in the first place.

## 23. Bug: escalated sterile-zone misses weren't actually distinguishable from routine misses
`replanner.handle_robot_failure()`'s decision text always claimed *"zones
marked MISSED_ESCALATED"* when R-003 fails with no sterile-certified
backup available — but the code underneath recorded plain
`ZoneStatus.MISSED`, the same status a zone gets for simply running out of
window time. `ZoneStatus` had no `MISSED_ESCALATED` value at all, so the
claim in the log text was aspirational, not real: a reader of the Zone
Outcomes table couldn't tell "no backup existed, a human had to decide"
apart from an ordinary miss without separately reading the disruption log.

**Fix:** added `ZoneStatus.MISSED_ESCALATED` (`models.py`) and switched
`handle_robot_failure`'s escalation branch to record it instead of plain
`MISSED`. `Monitor.shift_report()` counts it toward the same `missed`
total (it's still a coverage miss) but also tracks and surfaces a separate
`escalated` count — e.g. `2 missed (2 escalated to human)` — so the
distinction is visible in the summary line, not just buried in the
per-zone table or the disruption log. Regression test:
`tests/test_basic.py::test_robot_failure_marks_zones_missed_escalated_not_bare_missed`,
which explicitly asserts the at-risk sterile zones get the escalated
status and NOT plain `MISSED`.
