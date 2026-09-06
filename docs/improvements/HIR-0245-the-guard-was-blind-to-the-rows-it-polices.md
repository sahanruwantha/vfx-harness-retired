---
id: HIR-0245
title: The guard was blind to the rows it polices
status: accepted
introduced_in: unreleased
date: 2026-09-06
failure_class: the_amendment_scope_guard_compared_against_a_base_stripped_of_the_rows_being_amended_and_scoped_to_named_ids
mechanism: the_before_image_is_the_selected_view_and_the_comparison_covers_every_row_the_amendment_touched
adr: null
---

# The guard was blind to the rows it polices

## Observed failure

`caesar_curia` layer 1. A controller-dispatched rematerialization resolved
`hf-bd4ad4c4a58c2764e562`, whose `contract_ids` name exactly one row. Measured values from
`reports/layers/layer-2.columns_set.json`, bands from the live selected view `491eafe1`:

```
                measured   band at build      band now in force
bbox-f1-l2       0.6785    0.9 .. 1.0  FAIL   0.62 .. 0.85   -> PASSES
bbox-f121-l2     0.9105    0.85 .. 1.0 PASS   0.60 .. 0.85   -> FAILS
bbox-f301-l2     1.0       0.85 .. 1.0 PASS   0.60 .. 0.90   -> FAILS
```

**The amendment fixed the row it was asked about and broke two it was not**, against the
values the builder had measured as passing minutes earlier. HIR-0232's guard published the
candidate clean, with `local_findings: []`.

All three rows are one persistent `bbox_height` union over
`hall.*, floor.*, dais.*, columns.*, seating.*` — HIR-0152's irreversible side, which only
grows as layers 3-6 add furniture, figures and crowd. The original ceiling left 0.32 above
the measured 0.6785; the amended one leaves 0.17. **The unasked change halved the remaining
headroom on a quantity that cannot come back down**, so even the row it repaired is closer
to failing than it was designed to be.

## Two defects, and the second is the larger

**1. The before-image was the design base, which strips the rows being amended.**

`base_scene_checks_path` answered two different questions: *what the materializer designs
from*, and *what was admissible before*. They coincide on a first materialization and
diverge on a rematerialization, where HIR-0026 writes a **reverted overlay** as the design
base. Read from caesar's own views:

```
overlay (remat design base)   28 rows   owners {'2': 28}          bbox-f1-l2 absent
prior selected view           41 rows   owners {'1': 13, '2': 28} bbox-f1-l2 present
```

All 13 layer-1-owned rows are stripped. `tightened_conflict_rows` compares only rows present
in both base and candidate -- the natural scoping that makes it safe on a first
materialization -- so on a rematerialization of layer 1 it had **nothing to compare for any
row that layer owns**. Not a subtle case: the guard was blind to the entire row set a
controller-dispatched amendment touches, which is the case HIR-0232 was written for.

**2. The comparison was scoped to the ids the finding named.**

Even with the before-image corrected, `named & set(before) & set(after)` would have flagged
`bbox-f1-l2` and stayed silent on the other two. It polices the one row the finding put in
question and waves through every row it did not. **An unnamed row is further outside the
finding's bound, not nearer it.**

## Mechanism

The amendment check takes its before-image from the **selected view** rather than the design
base. The overlay stays reverted for authoring; only the comparison changed. When a selected
view exists and cannot be read, the validator emits a note rather than falling back
silently -- going quiet is how this became inert in the first place.

`tightened_conflict_rows` compares every row present on both sides and marks each
`TightenedBound` with whether the finding named it:

```
named=True   bbox-f1-l2 lowered its ceiling 1 -> 0.85 …
named=False  bbox-f121-l2 (a row the finding does not name at all) lowered its ceiling 1 -> 0.85 …
named=False  bbox-f301-l2 (a row the finding does not name at all) lowered its ceiling 1 -> 0.9 …
```

`AMENDMENT_RELAXATION_RULE` no longer says "a named row" -- leaving that prose beside a
predicate that no longer scopes that way would have been a fresh one-quantity-two-
derivations, in the same file.

## Validation

`src/tests/unit/test_amendment_relaxation_scope.py`, four new tests on caesar's real
numbers: the multi-row case, that an unnamed row says it was never in question, that the
measured values which passed before fail after -- because "out of scope" understates a
change that broke completed work -- and that a **wholly-widening** amendment stays clean.

That last one was not asked for and is the one that matters most: without it, *"the guard
now catches everything"* and *"the guard now refuses everything"* are indistinguishable from
a passing multi-row test.

## What this does not fix

**It does not repair the authority this amendment produced.** caesar's selected view still
fails at f121 and f301, and a restart builds on top of that. Both fixes prevent the next bad
amendment; neither undoes this one. That repair is a reviewed `vfx plan --rematerialize` on
layer 1 and belongs to the operator.

The wider scope means a rematerialization that narrows an unrelated row while any finding is
open is now reported. That is the rule -- such a narrowing is not bounded by the finding and
needs its own reviewed change -- but it is a behaviour change beyond the observed failure,
which is why the widening-stays-clean test exists.

Found by the caesar_curia driver, who diagnosed the reverted-overlay base and declined to
propose the fix because it "looks right and could break the reason the overlay is reverted",
and whose suggested multi-row test is what exposed the second defect.
