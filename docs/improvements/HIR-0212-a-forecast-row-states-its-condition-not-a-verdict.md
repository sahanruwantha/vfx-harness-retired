---
id: HIR-0212
title: A forecast row states the condition it turns on, not a verdict compiled before the measurement exists
status: accepted
introduced_in: unreleased
date: 2026-09-05
failure_class: a_conditional_classification_was_asserted_as_an_unconditional_property
mechanism: the_card_carries_the_irreversible_bound_and_the_sharers_and_derives_no_status
adr: null
---

# A forecast row states the condition it turns on, not a verdict compiled before the measurement exists

## Observed failure

caesar run `20260904T191549Z-e048a6`, layer 2 `floor_aisle_unit`. The builder quoted the
compiled card and reasoned correctly from it:

> "the compiled unit-scope card explicitly lists this unit's deferred-payment rows as
> `(none)` and classifies those three as `diagnostic_only` forecasts with
> `pending_producers: [benches_unit, dais_unit, columns_unit]` and `fault_owner: 1` — not
> this unit's responsibility since I'm not the dependency-complete `env.*` payer yet"

Sixty seconds later:

```
unit evidence: 10/10 bound checks observed · REVISE
  · [check:cam-bbox-f1-env] bbox_height reads 1.0 against 0.5..0.95
    — irreversible partial-subject union violation
```

Three surfaces, two answers, same three ids, same unit, same minute: `run_bpy`'s read-back
said REQUIRED BEFORE FREEZE, the card said `diagnostic_only`, and the evaluator failed the
unit on them.

## Root cause

The rule is conditional on a live measurement. `deferred_subject` promotes a forecast row
to a blocker exactly when the measured union has crossed the side it can never come back
from — growing past a ceiling on width, height or bottom, or falling below a floor on top.
Below that threshold it is diagnostic; above it, required.

`unit_scope` wrote `"diagnostic_only": True` and `"acceptance_evidence": False` as
literals, at kickoff, before any mutation exists to measure. It never called the
classifier. So it asserted a permanent property where the rule defines a conditional one.

The rendered heading repeated it, and that half was the stronger one. Verbatim from the
kickoff transcript:

```
deferred subject forecasts (diagnostic only; the complete-subject payer alone can
satisfy these rows):
  - {"acceptance_evidence":false,"diagnostic_only":true,"fault_owner":"1","id":"cam-bbox-f1-env",...}
```

The JSON carried a boolean; the heading carried a REASON — *the complete-subject payer
alone can satisfy these rows* — a claim about why the boolean is correct. The builder's
own account tracked the JSON field names but its reasoning paraphrases the heading: "not
this unit's responsibility since I'm not the dependency-complete `env.*` payer yet". It
was not reading a stray flag it might have questioned; it was reading an explanation,
corroborated by a boolean, both wrong.

That is why the fix has to change both. Correcting the row alone would leave the
persuasive half intact and produce a card that contradicts itself, which is worse than one
that is consistently wrong: a reader who resolves the contradiction in favour of the prose
ends up exactly where the builder did.

The reporting session found the sharper statement of it: those literals sit **three lines
below** the computation of `sharing`, the union producers and pending producers that
HIR-0197 added so a partial producer can see how much of a shared band it is consuming,
under a comment explaining why that matters. The card computes the instrument and then
stamps a label beside it telling the builder the rows are not its business. A builder that
read the sharing data and ignored the label would have done better than one that read
both — the wrong way round for a surface the rules tell agents to trust over their own
judgement.

The consequence was not hypothetical. The amendment's repair was "push the chamber
assembly back and centre it"; the builder widened the floor to span the full hall
footprint, which drove the `env.*` union to 1.0 at all three frames, over the camera's
bands on the irreversible side. HIR-0197 shipped the measurement built to prevent exactly
that, and a stale label on another surface made it irrelevant.

## Decision criteria

- A surface compiled before a measurement exists states the condition, never a verdict.
- Prose and structured fields on the same surface are corrected together. A heading that
  explains why a field is set is weighted more heavily than the field, so a half-fix leaves
  the persuasive half standing and makes the surface self-contradictory.
- The measurement and the sharers are the operative facts; status is derived from them,
  not asserted beside them.
- The condition is computed by the same module that owns the promotion rule, so the card
  and the classifier cannot drift.
- A row with no irreversible side carries no bound, and says so by omission rather than by
  a false negative.

## General mechanism

1. `deferred_subject.deferred_subject_irreversible_bound` returns the side, the bound and
   the direction that crosses it, for a row that has one — the same derivation
   `deferred_subject_union_slack` uses once a reading exists, without needing a reading.
   Repairable sides and non-union kinds return `None`.
2. The card's forecast rows drop `diagnostic_only` and `acceptance_evidence` and carry
   `status: "conditional"`, `diagnostic_while_inside_bound`, and `irreversible_bound`
   beside the union and pending producers.
3. The rendered heading states the rule: conditional, diagnostic while inside the bound,
   REQUIRED BEFORE FREEZE for this unit the moment it crosses, pending producers share
   whatever room is left, read the slack after every mutation.

## Rejected patch-level alternatives

- Computing a status in the card from a stale or synthetic measurement: invents a reading
  the unit has not taken.
- Removing the forecast rows from the card: HIR-0134 put them there so a partial producer
  can see what it is consuming, and removing them restores the blindness HIR-0197 fixed.
- Prompt text telling builders not to trust `diagnostic_only`: the field would still be
  false, and the rules tell agents to trust the card.

## Validation

- `src/tests/unit/test_forecast_status_is_conditional.py`: the irreversible bound is the
  side a union cannot return from, and is absent for a repairable side and a non-union
  kind; the rendered card never says "diagnostic only", states the conditional promotion,
  and carries the bound and the pending producers; the heading no longer claims the payer
  alone can satisfy the rows.

## Release and rollback

Card content only. No schema, authority or evidence change. Rollback restores the label
that contradicted the measurement beside it.

## Remaining limitations

The card still cannot show the slack, because it is compiled before the unit has measured
anything — it states the bound and the rule, and the number arrives at the first
post-mutation read-back. A builder that never mutates before freezing would see the
condition and no reading, which is correct but thin.
