# Design Decisions, Assumptions & Tradeoffs

This is the separate spec doc the assignment asks for: "Make reasonable
assumptions and document them. Ambiguity handling is part of the
evaluation." Everything here is a deliberate choice, not an oversight --
each entry says what was chosen, why, and what the alternative would have
cost.

## 1. Objective function

**Chosen:** minimize risk-weighted incomplete coverage first, then minimize
robot-hours spent. Zones are prioritized (Sterile > High-traffic > Standard,
then earliest window close first); a second robot is only added to a zone
when a single robot would leave under 20% slack against its window --
real risk of not finishing, not just "could go faster."

**Why not maximize coverage, or minimize total time, or minimize water
stops?** Those are all real candidate objectives and the assignment
explicitly asks to justify the choice.
- *Maximize coverage* alone doesn't distinguish a missed supply closet from
  a missed sterile OR corridor -- patient-safety zones need to dominate the
  ranking, not just count equally.
- *Minimize total time* would encourage over-provisioning robots onto easy
  zones for a speed win while starving tight-window zones of capacity.
- *Minimize water stops* is a real secondary cost (see dual-constraint
  section below) but shouldn't outrank finishing an OR corridor.

Risk-weighted-coverage-first, cost-second is the one that matches how a
real facility would actually be judged (SLA breaches in sterile areas are
categorically worse than an extra 10-minute water stop).

## 2. Empirical validation: water, not battery, is the real binding constraint

