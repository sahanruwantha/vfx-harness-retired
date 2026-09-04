---
id: HIR-0208
title: A record that digests its own serialization must round-trip byte-identically
status: accepted
introduced_in: unreleased
date: 2026-09-05
failure_class: an_additive_field_changed_what_a_sealed_records_digest_was_computed_over
mechanism: additive_fields_are_written_only_when_they_differ_from_the_readers_derived_default
adr: null
---

# A record that digests its own serialization must round-trip byte-identically

## Observed failure

`7cc2871` fixed the parse break of HIR-0207 and the resume stayed closed, one level up.
Two shots, independently, at the same gate and at zero cost:

```
226e633  [ 4.8s]  ...receipt.observation.plan.claims[0] has unsupported layer replay claim shape
7cc2871  [10.4s]  ...receipt.observation.observation_digest does not match its payload
```

Every layer replay observation the three live shots had sealed failed the same way: four
of four. One session found it by reading its own artifacts before resuming; the other
proved the check sits on the gate path ahead of any model spend, so establishing it cost
nothing.

## Root cause

`LayerReplayObservation` verifies `observation_digest` against the payload it
re-serializes through `as_dict`. HIR-0204 and HIR-0206 made `as_dict` emit two new keys
unconditionally — `evidence_frames` on every claim, `debt_points` on the plan. A receipt
sealed by the previous writer stored a digest over the shape it actually wrote; the new
reader recomputes over the enriched shape, and the two can never match.

HIR-0207 made those fields optional on read, which fixed parsing and could not fix this.
The property the digest depends on is stricter and was never stated: **a record read from
stored bytes must serialize back to exactly those bytes.** Optional-on-read restores the
first half of the round trip; the second half was still writing a shape the record had
never had.

Both the previous fix and its verification were applied at the level that passes. The
plan parses; the observation that contains the plan is where the digest lives. "Parses
under the new reader" was the same too-narrow check as "`DIGEST_SCHEMA` is untouched",
one layer up and a day later.

## Decision criteria

- A record whose digest covers its own serialization must round-trip byte-identically, or
  the digest that made it evidence stops verifying.
- An additive field is therefore written only when it differs from the default a reader
  derives for it, so the field is invisible in exactly the records that predate it.
- Omission is not suppression: a field carrying a real value still reaches the bytes.
- Verification is re-verifying a sealed record's stored digest, not parsing it.
- Sealed bytes are never rewritten to suit a reader.

## General mechanism

1. `LayerReplayClaimRequirement.as_dict` writes `evidence_frames` only when non-empty —
   an unframed claim serializes back to the exact four-key shape it was sealed in.
2. `LayerReplayEvaluationGroupPlan.as_dict` writes `debt_points` only when it differs from
   `_derived_debt_points()`: every judge point when a debt is present, none when it is
   absent. That is the same derivation the reader applies to a plan that omits the key, so
   the pair is symmetric by construction rather than by coincidence.
3. Both readers keep HIR-0207's optional acceptance, so old and new shapes parse and each
   serializes back to the shape it came from.

## Rejected patch-level alternatives

- Excluding additive fields from the digest input: makes the digest cover less than the
  record, so a genuine schedule change would stop being detectable.
- Digesting the stored bytes instead of the re-serialization: abandons the property that a
  record's digest is derivable from its parsed value, and leaves two sources of truth.
- Versioning the observation schema and migrating sealed receipts: rewrites immutable
  evidence and destroys the digests that made it evidence.
- Verifying old receipts against the shape they were written in: a second serializer per
  generation, growing without bound.

## Validation

- `src/tests/unit/test_debt_is_due_where_it_owns_the_point.py`: a plan whose debt owns
  every judge point omits `debt_points` and re-reads identically; a debtless plan omits it;
  an unframed claim serializes back to the exact four-key payload it was parsed from; a
  partial debt schedule and a declared evidence schedule are both written and round-trip.
- Verified against real sealed artifacts, on the whole observation rather than the plan:
  all four layer replay observations the three live shots sealed on the previous revision
  verify their stored `observation_digest` and round-trip byte-identically, including the
  debt-carrying one that derives `debt_points` of 3.

## Release and rollback

No stored bytes change and no digest moves. Rollback restores the orphaning.

## Remaining limitations

The rule now has two halves — parse the previous key set, and re-verify a sealed digest —
and neither is mechanically enforced across the other seventeen typed records that use
exact key-set equality. An architecture test that round-trips a stored payload for each
would close it. Two shots caught this by checking artifacts before trusting a verification
claim; that should not be the mechanism.
