---
id: HIR-0232
title: An amendment is bounded by the finding that drove it
status: accepted
introduced_in: unreleased
date: 2026-09-06
failure_class: a_repair_spent_fidelity_on_a_bound_its_finding_never_put_in_question
mechanism: a_finding_driven_amendment_may_enlarge_a_named_row_but_never_shrink_it
adr: null
---

# An amendment is bounded by the finding that drove it

## Observed failure

`room_1046_opening` layer 1 carried two camera-owned bands that the built geometry
falsified together. The controller dispatched a rematerialization from finding
`hf-6fc1f6f20655fcfdf849`, whose whole content on this point is:

```
contract_ids      ["cam-bbox-f1-building", "cam-bbox-f38-building"]
conflict.kind     "contract"
fault_owner_units ["camera_move"]
observation       f1 measures 0.3509 against 0.15..0.35 — 0.0009 over the upper bound
                  f38 measures 0.4576 against 0.55..0.8 — 0.0924 under the lower bound
```

The amendment resolved the contradiction. It also moved bounds the conflict never
implicated:

```
f1    0.15-0.35  ->  0.28-0.46     conflict was the UPPER bound (over by 0.0009)
f38   0.55-0.80  ->  0.40-0.58     conflict was the LOWER bound (under by 0.0924)
```

The driver then measured the references by horizontal-span segmentation, which separates
the lit building from the ground pool:

```
f1   refs/frame_0s.jpg    rows 258..395  = 0.216
f38  refs/frame_1p5s.jpg  rows 216..527  = 0.487

f1   ORIGINAL 0.15-0.35 CONTAINS 0.216    AMENDED 0.28-0.46 EXCLUDES it
f38  ORIGINAL 0.55-0.80 EXCLUDES 0.487    AMENDED 0.40-0.58 CONTAINS it
```

**Only f38 was ever wrong. The amendment fixed the broken row and broke the correct one**
-- at the establishing frame whose authored beat is *"the hotel emerges from darkness in
a small pool of light"*. A band that now requires >= 0.28 makes the reveal read larger
than the plate it is judged against.

## Root cause

The finding said one thing: these two rows are jointly unsatisfiable. It did not say "and
f1 should match the reference", because nothing in the system knows that -- there is no
instrument that measures subject extent in a reference still. So nothing defended f1, and
f1 was cheaper to move than the geometry.

**An amendment's search space is bounded by its finding's statement, and anything true
but unstated is free to be spent.** The amendment treated built geometry as ground truth
and authored bands as adjustable, which is exactly backwards for a band authored from a
reference.

The tempting fix -- check the new band against the reference -- requires the extent
instrument that does not exist. The fix that does not is one step earlier:

> **Resolving an unsatisfiability requires giving some row more room. It can never
> require giving one less.**

So a named row whose admissible set *shrank* was not asked for by the finding. That needs
no measurement, no reference, and no new evidence: only the before band, the after band,
and the finding's own `contract_ids`.

## Decision

`domain/amendment_scope.tightened_conflict_rows` compares the admissible interval of
every row a `contract`-kind finding names, in the base view and in the candidate, and
reports any that shrank. `validate_materialization` runs it and refuses with the row, both
bounds, and the legal next action.

Three scoping decisions, each of which the alternative would have got wrong:

- **Only rows the finding names.** An operator-directed rematerialization carries no
  finding and is bounded by the operator, who may narrow a band deliberately and say so
  in the trigger. The driver's corrected remat does exactly that; nothing here may refuse
  it.
- **Only `conflict.kind == "contract"`.** A capability or unit conflict names no rows, so
  the check is inert rather than guessing.
- **Not filtered by the finding's `layer`.** That field records where a finding was
  *raised*, not the authority it indicts: this record carries `layer: "2"` and
  `unit: "ground_island"` while naming two rows owned by layer 1 with a fault owner of
  `camera_move`. Filtering on it would mean the check never fired on the layer actually
  being amended -- verified against the live record before the reader was written.

## Validation

`src/tests/unit/test_amendment_relaxation_scope.py` -- seven tests on the exact rows and
finding from run `20260905T154134Z-2feeec`:

- both unasked moves are refused and named (`raised its floor 0.15 -> 0.28`,
  `lowered its ceiling 0.8 -> 0.58`);
- the refused move is shown to be *what excluded the reference*: the original band
  contains 0.216 and the amended one does not, so this is not a stylistic objection;
- an enlarge-only resolution is accepted and still admits the reference;
- an amendment with no finding is untouched;
- rows absent from either side are not compared;
- the reader does not filter on the raising layer;
- a non-contract conflict yields nothing.

`src/tests/unit/test_amendment_scope_end_to_end.py` is the discriminator and deliberately
imports **no symbol this change introduces**, so it fails on behaviour rather than on a
missing name. On the pre-fix tree:

```
E   Failed: DID NOT RAISE ValueError
```

The validator accepts the amendment silently. That is the defect, reproduced through the
public entry point.

## The refusal names the permitted move too

A first draft said only *"you shrank this"*. The `room_1046_opening` driver pointed out
that a materializer trying to author a compliant amendment needs to know where the
resolution was allowed to take its room, not just which move was refused -- and that the
solver reports `binding row(s):` with residuals.

Checked before claiming it: the finding records residuals **in prose only**
(`observations[].reason`), so the binding bound is not structurally readable from it. But
the amendment itself shows it -- **the bound that was enlarged is where the resolution
took its room** -- and that needs no new data:

```
cam-bbox-f1-building raised its floor 0.15 -> 0.28 — the resolution took its room at the
ceiling (0.35 -> 0.46), which is permitted; keep that and restore the other bound
```

That is the same finding stating the compliant amendment rather than leaving it inferable.

The driver's original objection was that the rule would "block the fix and the defect
together", because both rows shrink on one side and enlarge on the other. That does not
hold: `TightenedBound` judges the two bounds independently, so f38 with its floor dropped
and its ceiling kept at 0.80 is **accepted**. The objection reasoned at row granularity
about a rule that operates at bound granularity -- and it was raised, checked, and
withdrawn before the change landed, which is the process working.

## Consequence

This is narrower than the problem it comes from. It cannot tell that a band matches its
reference -- that needs the extent instrument, which is still queued -- and it cannot stop
an amendment authoring a *wider* band that is equally unfaithful. What it removes is the
specific move that cost this shot its establishing frame: spending a constraint nothing
put in question, in a repair that had licence to change one thing.