Section 1's objective function claims water stops are the secondary cost
worth minimizing and battery isn't. That claim was checked, not assumed:
`analysis/binding_constraint_study.py` runs 140 simulated shifts (all 7
days of the week x 20 random seeds each, no injected disruptions, so the
measurement isn't confounded by scripted failures) and records, every time
a robot is forced to return to the dock, whether battery or water was the
reason.

**Result: 320 dock returns total across all 140 shifts. 320 were
water-bound. Zero were battery-bound.** Every wet robot that ever needed a
mid-shift break needed it for water, every single time.

| OEM | water-bound stops | battery-bound stops |
|---|---|---|
| AutoScrub (R-001/002/003) | 280 | 0 |
| FloorBot (R-006/008) | 40 | 0 |
| CleanPath (dry, no tank) | n/a | 0 |

This isn't a scheduling artifact -- it falls straight out of the OEM spec
sheet numbers in the Fleet Roster table. Every wet robot in this fleet
carries a **90-minute** water tank against a **180-240 minute** battery
(a 2-2.7x ratio). Any continuous clean run longer than 90 minutes hits
water first, unconditionally, regardless of scheduling quality -- there is
no assignment strategy that changes this, because the hardware was never
given enough water capacity to match its own battery life. A companion
run, `analysis/single_day_trace.py` (one full Tuesday, no disruptions),
shows the same pattern concretely: 3 total dock trips fleet-wide across
the whole shift, and every one of them logs `binding constraint: water`.

The same 140-shift run also found **0 partial and 0 missed zones** under
routine operation -- every miss shown elsewhere in this system (Z5 in the
Tuesday demo, the R-003 sterile zones) came from an *injected* disruption
(a forced 25-minute escort override, a hard robot failure), never from
ordinary battery/water/escort variance. That's a second data point for the
objective function: this facility's windows have enough built-in slack
that raw completion speed isn't the scarce resource in steady state --
water stops and disruption resilience are.

**Practical implication:** if this fleet were being re-speced rather than
just re-scheduled, the single highest-leverage hardware change would be a
bigger water tank on the AutoScrub/FloorBot units, not a bigger battery --
battery is already sized well past what the schedule ever asks of it.

## 3. Charging is not linear -- redeploy at 90%, not 100%

The assignment gives one number for charging: "90 minutes, 0% to 100%."
The first implementation took that literally -- a flat rate, 1.11%/min,
the whole way. That's not how Li-ion packs actually charge, and it was
worth fixing because the difference isn't cosmetic.

**Real lithium-ion charging is two-phase (CC/CV):** constant current (CC)
up to roughly 90%, fast, followed by a constant-voltage (CV) taper for the
last 10% that's deliberately slow -- the charger drops current as the
pack approaches full to avoid overvoltage, and that taper is *why* the
last 10% takes so long. Applied to the assignment's 90-minute total: the
0-90% CC phase takes about 1/3 of the total (**30 min**, ~3.0%/min), and
the 90-100% CV phase eats the other 2/3 (**60 min**, ~0.17%/min) --
roughly an 18x slowdown for the last stretch. This is implemented as a
genuine piecewise curve, not a lookup table: `hal/base.py`'s
`charge_pct_after()` / `charge_minutes_to_target()` integrate the CC and
CV segments correctly even when a single charging stop straddles the 90%
boundary.

**Dispatch policy changed to match:** robots now redeploy once they hit
`CHARGE_DISPATCH_TARGET_PCT = 90%` instead of waiting for 100%
(`dispatcher.py::_arrive_at_dock`). The cost is real but small -- 90% down
to the `BATTERY_RETURN_PCT = 12%` forced-return floor is still 78 usable
percentage points per cycle, a robot rarely burns that much in one zone
run. The benefit is not small: skipping the CV tail means skipping the
majority of what a full charge actually costs in time.

**Measured, not assumed** -- re-running the same two studies from #2 with
the new curve and policy:

| | old (linear, target 100%) | new (CC/CV curve, target 90%) |
|---|---|---|
| Mean slack (140-shift study) | 137.2 min | **170.8 min** (+33.6 min, +24%) |
| R-008 dock stop (Tue trace) | 30 min | **10 min** |
| R-001 dock stop (Tue trace) | 36 min | **10 min** |
| R-003 dock stop (Tue trace) | 49 min | **15 min** |
| Last zone finished (Tue trace) | 04:46 AM | **04:08 AM** (38 min earlier) |
| Water-vs-battery split, coverage | unchanged (100% water, 0% missed) | unchanged |

The water-vs-battery finding from #2 doesn't change -- water is still the
binding constraint on every single dock return, because this policy only
touches *how long* a charge-inclusive stop takes, not *whether* one
happens (that's still triggered by water). What changes is that every
stop is shorter, which is exactly the "room for interruptions/delays"
outcome intuited going in: ~34 extra minutes of fleet-wide slack per shift
is schedule margin that's now available to absorb a disruption instead of
being spent trickle-charging batteries nobody was about to run out of.

**Why 90% and not some other threshold, and why not adaptive?** 90% is
the natural boundary the CC/CV curve itself provides -- it's where the
charging rate cliffs, so it's the target that captures "all of the fast
phase, none of the slow one." A fancier policy could vary the target based
on how much schedule slack remains (top off further when there's nothing
better to do, stop earlier under time pressure); that's a reasonable
future refinement, deliberately not built here to keep the policy a
single named constant with an obvious justification rather than another
tunable heuristic layered on top of the scheduler's existing ones.

## 4. Scrubbers drive faster when they're not scrubbing

**Observation, not something built into the original model:** a
traction-drive auto-scrubber like the AS-900/AS-900H doesn't have one
driving speed. It has two distinct speed profiles, and which one applies
depends on whether the scrub deck and squeegee are down:

- **Transport mode** (deck/squeegee raised): high-speed driving, used to
  cross already-clean areas, go up ramps, or return to the dock.
- **Scrubbing mode** (deck down, actively cleaning): speed is capped on
  purpose, so the brushes get enough dwell time to agitate dirt and the
  squeegee has time to fully dry the floor behind it. The cap is a
  cleaning-quality constraint, not a drivetrain limit.

This system already had exactly this two-mode split, just not in name --
`CLEAN` phase throughput is governed by `coverage_ft2_hr` (the
scrubbing-mode cap), and `TRAVEL` phase is a separate cost entirely. What
it was missing: **all travel was charged at one flat rate**, whether it
was a normal zone-to-zone repositioning or a dock-service round trip, even
though a dock round trip is the cleanest possible case of pure transport
mode -- deck fully raised and stowed, a known repeated route, nothing to
clean along the way.

**Change made:** `TRAVEL_MINUTES_DOCK` now applies specifically to the
zone->dock and dock->zone legs of a service trip
(`dispatcher.py::_begin_dock_return` and the DOCK_SERVICE resume path).
The baseline 5-minute figure is left untouched for ordinary zone-to-zone
dispatch, since that's an explicit assignment-given constant ("5
minutes... per transition") and there's no comparable evidence for
treating that specific leg as faster -- extending the speed differential
past the case it's actually justified for would be overreaching.

**The number: a 50% speed increase, not a 50% time cut.** Speed and time
are inverses at constant distance -- a 1.5x speed multiplier means
`time / 1.5`, not `time * 0.5`:

```
TRAVEL_MINUTES_DOCK = TRAVEL_MINUTES / 1.5 = 5 / 1.5 = 3.33... min
```

Rounded to **3 minutes** to match this simulator's discrete 1-minute tick
resolution (a fractional target would otherwise silently round *up* to
the next whole tick under a naive countdown-by-1 loop -- 3.33 would
actually cost 4 simulated minutes, not 3.33, an artifact worth catching
rather than shipping quietly). No AS-900-specific transport-vs-scrubbing
speed ratio is vendor-published; 50% is a stated assumption pinned to the
general behavior of this machine class, not a spec value, and is called
out as such rather than presented as more precise than it is. Battery
cost per transition (`TRAVEL_BATTERY_PCT`) is left unchanged: higher
speed plausibly draws more current over a similar distance, so there's no
basis for assuming transport mode is also more energy-efficient, only
faster.

**Measured impact, stacked on top of #3's charging change:** re-running
the 140-shift study with both changes in place, mean fleet-wide slack
rises again, from 170.8 min (charging change alone) to **174.6 min**
(+3.8 min further, +27.3% total vs. the original 137.2-min baseline
before either change). Smaller than the charging curve's effect -- a dock
round trip is a few minutes either way, a CV taper was tens of minutes --
but it's the same direction and it's free: no coverage or safety margin
given up for it (0 partial/missed zones across all 140 shifts, unchanged).

**Day-by-day operational tolerance, both changes combined:**
`analysis/daily_tolerance_table.py` simulates consecutive calendar days
(cycling real weekdays, distinct random seed per day, no injected
disruptions) and reports, per day, how long the facility actually took to
clean vs. the fixed 720-minute (12h) allocated shift window -- the delta
is the schedule margin available that day for an unplanned delay. Across
28 simulated days (4 full weeks, every weekday's zone mix repeated 4x):

| Metric | Value |
|---|---|
| Days with full coverage | 28/28 |
| Mean time to clean facility | 547.4 min (9.12 h) |
| Allocated capacity (fixed) | 720 min (12.00 h) |
| **Mean tolerance/delta** | **172.6 min (2.88 h)** |
| Tightest day observed | 166.0 min (2.77 h) |
| Most slack observed | 181.0 min (3.02 h) |
| stdev | 4.6 min |

That ~2.8-3.0 hour band, consistent across every day type, is the number
to actually plan an SLA or a maintenance/escalation window around -- not
a best-case number from a lucky night, a worst-case number that scares
everyone, or a scripted-disruption number that isn't representative of a
normal shift. Run `python -m analysis.daily_tolerance_table [n_days]` to
regenerate for a different sample size or inspect the day-by-day rows.

## 5. Heuristic scheduler, not an ILP/CP-SAT solver

8 robots x 8 zones is small enough that a real constraint solver (OR-Tools
CP-SAT, e.g.) would find a provably-optimal assignment in milliseconds, and
in a production system that's the right call, especially once you add
soft preferences (minimize robot-hours, balance wear across the fleet).

**Chosen instead: a greedy, priority-ordered heuristic.** Reasons:
- It's the difference between a 40-line, walk-through-able function
  (`scheduler.generate_schedule`) and a solver invocation whose behavior
  requires understanding a constraint model to reason about. Per the
  rubric, "a valid heuristic is fine" and the evaluation weighs systems
  thinking over polish -- a heuristic I can fully explain in the 30-minute
  walkthrough is worth more here than a solver I'd have to hand-wave past.
- The **Dispatcher is the actual source of truth for water/battery timing**
  (see #6) -- the scheduler only needs to prove a plan is *plausible*, not
  compute exact break timing, which shrinks the problem a solver would need
  to solve anyway.
- Tradeoff accepted: the heuristic can leave value on the table (e.g. it
  won't discover a globally-better swap two hops away). For 8 zones this
  is a rounding error; it would not scale to hundreds of zones without a
  real solver.

## 6. The scheduler plans; the Dispatcher decides when cycles actually happen

The scheduler produces a *plan* (who cleans what, roughly when, with a 20%
time buffer for cycle overhead as a feasibility check). It **deliberately
does not** try to precompute the exact minute a robot will need a water or
battery break. That job belongs to the Dispatcher, which simulates real
physics minute-by-minute and inserts a break exactly when the telemetry
says the robot needs one.

**Why split it this way instead of a fully precomputed schedule?** Because
precomputing exact break timing requires assuming nothing goes wrong
(no escort delay, no WebSocket drop stealing a few minutes, no ad-hoc
diversion) -- a plan built on that assumption is stale the moment reality
diverges, which is most nights. Organic, telemetry-driven breaks are
inherently robust to exactly the disruptions this system needs to handle.
The cost: the scheduler's window-fit check is an approximation (a flat 20%
buffer), so it will occasionally accept a zone that later needs a second,
unplanned-for cycle and runs past its window -- that shows up honestly as
`PARTIAL` in the shift report rather than being hidden.

## 7. Shared per-zone progress pool for multi-robot zones

When two robots are assigned to the same zone (e.g. a large zone under
time pressure), they draw down **one shared remaining-sqft counter**, not
two independent ones. The first implementation gave each robot its own
copy of the zone's full square footage, which meant two robots "finishing"
a zone independently -- i.e. each doing the *entire* zone redundantly
instead of splitting it, which silently defeated the entire "add a second
robot to cut completion time" mechanism. Caught by inspecting the shift
report (a zone with two robots assigned took exactly as long as one would
have) and fixed by moving remaining-sqft into a dict shared across
`RobotController` instances, keyed by zone id. This is the kind of bug
that a solver would never introduce (it reasons about the whole assignment
at once) but a greedy per-robot heuristic can, and is worth naming
explicitly rather than papering over.

## 8. FloorBot water uncertainty: conservative, not aggressive

FloorBot's coarse 4-bucket water reporting (`high/med/low/empty`) means a
"low" reading covers a wide true range (roughly 8-33% of tank capacity,
i.e. anywhere from ~7 to ~30 minutes remaining). Two policies are possible:

- **Aggressive:** trust the estimate (bucket midpoint, or the fused
  usage-time estimate from #9), keep cleaning until "empty." Maximizes
  cleaning time per water cycle, risks running the tank dry mid-zone (an
  actual failure mode, not just a missed deadline -- a dry scrubber can
  damage the floor or itself).
- **Conservative (chosen):** treat "low" as the trigger to return, not
  "empty." Costs some cleaning time (the robot could sometimes have safely
  continued), buys certainty against ever running dry.

This system takes the conservative branch (`dispatcher.wants_return_now`),
and the same policy is what drives the R-008 water-anomaly response in
`replanner.handle_water_anomaly`: an implausible bucket reading gets a
precautionary dock visit immediately rather than waiting for "empty" to
confirm a leak. The assignment's own "What Will Impress Us" section calls
this exact tradeoff out, so the choice and its cost are made explicit here
rather than left implicit in a threshold constant.

## 9. FloorBot's coarse reading, sharpened with a usage-time model

**Question worth asking of any coarse sensor:** FloorBot only reports a
4-value bucket, but AutoScrub reports a precise water percentage from the
same underlying physical quantity (a tank draining at a known rate while
cleaning). Since we already know the OEM's rated `water_hours` and can
track exactly how long a robot has been actively cleaning since its last
confirmed refill -- the same continuous signal AutoScrub gets for free
from a precise sensor -- can that usage-time reasoning sharpen FloorBot's
coarse signal instead of just reporting a static bucket midpoint?

**Yes, with one important caveat: it's fusion, not replacement.** A pure
time-based model would drift -- floor texture, water pressure setting, a
partially-clogged squeegee, or a real leak would all make actual
consumption diverge from the nominal rate, and a model with no ground
truth has no way to notice. So `hal/floorbot.py` now tracks
`_active_minutes_since_refill` and derives a continuous estimate
(`usage_model_pct = 100 * (1 - active_minutes / water_hours*60)`), but
**clamps it every reading to the range the robot's own coarse bucket
still supports** (`bucket_pct_bounds()`). Between bucket transitions the
reported `water_pct` now moves smoothly with actual usage instead of
jumping only when the bucket itself changes; at each bucket reading, it's
snapped back to what the real (if coarse) sensor confirms, so the model
can never wander further from reality than one bucket-width.

**The gap between the two signals is itself useful.** `water_model_bucket_drift_pct`
(how far the raw usage-time model sat from the bucket-clamped value before
clamping) is exposed in `meta` and checked in
`Monitor._check_water_anomaly`: a large, persistent gap means usage-time
alone no longer explains what the sensor is reporting -- exactly the
leak/sensor-fault signature the R-008 scripted disruption represents, now
detectable from ordinary telemetry rather than only when scripted. Wiring
`replanner.handle_water_anomaly` to run its forced reading through
`monitor.ingest()` (not just its own hand-authored `flag_anomaly` call)
confirms this: **both the scripted flag and the automatic
`water_model_bucket_divergence` flag fire on the same event**, which is
the useful validation -- the general-purpose detector independently
catches the same anomaly the disruption script asserts, so the mechanism
isn't just tuned to a demo, it works against a different signal entirely
(bucket vs. drift) hitting the same conclusion.

**Deliberately NOT changed: the return-trigger policy.**
`dispatcher.wants_return_now()` still keys off the raw bucket
(`bucket in ("low", "empty")`), not the fused estimate, and that's on
purpose -- #8's conservative policy exists specifically because a single
coarse reading can't be trusted for a safety-relevant decision (running
the tank dry). The fused estimate makes the *dashboard and anomaly
detection* smarter; it does not make the *dispatch trigger* more
aggressive. Those are different consumers of the same signal with
different risk tolerances, and conflating them would quietly undo #8's
whole rationale.

**A genuine edge case caught during implementation, worth naming:**
resetting the usage clock on "status just left WATER_CYCLE/DOCK_SERVICE"
sounds right but isn't -- the true tank level rises continuously
*during* a refill while the bucket only updates once it crosses each
threshold, so a status-transition-based reset produces a real but
spurious divergence flag on every single dock stop (caught by seeing it
fire 7 minutes in a row in the shift log). Fixed by tying the reset to
the physical tank state directly (`water_pct >= 99.5`) instead of a
status-transition heuristic, and by excluding `WATER_CYCLE`/
`DOCK_SERVICE` from the drift check entirely in `Monitor` -- mid-refill
telemetry is a transient by construction, not a signal to alarm on.

## 10. Scripted vs. emergent disruptions in the demo

Two of the five required disruptions emerge **organically** from the
simulated physics with zero scripting: R-001 running its water tank dry
mid-Z1 (the tank math alone produces this, matching the assignment's own
"R-001 hits water empty at ~10:30 PM" narrative beat almost exactly), and
R-008's routine charge/water cycles during its two garage runs.

The other three (R-003 sensor fault, R-005 WebSocket drop, the escort
delay) plus the ad-hoc request are **injected at a scripted sim-time** by
`scenario.py`, the way a live event feed (MQTT fault alert, security
system, a phone call from the facility manager) would actually deliver
them to a real orchestrator -- these aren't things physics alone would
ever produce in a simulator; they need an external trigger. The handler
functions in `replanner.py` are written generically (they take a
robot/zone/time, not "the demo's specific situation") and are exercised
directly in `tests/test_basic.py::TestReplanner` independent of the
scripted timeline, including a path the demo timeline doesn't hit (the
WebSocket drop that *never* reconnects and must escalate).

One consequence worth flagging: the assignment's sample timeline assumes
every robot is still mid-clean at 2:20 AM when the WS-drop fires. This
system's zones mostly finish well before midnight (8 robots easily cover
7 active zones on a Tuesday), so a hardcoded 2:20 AM would land on an
*idle* CleanPath robot -- not wrong, just a weaker demo. `scenario.py`
instead fires the WS-drop during whichever CleanPath robot's actual
cleaning window it falls in, which is the more honest way to exercise the
same mechanism.

## 11. R-001 vs. R-006 to the offline garage (Z8)

The assignment brief is internally inconsistent here: Section 3's bullet
list says *"R-001 is dispatched to Z8,"* but the detailed sample-timeline
(steps 4 and 7) says *"R-006 (FloorBot) dispatched to Z8 ... R-006 returns
from garage, reconnects."* R-001 is an AutoScrub AS-900 (a scrubber, but
one already busy on Z1 in the same timeline); R-006 is a FloorBot FB-200.
**Assumption made: the detailed timeline is authoritative** (it's more
specific and names the OEM), so this system's scheduler prefers hard-scrub
capable robots for the concrete garage floor, which in practice assigns a
FloorBot. This ambiguity is called out here rather than silently resolved.

## 12. No LLM component

Per the assignment: *"If you use LLMs for any component, explain what they
handle vs. deterministic code."* This system uses **no LLM anywhere** --
every decision (scheduling, dispatch, anomaly detection, disruption
handling) is deterministic threshold/rule-based code. Reasons:
- Determinism matters more here than for a typical LLM use case: the same
  seed should produce the same shift, every time, for testing and for a
  walkthrough demo that doesn't risk an off-script model response mid-call.
- None of the required decisions are actually language tasks. "Is the
  bucket reading anomalous," "does this robot fit the window," "should we
  escalate" are all well-specified numeric/rule questions, not
  interpretation problems.
- Where an LLM *would* plausibly earn its place in a real version of this
  system: turning `monitor.shift_report()`'s structured data into a
  free-text summary for a facility manager who doesn't want a table, or
  drafting the human-escalation message for the R-003-style failure case.
  Both are presentation-layer tasks downstream of a decision the
  deterministic code already made -- exactly the "LLM handles
  presentation, code handles the decision" split the assignment is
  checking for. Not implemented here because it would be UI polish on top
  of an otherwise-untouched decision layer, and the assignment says
  explicitly that production UI isn't part of what's being evaluated.

## 13. ML for scheduling optimization

Not used. The fleet/zone count (8 and 8) doesn't benefit from a learned
model over a simple heuristic or solver, and there's no training data (a
single simulated night) to learn from responsibly. The one place ML is
explicitly designed for but not implemented is anomaly detection (see #15).

## 14. Consumable profiling: trip-normalized rates, degradation & peer-outlier detection

This closes two gaps flagged in an earlier review: **"estimated time to
next stop"** wasn't surfaced anywhere despite the dispatcher already
computing the underlying rate internally, and **battery aging** was
explicitly documented as unimplemented, blocked on not having a
multi-shift consumption history to compare against. Both come from the
same underlying idea: track each robot's *own observed* consumption
instead of only trusting the OEM's *rated* spec number, and persist it
across shifts so "is this getting worse" is answerable at all.

**The normalization problem, and why it's the actual hard part.** Raw
per-tick telemetry isn't directly comparable across trips: a 45-minute
trip and a 90-minute trip used different total resource amounts for
reasons that have nothing to do with the robot's health, and wall-clock
trip length is inflated by travel/escort/dock time that consumes nothing
at all. `profile.py::extract_trip_segments` cuts each robot's telemetry
into continuous **active-use segments** -- exactly the stretches where
`status` is `CLEANING` or `OFFLINE_MISSION`, i.e. actually consuming a
resource -- and reports a normalized rate: **percent used per 60 active
minutes**. "60 minutes of vacuuming today vs. 60 minutes of vacuuming
yesterday," the way the assignment framed it, is precisely
`battery_rate_per_60min` / `water_rate_per_60min` on a `TripSegment`.
Verified against a real simulated shift: the normalized water rate for a
1.5h-tank scrubber comes out to ~66.6-66.7%/60min regardless of whether
the underlying trip was 65 or 87 active minutes long -- matching the
nominal 100/1.5=66.67 spec rate almost exactly, which is exactly the
"apples to apples" property normalization is supposed to buy.

**Two comparisons, because they catch two different failure modes:**
- **Self-baseline (`flag_degradation`):** this robot's recent trips vs.
  its OWN earliest recorded trips. Catches a slow drift over many
  shifts -- an aging battery, a slowly-worsening leak -- using nothing
  but the robot's own history, no peer or spec value needed.
- **Peer-outlier (`flag_peer_outlier`):** this robot vs. the pooled rate
  of every OTHER robot sharing the same OEM **and** model (z-score
  against peer variance, falling back to a flat ratio when the peer
  group has ~zero variance to compute a z-score against). Catches a unit
  that's *chronically* different from its siblings -- manufacturing
  variance, a persistent config difference -- that a self-baseline check
  would never see, because it never drifts, it's just always been that
  way.

**These are validated to actually be different, not just described
that way.** `analysis/consumable_profile_demo.py` runs 20 shifts with two
distinct synthetic fault shapes injected: R-001 starts healthy and its
`battery_hours` ramps down over the second half (an aging signature) --
caught by `flag_degradation`, correctly NOT caught by `flag_peer_outlier`
(no same-model peer with trip data in this fleet). R-007 is ~35% worse
than spec from shift 1, constant the whole time (a chronic/manufacturing
signature) -- caught by `flag_peer_outlier` against its healthy sibling
R-005 (both CleanPath CP-X1), correctly NOT caught by `flag_degradation`
(there's no drift to detect; it was always this way). A control robot,
R-005 itself, is never touched and correctly triggers neither check. Four
independent assertions, not one lucky flag firing.

**Honesty about what's synthetic vs. real:** this simulator's base
physics use a constant nominal drain rate straight from the OEM spec
(see #17) and do not model hardware aging on their own -- there's no
mechanism for a battery to spontaneously get worse across simulated
nights. The demo script's drift is injected on purpose, the same way the
R-008 water-anomaly disruption is scripted elsewhere in this system: it
proves the general-purpose detection mechanism is correct against a KNOWN
trend, not that real degradation happens to occur in an ordinary run.

**Tier 1 and Tier 2 meet at the ETA.** `Monitor._resource_rate` prefers a
robot's own `learned_rate` (mean of its most recent trips, once any exist)
over the OEM's nominal spec rate; `dashboard()` shows which source is
active (`[learned]` vs `[spec]`) so a reader can tell whether a number
reflects real observed behavior or is still falling back to the nameplate
figure. `minutes_remaining()` explicitly estimates **active** minutes
remaining assuming uninterrupted continued cleaning from now -- it does
not attempt to forecast future travel/escort gaps between now and then,
which aren't knowable in advance and would just be a second, unrelated
estimate bolted onto this one.

**A real bug found while wiring this up:** offline-mission robots
(`OFFLINE_MISSION` status, e.g. FloorBot on Z8 garage duty) had their
telemetry buffered but never actually fed into `Monitor` -- the
"reconciling N buffered telemetry samples" log line was true in that the
count was right, but nothing was ever ingested, so an offline-mission
robot could never accumulate a trip profile at all, silently. Fixed in
`dispatcher.py::_end_offline_mission` to actually replay the buffered
samples into `monitor.ingest()`, in original chronological order, on
reconnect -- each anomaly this surfaces is correctly timestamped to when
it actually happened, even though it's only detected once the robot is
back on the network, which is the honest story for a robot with no live
link. Regression test:
`tests/test_basic.py::test_offline_mission_telemetry_is_reconciled_not_lost`.

**Persistence: flat JSON, not a real database.** `ProfileStore.save()`/
`.load()` follow the same pattern as `persistence.py` -- a deliberately
scoped-down choice (see #16 for the same tradeoff applied to shift state)
rather than standing up SQLite or a real time-series store for an
exercise of this size. The data model (`TripSegment` as a flat, normalized
record) is exactly what a real database schema would look like; swapping
the JSON file for an actual table is a persistence-layer change, not a
data-model change.

## 15. Anomaly detection: threshold-based now, ML-shaped data model

Per the rubric: *"Threshold-based is fine, but design the telemetry data
model so ML-based anomaly detection could be added later."* `Monitor`
keeps every telemetry sample as a flat, timestamped, normalized record
(`monitor.history[robot_id]`, a list of the common `Telemetry` schema) --
exactly the shape a feature pipeline would want for training a per-robot
drain-rate or leak classifier. The threshold checks in
`Monitor._check_water_anomaly` are a placeholder for what would become a
model call; swapping them out would not require touching the schema, only
the function body. `profile.py`'s degradation/peer-outlier checks (#14)
are a second generation of the same idea, still explicitly threshold-based
(a ratio and a z-score, not a learned model) but a step closer -- they
already operate on the kind of engineered feature (a normalized per-trip
rate) a real model would actually want as input, rather than raw
per-tick telemetry.

## 16. Persistence granularity: shift-boundary, not mid-tick

Per the rubric: *"State must persist across restarts."* This orchestrator
is a nightly batch scheduler -- the realistic restart scenario is "the
process restarts between shifts" (deploy, crash-and-restart before the
next shift starts), not "the process restarts mid-tick at 2 AM." Persisted
at that granularity: fleet state, zone outcomes, consumable stats,
anomalies, the disruption log, and a tail of telemetry history, written to
JSON at the end of a shift (`persistence.save_shift_state`) and reloadable
without re-simulating (`main.py --load`).

**Tradeoff:** a crash at 2 AM mid-shift would lose that night's progress
and need a re-run from the schedule, not a resume from tick 300. Extending
this to true mid-shift checkpointing would mean calling
`save_shift_state` every N ticks instead of once at 07:00 -- the same
schema, just written more often -- and was left out because the added
complexity (checkpoint cadence, partial-shift resume logic in the
Dispatcher) wasn't justified by the assignment's 6-8 hour scope for a
failure mode (mid-shift process crash) that's a small fraction of the
realistic restart cases.

## 17. Robot simulator fidelity

Travel between zones is modeled as a flat cost (5 min, 2% battery) per the
spec, applied once at the start of a transition rather than drained
per-minute, because the assignment specifies it as a flat transition cost,
not a rate -- except for dock-service legs specifically, which run faster
(transport mode; see #4). Charging follows a two-phase CC/CV curve rather
than a flat rate, and the dock-service policy targets 90% rather than
100% -- see #3 for the full rationale and measured impact. Water refill is
a fixed 10-minute cycle regardless of starting level (per spec -- "dump +
refill," not proportional to how empty the tank was; unlike charging, the
assignment doesn't suggest water refill has a comparable slow-taper
physical mechanism, so the refill duration itself stays flat-rate -- what
FloorBot's *reported percentage* means between refills is a separate
question, addressed in #9). Security escort delay is modeled as
`random.uniform(0, 10)` minutes for any zone entry after 23:00, seeded for
reproducibility, with the Z5 disruption overriding it to a forced 25
minutes for that one event.

## 18. Bug found by re-checking the system against the assignment's own sample timeline

Directly re-running this system against the assignment's "Sample
Simulation: One Night Shift" narrative (rather than just checking the
final shift report) surfaced a real bug: step 6 says *"11:00 PM -- R-003
begins sterile zones. Sanitization cycle before Z7"* -- but the very
first sterile zone of the night was NOT triggering sanitization.

**Root cause:** `RobotController.last_classification` was seeded as
`None`, and `needs_sanitization()` had an explicit
`and self.last_classification is not None` guard -- meaning "no previous
zone recorded yet" was being treated as "no transition happened," so the
very first zone of a sterile-certified robot's night silently skipped the
sanitization cycle no matter what it was. Every *subsequent* transition
worked correctly; only the first one of the shift was wrong, which is
exactly the kind of off-by-one-at-the-boundary bug that's easy to miss
because most of the system's own tests and the shift report don't
distinguish "sanitized before the 1st sterile zone" from "sanitized
before the 2nd."

**Why it's wrong, not just incomplete:** the dock is a non-sterile area
(a maintenance/charging area, not a patient-care zone) -- so a robot's
first zone of the night genuinely is a non-sterile-to-sterile transition
if that first zone happens to be sterile, not an undefined edge case that
should be skipped.

**Fix:** seed `last_classification` as `"Standard"` (dock is non-sterile)
instead of `None`, and drop the now-unnecessary `is not None` guard. One
line changed in the seed, one line simplified in the check. Verified with
a direct trace re-run (`R-003: sanitization cycle before entering Z7`
now appears at 23:00, matching the narrative's 11:00 PM beat) and a new
regression test, `tests/test_basic.py::TestSanitization`.

**Why this is worth naming explicitly:** it's a concrete answer to "would
this actually work against the sample simulation" that isn't just "yes" --
the honest answer is "yes, and re-checking it against the assignment's
own narrative line-by-line (not just trusting the final report looked
right) is what caught a real bug," which is a more useful thing to be
able to say in a walkthrough than a system that's never been checked that
closely.

## 19. Verifying the facility data actually matches the spec, not just asserting it does

Raised as a direct question: does the simulation's zone/window/day data
really match the given facility spec, and why does the weekly reports'
"Allocated" column show a flat 12 hours every single day regardless of
which zones are scheduled?

**The zone data was re-verified field by field, fresh, not just
re-asserted.** Every zone's sqft, floor type, classification, window
start/end, day-restriction, and WiFi flag is now locked in by
`tests/test_basic.py::TestFacilitySpecMatchesImage`
(`test_every_zone_field_matches_the_spec_image_exactly`), so a future
change to `facility.py` that silently drifts from the given spec fails a
test instead of going unnoticed. Companion tests confirm Z4 runs only
Mon/Wed/Fri, Z8 only Tue/Sat, and every other zone runs all 7 days --
exactly the pattern in the facility notes.

**The flat 12-hour "Allocated" figure is correct, not a bug -- but it was
hiding real day-to-day variation that's worth surfacing.** The
assignment defines the shift itself as a fixed operating window --
*"Simulate one night shift (7:00 PM - 7:00 AM)"* -- a given constant
independent of which zones happen to be active that night, so a report
showing 12h every day is accurately reflecting that constant, not a
scheduling artifact. What genuinely *does* vary by day is which zones
are active and how many window-hours they collectively represent, and
the reports weren't surfacing that number anywhere, which is exactly
what made the flat 12h look suspicious even though it was right.
`analysis/facility_week_report.py`'s weekly summary now has a second,
clearly-labeled column, **Zone-Window Demand** (sum of every scheduled
zone's own window duration that night), which does vary as expected:
41h on Mon/Wed/Fri (Z4 active), 49h on Tue/Sat (Z8's 12h "anytime"
window active), 37h on Thu/Sun (neither). Locked in by
`test_zone_window_demand_varies_by_day_unlike_shift_capacity` and
`test_shift_capacity_is_the_fixed_12h_night_shift_from_the_brief` --
two tests that check the DIFFERENCE in behavior between the constant
(shift capacity) and the variable (zone-window demand) explicitly,
rather than just checking one number in isolation.

## 20. What's intentionally not handled

Per the rubric's own "What We Don't Care About": no production UI (this is
a CLI + JSON), no real OEM API integration (all three are simulated), and
not every conceivable edge case. Specific things noted rather than
silently ignored:
- ~~Multi-night history (battery aging detection) isn't meaningfully
  testable from a single simulated shift~~ -- **addressed**: see #14,
  `ProfileStore` persists trip history across shifts specifically to make
  this answerable, with a real (if threshold-based, not ML) degradation
  detector, tested against synthetic fault injection since the base
  physics don't model aging on their own.
- The scheduler doesn't rebalance robot wear (total hours/cycles) across
  nights -- `ProfileStore` now has the underlying data (`n_trips`,
  cumulative usage per robot) that a wear-balancing pass would need, but
  the scheduler itself doesn't read it or factor it into assignment yet.
- Freight-elevator transit time (+3 min between floors) is called out in
  the facility notes but not modeled as a distinct cost from the flat
  5-minute travel transition; folding it in would mean tracking which
  zones share a floor, which the facility model doesn't currently encode.
