---
id: HIR-0213
title: A sealed projection and its re-derivation select evidence the same way
status: accepted
introduced_in: unreleased
date: 2026-09-05
failure_class: a_producer_was_converted_and_its_re_derivation_was_not
mechanism: every_selector_of_sealed_evidence_is_named_and_asserted_to_use_the_shared_predicate
adr: null
---

# A sealed projection and its re-derivation select evidence the same way

## Observed failure

hansa run `20260904T235126Z-e651ac` on `3823bf0`. `mint` passed (HIR-0210), the evaluation
receipt passed (HIR-0211), and the outcome projection refused its own record:

```
ValueError: layer outcome projection.canonical authoritative evidence does not
            exactly project the terminal canonical verdicts
  domain/layer_outcome_projections.py:217  _validate_canonical_projection
```

The reporting session offered the cause as a hypothesis and declined to verify it, noting
that the architecture test shipped with HIR-0210 would answer immediately. It did:
`domain/layer_outcome_projections.py` is in the inventory at two reads.

## Root cause

HIR-0210 converted the producers of the sealed `authoritative` list —
`layer_finalization_receipts._authoritative_point_evidence` and
`revalidation._authoritative_projection` — to keep every typed measurement, so a
builder-paid image row now appears in the record a layer is sealed on.

`layer_outcome_projections` re-derives that same list from the terminal canonical
verdicts, to check the stored projection is exactly what the verdicts imply. Its filter
still read the autonomy flag. So the two sets differed by precisely the rows HIR-0205
exists to admit, and the projection rejected a record that was correct.

HIR-0210's own decision criteria named this rule — "producer and consumer of a derived
field convert together" — and it was applied to one pair (`evidence_failures`, converted
in `verify.py` and `layer_evaluation_receipts.py` together) and missed for another. The
inventory listed the module; I read that list as "remaining, unexamined" rather than
cross-checking it against what I was converting in the same change. The instrument was
present and I did not query it.

## Decision criteria

- Every module that produces or re-derives the sealed evidence list is named in one place
  and asserted to select through the same predicate.
- An inventory of what remains is not a substitute for checking whether an item is paired
  with something being changed.
- A projection that verifies a record against a re-derivation must not be able to disagree
  with the producer of that record by construction.

## General mechanism

1. `layer_outcome_projections` selects through
   `evidence_authority.is_recorded_evidence`, matching both producers. Its remaining raw
   reads are a boolean type check and the stored-list read, neither of which decides
   membership.
2. `test_autonomy_flag_reads_are_inventoried` gains `SEALED_EVIDENCE_SELECTORS`, naming
   the five modules that produce or re-derive sealed evidence — two projection producers,
   one projection re-derivation, and the `evidence_failures` producer/consumer pair — and
   asserts each uses the shared predicate. Converting one side of a pair now fails the
   suite.

## Rejected patch-level alternatives

- Relaxing the projection's equality check: it exists to prove the stored record is
  exactly what the verdicts imply, and a tolerance would make it prove nothing.
- Converting only the failing module: leaves the next unpaired selector free to produce
  the same failure, which is how this one arrived after HIR-0210.

## Validation

- `src/tests/architecture/test_autonomy_flag_reads_are_inventoried.py`: every named
  selector of sealed evidence uses `is_recorded_evidence`, and the raw-read inventory is
  unchanged at twenty-two across fourteen modules.

## Release and rollback

No schema or digest change. A layer whose canonical verdicts include a bound image row now
projects it into the sealed outcome, where the projection previously refused to mint at
all. Rollback restores the refusal.

## Remaining limitations

`SEALED_EVIDENCE_SELECTORS` is a hand-maintained list, so a new selector added without
being named there is still unguarded — the same weakness as the inventory it sits beside.
Both make an omission visible on the next change to a listed module rather than at the
moment it is introduced.

The reporting session also established a second-order cost worth recording here rather
than losing: releasing an orphaned finalization claim discards the paid judgment bound to
it, because release marks unsealed judgment output non-reusable. Two critic-scored
judgment debts, persisted intact through a failed receipt, were invalidated by the release
that cleared the claim blocking them. The release was correct and the only sanctioned
clearing transaction; the cost is that a release AFTER judgment is more expensive than one
before it, and nothing tells an operator which case they are in. A stop that names the
claim it left held should also name whether paid judgment is bound to it.
