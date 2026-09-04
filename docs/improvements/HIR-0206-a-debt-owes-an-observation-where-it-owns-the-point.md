---
id: HIR-0206
title: A judgment debt owes an observation only at the judge points it owns
status: accepted
introduced_in: unreleased
date: 2026-09-04
failure_class: a_per_point_obligation_was_demanded_from_a_layer_level_flag
mechanism: the_group_plan_carries_the_debts_own_judge_points_from_one_source_of_truth
adr: null
---

# A judgment debt owes an observation only at the judge points it owns

## Observed failure

caesar run `20260904T143311Z-c0f282`, layer 2 composed finalization, on `b87035c`. The
layer's five units all sealed and passed. Its composed evaluation then died:

```
ValueError: layer evaluation canonical[1].judgment_observation has unsupported shape
  domain/layer_evaluation_receipts.py:237   _judgment_observation
  agents/builder/layer_composition_finalization.py:656  finalize_composed_layer
```

The exception escaped the builder boundary unhandled, so the run terminalized as an
unclassified harness defect at exit 1 after 141 minutes, and the controller correctly
refused to dispatch because `route_engineering` has no adapter.

The ledger shows all three judge frames were judged:

| round | frame | decided_by | mean | |
|---|---|---|---|---|
| 0 | 1 | `provisional_requirement_contract_gap` | 2.0 | fail — critic ran |
| 1 | 121 | `unit_executable_evidence` | 5.0 | pass — no vision call |
| 2 | 301 | `unit_executable_evidence` | 5.0 | pass — no vision call |

Frames 121 and 301 were decided executably without a vision call, which is HIR-0114
working as designed. That is why one critic event exists and not three.

The critic's single qualitative reading was the most valuable output of the run — that
the wide does not read as an enclosed chamber, with the repair named in one sentence and
correctly scoped to the bounded producer units. It survived only inside a defect audit
document.

## Root cause

`JudgmentDebtPayment.prepare` returns `None` at a judge point the debt does not own:

```python
if (int(frame), str(ref)) not in self._points:
    return None
```

`self._points` is built from `decision["judge_points"]` — the moments the debt owns, not
the group's judge list. So no observation is compiled at frame 121 or 301, exactly as
HIR-0163 requires: an observation records an actual attempt, and no attempt is made where
the debt is not due.

The evaluation receipt then demanded one at every qualitative canonical row from
`plan.debt_id is not None`. `debt_id` is a layer-level flag that answers "does this
group's decision carry a debt". It was read as "every canonical row here was paid by a
fresh observation". The producer was correct throughout; the consumer asked a question the
flag was never built to answer, and the record had nowhere to put the finer one.

This is HIR-0204's defect one level down: a per-point obligation derived from a coarser
flag. There, a claim's judge list was treated as its bindings' schedule. Here, a group's
judge points are treated as the debt's moments.

## Decision criteria

- The schedule comes from the debt, not from the group that contains it.
- One source of truth: the plan's `debt_points` and `JudgmentDebtPayment._points` are
  derived from the same `decision["judge_points"]` and cannot disagree.
- Symmetry fails closed both ways: a missing observation where the debt owns the point is
  refused, and an observation where it does not is refused as a producer disagreement.
- Absence is not permission: a debt that owns no judge point is refused at construction,
  because such a debt could never be demanded anywhere.
- The refusal names what it expected and what it received.
- The ledger row records the frame it judged and whether it carried an observation,
  because that row is where an operator looks and it stored neither.

## General mechanism

1. `LayerReplayEvaluationGroupPlan` gains `debt_points`: the judge points this group's
   debt owns. Validation requires it empty without a debt, non-empty with one, free of
   duplicates, and a subset of `judge_points`, naming the offending points otherwise.
2. `_canonical_row` demands a judgment observation only at a point in `debt_points`, and
   refuses a verdict that carries one at a point outside it. `_judgment_observation`'s
   refusals now name the frame and the exact expected versus received key set instead of
   "unsupported shape".
3. `_compile_group_plan` fills `debt_points` from `decision["judge_points"]`, the same
   value `JudgmentDebtPayment._points` is built from.
4. `Ledger.record_round` records `frame` and `judgment_observed`; the canonical call sites
   pass the frame explicitly.

## Rejected patch-level alternatives

- Attaching an observation at every point regardless: fabricates an attempt that was never
  made, which HIR-0163 forbids.
- Accepting a missing observation wherever one is absent: detect-and-continue, and it would
  hide a genuinely unpaid debt.
- Deriving the debt's points inside the receipt from the decision: a second reader of the
  same fact, which is how this class of defect starts.

## Validation

- `src/tests/unit/test_debt_is_due_where_it_owns_the_point.py`: the shipped shape — three
  judge points, a debt owning one — round-trips; a debt point outside the group's judge
  points is refused; a debt owning nothing and a debtless group owning something are both
  refused; duplicates and malformed pairs are refused; a point the debt does not own mints
  without an observation; a stray observation at such a point is refused; and a missing
  observation where the debt owns the point still fails closed with a message naming the
  frame.

## Release and rollback

Additive field on the group plan, read optionally for receipts sealed before it (HIR-0207).
No capsule or digest change. Rollback restores the crash.

## Remaining limitations

The exception still escapes the builder boundary as an unhandled `ValueError` rather than a
typed stop, so a future defect in this family will again terminalize as an unclassified
boundary. That is the mechanism after this one, and it is why this defect cost two shots and
three sessions to attribute.
