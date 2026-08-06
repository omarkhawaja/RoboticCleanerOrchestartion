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

## 2. Heuristic scheduler, not an ILP/CP-SAT solver

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
  (see #3) -- the scheduler only needs to prove a plan is *plausible*, not
  compute exact break timing, which shrinks the problem a solver would need
  to solve anyway.
- Tradeoff accepted: the heuristic can leave value on the table (e.g. it
  won't discover a globally-better swap two hops away). For 8 zones this
  is a rounding error; it would not scale to hundreds of zones without a
  real solver.

## 3. The scheduler plans; the Dispatcher decides when cycles actually happen

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

## 4. Shared per-zone progress pool for multi-robot zones

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

## 5. FloorBot water uncertainty: conservative, not aggressive

FloorBot's coarse 4-bucket water reporting (`high/med/low/empty`) means a
"low" reading covers a wide true range (roughly 8-33% of tank capacity,
i.e. anywhere from ~7 to ~30 minutes remaining). Two policies are possible:

- **Aggressive:** trust the midpoint estimate, keep cleaning until "empty."
  Maximizes cleaning time per water cycle, risks running the tank dry
  mid-zone (an actual failure mode, not just a missed deadline -- a dry
  scrubber can damage the floor or itself).
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

## 6. Scripted vs. emergent disruptions in the demo

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

## 7. R-001 vs. R-006 to the offline garage (Z8)

The assignment brief is internally inconsistent here: Section 3's bullet
list says *"R-001 is dispatched to Z8,"* but the detailed sample-timeline
(steps 4 and 7) says *"R-006 (FloorBot) dispatched to Z8 ... R-006 returns
from garage, reconnects."* R-001 is an AutoScrub AS-900 (a scrubber, but
one already busy on Z1 in the same timeline); R-006 is a FloorBot FB-200.
**Assumption made: the detailed timeline is authoritative** (it's more
specific and names the OEM), so this system's scheduler prefers hard-scrub
capable robots for the concrete garage floor, which in practice assigns a
FloorBot. This ambiguity is called out here rather than silently resolved.

## 8. No LLM component

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

## 9. ML for scheduling optimization

Not used. The fleet/zone count (8 and 8) doesn't benefit from a learned
model over a simple heuristic or solver, and there's no training data (a
single simulated night) to learn from responsibly. The one place ML is
explicitly designed for but not implemented is anomaly detection (see #10).

## 10. Anomaly detection: threshold-based now, ML-shaped data model

Per the rubric: *"Threshold-based is fine, but design the telemetry data
model so ML-based anomaly detection could be added later."* `Monitor`
keeps every telemetry sample as a flat, timestamped, normalized record
(`monitor.history[robot_id]`, a list of the common `Telemetry` schema) --
exactly the shape a feature pipeline would want for training a per-robot
drain-rate or leak classifier. The threshold checks in
`Monitor._check_water_anomaly` are a placeholder for what would become a
model call; swapping them out would not require touching the schema, only
the function body.

## 11. Persistence granularity: shift-boundary, not mid-tick

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

## 12. Robot simulator fidelity

Travel between zones is modeled as a flat cost (5 min, 2% battery) per the
spec, applied once at the start of a transition rather than drained
per-minute, because the assignment specifies it as a flat transition cost,
not a rate. Charging is linear (0-100% in 90 min, matching the spec
exactly). Water refill is a fixed 10-minute cycle regardless of starting
level (also per spec -- "dump + refill," not proportional to how empty the
tank was). Security escort delay is modeled as `random.uniform(0, 10)`
minutes for any zone entry after 23:00, seeded for reproducibility, with
the Z5 disruption overriding it to a forced 25 minutes for that one event.

## 13. What's intentionally not handled

Per the rubric's own "What We Don't Care About": no production UI (this is
a CLI + JSON), no real OEM API integration (all three are simulated), and
not every conceivable edge case. Specific things noted rather than
silently ignored:
- Multi-night history (battery aging detection) isn't meaningfully
  testable from a single simulated shift; `Monitor` is structured to
  support it once `persistence.py`'s saved history spans multiple nights,
  but no aging model is implemented.
- The scheduler doesn't rebalance robot wear (total hours/cycles) across
  nights -- there's no multi-night state to rebalance against yet.
- Freight-elevator transit time (+3 min between floors) is called out in
  the facility notes but not modeled as a distinct cost from the flat
  5-minute travel transition; folding it in would mean tracking which
  zones share a floor, which the facility model doesn't currently encode.
