---
id: HIR-0238
title: The layer judge list is covered by the union, not by each unit
status: accepted
introduced_in: unreleased
date: 2026-09-06
failure_class: a_layer_published_with_judge_frames_no_unit_required_claim_could_cover
mechanism: layer_judge_coverage_is_checked_against_the_union_of_unit_required_claims
adr: null
---

# The layer judge list is covered by the union, not by each unit

## Observed failure

`caesar_curia` layer 1, run `20260906T002035Z-f1ad1d`. Reported by the caesar_curia
driver, with these three lines:

```
LAYER 1 judge list                     [1, 121, 301, 541, 841, 1081]   (sparse bundle AND jit view)
union of unit required-claim moments   [1, 121, 301]
unit camera_rig judge frames           [1, 121, 301]      <- internally consistent, covered
```

The unit is fine: two `executable_required` claims covering exactly its own three judge
frames. The composed canonical evaluates the **layer's** six, finds three with no required
claim, and emits -- correctly -- the look-less refusal:

```
"mean": 1.0, "pass": False, "contract_gap": True,
"issues": ["look-less evaluation cannot be settled by a visual critic;
            required claims at this frame must be executable_required"],
"decided_by": "lookless_requires_executable_claims"
```

Materialization published a layer whose composed evaluation cannot be satisfied by
construction, and the plan gate passed it.

## Why it was allowed

HIR-0045 states the rule generally -- a judge frame with no required claim is a
contract_gap. `uncovered_unit_judge_frames` implements it over **one unit's** judge list.
A unit judge list is a subset of the layer judge list, so a layer frame outside every
unit's judge list satisfies the unit rule vacuously, at every unit, forever.

The composed canonical is the consumer, and it is quantified differently.
`agents/builder/provisional_judgment.py:_composition_judge_unit` builds the synthetic
judge unit like this:

```python
layer_points = tuple(getattr(layer, "judges", ()) or ())      # judges: from the LAYER
...
claims = (*unit_claims, *qualitative)                         # claims: from the UNITS
judges = tuple(... for frame, ref in dict.fromkeys((*layer_points, *debt_points)))
```

So `uncovered_unit_judge_frames(composition_unit)` **is** the layer-level check. The
predicate has been in `domain/` the whole time; no boundary applied it to the composed
unit, which is the only unit that carries the layer's judge list.

This is the same shape as HIR-0236 the day before -- a rule stated generally and
implemented at one narrower quantifier -- and the same shape as the duplicate-derivation
family the day before that. Here the two derivations are of *which frames must be
covered*: one in the validator (per unit) and one in the composed judge (per layer),
never pinned against each other.

## Mechanism

`domain/work_units/frames.py` gains two functions and one rule string, sharing the
existing definition of "covered":

- `_required_claim_moments(claims)` -- the union of required claim moments, now called by
  `uncovered_unit_judge_frames` too, so there is one definition of covered;
- `composed_evaluation_is_lookless(units)` -- no unit declares `look_capabilities`, some
  required claim exists, and every required claim is `executable_required`;
- `uncovered_layer_judge_frames(judge_frames, units)` -- the layer frames that union does
  not reach, empty whenever a critic will decide the layer instead.

`_composition_judge_unit` now calls `composed_evaluation_is_lookless` for its own early
returns. Its four guards were exactly that predicate written out; replacing them makes the
builder and the gate share one answer to "will this layer be decided mechanically", so the
gate cannot demand executable coverage of a layer a critic actually judges. That
equivalence was checked guard by guard and the replacement is behaviour-preserving.

Two callers: `orchestration/jit_materialization/validate.py` reports a collectable finding
at `/layer/stages`, and `evaluation/plan_gate/evidence_coherence.py` emits a blocking
`layer-judge-coverage` finding built with `Finding.in_layer`, so the owning layer is in the
typed field and not only in the prose (HIR-0187).

The pointer is `/layer/stages`, not `/layer/judge`: the layer judge list is structural and
materialization cannot change it. The fix is a required executable claim reaching those
frames, on a unit that judges them -- and the rule string says so rather than only naming
the violation.

## Validation

`src/tests/unit/test_layer_judge_coverage.py`, using caesar's exact frame numbers.

The file deliberately imports **no symbol this change introduces**. It reaches the
mechanism through `_composition_judge_unit`, `uncovered_unit_judge_frames` and
`_check_evidence_coherence`, all of which predate it, and names the gate finding by its
string. So on the pre-fix tree it fails because a finding is absent, not because a module
is missing:

```
matches = [finding for finding in findings if finding.check == CHECK]
E       AssertionError: []
```

Five of the seven tests pass on both trees, and that is the point of them: they state the
situation the fix addresses -- the composed unit is unsatisfiable while the real unit is
clean, a look-owning layer compiles no look-less judge at all -- and they would keep
passing if the fix regressed. The two that move are the mechanism.

Coverage is asserted as a union: two units, neither judging all six frames, together
clear the layer. Both exemptions are asserted through the builder as well as the gate, so
a future narrowing of one and not the other fails here.

## Verified in action, on the artifact that produced it

Not on a fixture. `caesar_curia`'s selected JIT view
(`state/jit-layers/views/24199bde.../layers.json`, bundle `05275c6f`, plan revision 4)
copied read-only to scratch, and `_check_evidence_coherence` run over it from two trees:

```
=== vfx-harness-wt9 @ 50a5b9f (main) ===
total findings: 0   layer-judge-coverage: 0

=== vfx-harness-wt11 @ 1bb281c (this change) ===
total findings: 1   layer-judge-coverage: 1
  check=layer-judge-coverage blocking=True layer='1'
  where=layer 1 judge f541, f841, f1081
  what=composed canonical is decided mechanically here, and no unit required claim covers
       these layer judge frames
```

The view itself, read rather than taken from the report:

```
layer 1: judges=[1, 121, 301, 541, 841, 1081]
         units=['camera_rig']
         required moments=[1, 121, 301] authorities=['executable_required'] look=[]
```

On main the gate returns **zero** findings for the exact authority that published an
unsatisfiable layer. Layers 2-5 carry no stages yet and are correctly untouched.

This is `_check_evidence_coherence` in isolation over a copy of the selected view, not a
full `vfx plan` gate invocation -- but it is the same function on the same bytes, and the
before/after is a comparison actually performed rather than described.

## What this does not fix

`decided_by: lookless_requires_executable_claims` still crashes the layer evaluation
receipt, whose allowlist accepts one value where the composed path emits five. That is the
caesar_curia driver's own change, in `domain/layer_evaluation_receipts.py`, and it is
deliberately separate: this record removes the *reason* a correct diagnosis was produced,
that one removes the crash when a correct diagnosis is produced for some other reason.

It also does not claim the layer judge list was wrong to carry six frames. Whether layer 1
should judge f541 onward is a planning question this change does not answer; it only makes
publishing a layer that cannot be judged impossible.
