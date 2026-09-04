---
id: HIR-0211
title: A passing critic verdict records how it was decided
status: accepted
introduced_in: unreleased
date: 2026-09-05
failure_class: a_required_field_was_set_on_every_branch_except_the_ordinary_one
mechanism: the_verdict_is_stamped_where_it_is_produced_instead_of_defaulted_by_one_consumer
adr: null
---

# A passing critic verdict records how it was decided

## Observed failure

hansa run `20260904T210930Z-29f9d8`, the furthest that shot has ever reached: composed
canonical replay, raster, and two independent reference judgments all executed, both
judgment debts paid by the critic against their references. Then:

```
ValueError: layer evaluation canonical[2].verdict.decided_by must be a non-empty trimmed string
  domain/layer_evaluation_receipts.py:327  _canonical_row
  agents/builder/layer_composition_finalization.py:677  finalize_composed_layer
```

`canonical[0]` and `canonical[1]` are the two rows a judgment debt came due on and both
carried the field. `canonical[2]` is the third judge frame, judged by an ordinary critic
call that passed.

## Root cause

Every branch that ends a judgement early labels itself: `checks` in critic focus,
`no_optical_signal` in payment, `actionable_panel_dissent` in panel reconciliation,
`unit_executable_evidence` and `provisional_requirement_contract_gap` in the verdict
paths. The ordinary case — a critic call that returns a passing verdict — labelled
nothing.

That survived because one consumer papered over it. `unit_finalize` reads
`v.get("decided_by", "critic")`, so the unit path silently supplied the missing value,
while `_canonical_row` requires it and the layer path does not. Two consumers of one
field, disagreeing about whether it is optional, and only one of them ever saw a passing
composed critic row — because no shot had previously reached one.

## Decision criteria

- A field a receipt requires is set where the value is produced, not defaulted by
  whichever consumer happens to notice.
- A branch that already recorded how it decided keeps its own answer.
- Empty and whitespace are not answers; the receipt rejects both, so the producer must
  treat them as unset.

## General mechanism

`agents/builder/critic._decided` stamps `decided_by` on a verdict that lacks a non-empty
one, and both of `_judge`'s return paths pass through it — the single-call verdict and the
panel result. Branches that already labelled themselves are untouched.

## Rejected patch-level alternatives

- Defaulting in `_canonical_row` as `unit_finalize` does: adds a third opinion about
  whether the field is optional and loses the distinction between a critic verdict and an
  unlabelled one.
- Relaxing the receipt to accept an empty string: the field exists to record how a layer
  was decided, and an empty one records nothing.

## Validation

- `src/tests/unit/test_forecast_status_is_conditional.py`: a passing verdict is stamped
  `critic`; a verdict that already named `checks`, `no_optical_signal` or
  `actionable_panel_dissent` keeps it; empty and whitespace-only values are treated as
  unset.

## Release and rollback

Additive field on verdicts that previously omitted it. No schema change. Rollback restores
the crash on the first passing composed critic row.

## Remaining limitations

`unit_finalize` still defaults the field rather than reading it, so the two consumers
still disagree in principle even though the producer now always supplies a value. Removing
that default is safe once no producer can omit the field, and is not done here.
