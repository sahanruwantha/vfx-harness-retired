# Shot state at 2026-09-05, main `261c5fa`

Written to disk because the sessions driving the three shots ended and their live context
could not be handed over. Everything below is recoverable from the repository and the shot
artifacts; nothing here depends on a conversation.

## What each shot was mid-test on

### room_1046_opening — the only shot that can test HIR-0204, and it confirmed it

Layer 1 **sealed** in 99.7s at the point that previously raised, with zero "missing
deterministic evidence" and zero finalization conflicts. HIR-0204 is confirmed on the shot
that found it.

Layer 2 holds `facade_grid` in `hypothesis_falsified`, finding
`hf-2471bd031b5215b66ba1`, `fault_owner_units ["camera_path"]` — sealed in layer 1.
`ground_island` is `blocked`, `corner_massing` passed.

**The finding is correct and the animation is what is wrong.** The sealed layer-1 camera is
byte-identical at both layer-2 judge frames: `location[0]` and `location[1]` LINEAR between
equal values from f1 to f113, both rotation channels flat, `data.lens` unkeyed before f113.
Judge frames f39 (1.56s) and f76 (3.04s) sit inside that hold. The brief asks for
"1.5-4.5 seconds — Approach: the camera advances", and the first non-identical key is at
f113 = 4.52s, exactly where the brief's acceleration is specified to begin. So layer 1 built
the post-4.5s ramp and omitted the approach.

`facade-grid-parallax-f39-f76` is `parallax_displacement_profile`, `op min`, `lo 1.02`,
`owner_layer 2`. A layer-1 amendment **cannot** touch it — `validate.py:355` refuses a row
whose `owner_layer` is not the candidate's layer — so the padding path is closed by
construction. The only resolutions are authoring the missing camera motion, or a recorded
vocabulary gap plus an explicit decision authored by layer 2.

**The outstanding test**, in two parts that can fail independently:
1. does the controller dispatch the amendment at all (single owner, in range, so it should);
2. does the rematerialization author camera motion, or leave the row failing.
"Dispatched and the parallax row still fails" is a legitimate outcome, not a contradiction.

### caesar_curia — furthest along, and the budget warning

Layer 1 passed. Layer 2 rebuilt after the first fully unassisted ADR-0010 dispatch, with
`columns_unit` going out `look_capabilities []` and the image domain paid through a judgment
decision rather than by manufacturing a signal family. Its composed critic returned a real
finding — the wide does not read as an enclosed chamber — which reached the receipt as a
contract gap, which is the loop working.

**On its last report it was at 2 of 2 dispatches on the old per-shot cap.** That cap is now
per-run (HIR-0215), so a run started from `00fbbd7` or later begins at 0 of 2.

**Budget:** roughly $49 across 52 sessions, ~$35 of it on layer 2 alone, on a 7-layer shot.
It has never reached a construction route and never exercised the human-domain judgment debt.

### hansa_silk_road — the reproduction, now unblocked

Layer 1 sealed. Layer 2's units sealed, then `hero_podium_material` published with
`mutates.roles: []` and no derived write-cluster, so it could execute no `run_bpy` at all.

**Both halves are now fixed and confirmed live**: the per-run cap let the controller dispatch
at 11.4s for $0.00 on a shot that had been terminal, and the zero-cluster gate caught *both*
bad units at the plan gate where the prior run reached only the first.

Its selected view may still hold the unbuildable unit; only a dispatch or an operator
rematerialization retracts a published one. The staging gate stops the *next* one.

## Fixes landed today, in order

| record | what |
|---|---|
| HIR-0204 | evidence is due at the frame its contract declares |
| HIR-0205 | a bound requirement asks whether the row passed, not whether it may veto alone |
| HIR-0206 | a judgment debt owes an observation only where it owns the point |
| HIR-0207 | a widened record still reads the generation before it |
| HIR-0208 | a record that digests its own serialization round-trips byte-identically |
| HIR-0209 | sessions are told their exact deferred tool names |
| HIR-0210 | the fourth autonomy consumer, plus an inventory that finds the fifth |
| HIR-0211 | a passing critic verdict records how it decided |
| HIR-0212 | a forecast row states its condition, not a verdict |
| HIR-0213 | a projection and its re-derivation select evidence alike |
| HIR-0214 | a falsified unit is a typed stop; a cause has its own identity |
| HIR-0215 | a per-run cap counts its run; a unit that cannot mutate is refused |
| HIR-0216 | a gate reports every violation; a kickoff renders what it cites |

