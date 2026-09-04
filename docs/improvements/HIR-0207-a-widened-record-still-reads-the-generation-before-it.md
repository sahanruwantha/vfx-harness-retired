---
id: HIR-0207
title: A widened durable record still reads the generation sealed before it
status: accepted
introduced_in: unreleased
date: 2026-09-04
failure_class: an_additive_field_was_made_required_on_read_and_orphaned_every_sealed_receipt
mechanism: additive_record_fields_are_required_on_write_optional_on_read_with_a_derived_historical_default_and_a_prior_generation_parse_test
adr: null
---

# A widened durable record still reads the generation sealed before it

## Observed failure

Within an hour of pushing `226e633`, a session resumed its shot on the new revision and
the gate refused in 4.8 seconds, before any model spend:

```
✗ layer 2 plan did not clear the deterministic gate
  ✗ [hierarchy] layer 1 publication is invalid: work-unit
    state.layer_finalization.terminal_receipt.evaluation_receipt.replay_receipts[0]
    .receipt.observation.plan.claims[0] has unsupported layer replay claim shape
  status: failed, exit 3
```

A second session independently verified the same break against its own artifacts before
upgrading, and held. Between them: two passed layers, eight sealed units, and $31 of
banked work that could not be read on the new revision.

HIR-0204 widened the accepted key set of a durable record:

```python
-    {"claim_id", "authority", "judge_frames", "evidence_ids"}
+    {"claim_id", "authority", "judge_frames", "evidence_ids", "evidence_frames"}
```

and parsed `value["evidence_frames"]` unconditionally. Every layer replay receipt sealed
before that commit carries the four-key shape, so every one became unreadable.

I had told both sessions their sealed work would survive, on the grounds that
`DIGEST_SCHEMA` was untouched. That was true and it was the wrong question.

## Root cause

Two independent durability surfaces were treated as one. `DIGEST_SCHEMA` governs unit and
layer capsule content and has a migration command behind it
(`vfx migrate-digest-schema`). Typed durable records — replay receipts, evaluation
receipts, finalization receipts — have their own accepted key sets, enforced by exact
`set(value) != _FIELDS` equality, and no migration path at all. A change to the second
surface is invisible to every check that guards the first.

The rejection itself was correct: it failed closed at a gate, before spend, naming the
exact field. AGENTS.md requires obsolete schemas to be migrated or rejected, never
interpreted heuristically, and that is what happened. What was missing is the other half
of the same rule — the migration. The only route left to an operator was to discard every
receipt sealed before the change.

The deeper cause is that the field was made required on read when its own semantics make
absence meaningful. `evidence_frames` absent is not a missing value to guess at: it is
exactly the state a new record carries when no row declares a schedule, and it is the
faithful reading of what that receipt proved, because a receipt sealed under the previous
reader recorded rows that were due at every frame their claim judged. Reading it any other
way would misrepresent history. The same is true of `debt_points` (HIR-0206): a stored
plan without it minted under a reader that demanded an observation at every judge point,
so a debt there owned all of them and a debtless group owned none.

Neither test suite caught it because both new fields round-tripped perfectly. A widening
is trivially self-consistent; what it breaks is the generation before it, and nothing
parsed one.

## Decision criteria

- An additive field on a durable record is required on write and optional on read.
- Absence is read as a derived historical default only where that default is the single
  reading consistent with the record having been written at all. Where absence is
  genuinely ambiguous, the record is rejected and a migration is owed.
- Required fields stay strictly required, and the refusal names which ones.
- A widening is pinned by a test that parses a payload with the previous key set, not only
  by a round-trip of the new one.
- Resumability is verified against real sealed artifacts before it is claimed.

## General mechanism

1. `_CLAIM_REQUIRED_FIELDS` and `_PLAN_REQUIRED_FIELDS` name the keys a record must carry;
   `_CLAIM_FIELDS` and `_PLAN_FIELDS` add the optional additive ones. Both readers accept
   `required <= set(value) <= accepted` and refuse anything else, naming the keys received
   and the keys required.
2. Absent `evidence_frames` reads as `()` — no row declares a schedule, so every bound row
   is due at every frame its claim judges, which is what the prior generation recorded.
3. Absent `debt_points` reads as the plan's own `judge_points` when a `debt_id` is present
   and `()` otherwise — the only reading under which that receipt could have minted.
4. Both defaults are pinned by tests that parse the previous key set, and the fix was
   verified by parsing every replay receipt the three live shots had actually sealed.

## Rejected patch-level alternatives

- A compatibility window with a declared expiry: correct for a genuinely obsolete schema,
  wrong here. Nothing expires — absence keeps its exact meaning permanently, and a window
  would eventually orphan the same receipts on a timer.
- A `vfx migrate-receipts` command rewriting sealed bytes: mutates immutable evidence to
  suit a reader, and every rewritten receipt would lose its original digest.
- Telling the sessions to archive and restart: $31 of banked work discarded to work around
  a two-line reader defect.
- Reverting HIR-0204: restores a defect that stalls a shot entirely.

## Validation

- `src/tests/unit/test_debt_is_due_where_it_owns_the_point.py`: a stored plan without
  `debt_points` reads as owning every judge point with a debt and none without one; a
  four-key claim row parses with `evidence_frames == ()`; a payload missing a genuinely
  required key is still refused, and the refusal names what is required.
- Verified against real sealed artifacts rather than fixtures: all four layer replay
  receipts written by the three live shots on the previous revision parse under the new
  reader, including the debt-carrying one, which derives `debt_points` of 3 from its 3
  judge points. Under the pushed reader they raise, which is the live failure both
  sessions reported.

## Release and rollback

No stored bytes change and no digest moves. Rollback restores the orphaning.

## Remaining limitations

This fixes the two fields that broke and states the rule; it does not mechanically enforce
it. Every other typed record still uses exact key-set equality, and nothing yet fails a
build when a new required key is added to one. An architecture test that parses a
previous-generation payload for each durable record would close that, and is not done here.