## Still open, none blocking a run

- **The staging-refusal forecast.** `look_capabilities` and `image-signal-bootstrap` cannot
  both fire on one candidate — adding the image claims the first demands is what creates the
  debt the second refuses. The first should forecast the second. Its four sibling refusals
  may be collectable instead; each precondition needs checking first, because "collect them
  all" is exactly the change that turns one true refusal into one true and one spurious.
- **The rematerialization receipt records that authority changed, not in which direction.**
  `RematerializationCommit` carries before/after digests and nothing distinguishing
  "amended the producer" from "amended the contract". A mechanism whose purpose is auditing
  authority changes must not certify a difference without certifying its direction.
- **Truncation advice.** "Raise the budget or split the layer" is wrong for a model-silence
  truncation, and the path knows the idle deadline is what fired. Two shots reported it.
  Separately, ~1074 seconds elapsed between truncation detection and terminalization.
- **Spend accounting.** Three irreconcilable figures for one run, `num_turns` persisting as
  `None`, and a missing `cost.jsonl`. Console-derived sums are currently the only trustworthy
  numbers.
- **`--dry-run` claims `runs/latest.json`**, the documented reading order's first step, so a
  preview displaces the real terminal run for any later reader.
- **`control_roles` has no accepted form the materializer can find.** Eight refusals across
  three materializations, each naming the offending token and never the accepted shape. This
  is the upstream cause of the zero-cluster unit: unable to express a mutation selector, the
  materializer settled for a bare control, which derives no family.
- **A truncated unit still emits a build summary describing geometry that was never written.**
  One directory from the sealed script that contradicts it.
- **The adoption counter reports `measured: true` with `measured_calls: 0`** alongside 56
  `check_scene` calls, so the counter measures something narrower than its name.

## Two design rules earned today, not yet written into AGENTS.md

Both need their fixes to land first; a rule the code does not follow is the "prose alone is
not enforcement" failure.

**A surface that instructs an action states what it already knows about that action's
consequence.** The test is whether the surface holds a property of the *current* state that
the instructed change cannot alter — a signal family does not appear by adding image claims,
a falsified unit is not un-falsified by widening a run range, a deferred bound does not move
because a unit has not measured it. Where the instructed change *would* alter the property, a
verdict scoped to the moment is honest and no forecast is owed. Instances: the unit card
(HIR-0212, fixed), the staging refusal (open), the controller's out-of-range advice (open).

**Where an artifact exists, reasoning about a description of it is not verification.** A
description is inherently uncomparative, so any conclusion depending on a comparison the
description did not make is unsupported however accurate the description is. Four of this
session's own errors were instances, each corrected by opening the file.

## Measurement worth keeping

Measure-to-mutate ratio, counted as `check_scene + contract_result + inspect_scene +
inspect_nodes + list_keyframes + probe_candidate` against `run_bpy`, over every run of each
shot:

| shot | measurement | run_bpy | ratio |
|---|---|---|---|
| room | 87 | 20 | 4.3 : 1 |
| hansa | 167 | 60 | 2.8 : 1 |
| caesar | 542 | 109 | 5.0 : 1 |

**What it cannot tell us:** nobody measured this before the instruments existed, so there is
no before, and a high ratio is equally consistent with "the instruments are used" and "the
instruments take five calls to get right". What it establishes is that measuring dominates
mutating by three to five everywhere visible, which is the shape the design predicts.

Cost per turn is near-constant **within a role** (~$0.03, 1.3x spread across eight builder
units) and varies 3-6.5x across roles. So unit cost is linear in turn count: a long unit is
not an expensive unit. Within-role comparisons are about the work; cross-role ones are mostly
about the role.
