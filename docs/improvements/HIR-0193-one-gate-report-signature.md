---
id: HIR-0193
title: A finding's text ended a signature in a space and routed the run to engineering
status: accepted
introduced_in: unreleased
date: 2026-09-04
failure_class: producer_and_reader_of_one_identity_disagreed
mechanism: one_gate_report_signature_function_both_sides_call
adr: null
---

# A finding's text ended a signature in a space and routed the run to engineering

## Observed failure

`vfx run artifacts/room_1046_opening --rounds 2` gated layer 1, correctly found one
blocking `capability-origin` finding owned by layer 1 — and then could not turn it into a
transaction:

```
plan-stop-evidence.json   evidence_kind: harness_defect
                          issues: ['gate_report_signature_invalid']
✗ controller refused dispatch (not_dispatchable): route_engineering has no
  receipt-backed controller adapter; the envelope remains the run's terminal stop
```

The finding was well-formed and the amendment was dispatchable. The stop compiled as a
harness defect because the *report's signature* failed validation.

## Root cause

`GateResult.signature()` builds the report's identity from a fixed 60-character slice of
each finding's text:

```python
f"{f.check}:{f.where}:{f.what[:60]}"
```

`_parse_gate_report` validates every published text field with `_text`, which requires
`value == value.strip()`, and separately recomputes the expected signature with its own
copy of the same expression.

The new finding's text has a space at index 59, so the slice ended in whitespace and the
report failed its own validator. One space in one message, and a run that had correctly
diagnosed itself routed to the engineering sink instead of repairing itself.

The defect is not the space. It is that the producer and the reader each implemented the
signature rule, so they were free to disagree — the same shape as HIR-0187's
`required_after_source` (a producer restating what its validator owned) and HIR-0188's
lifecycle key vocabulary. A slice of arbitrary prose was being used as a validated
identifier with no normalisation anywhere.

## Decision criteria

- One identity, one implementation. A value that must satisfy a validator is computed by
  code the validator shares.
- Normalise at the boundary that creates the value, so no message text can make a report
  unpublishable.
- Stripping must not weaken the identity: distinct findings must still sign differently.

## General mechanism

`gate_report_signature(rows)` is the single implementation, used by `GateResult.signature`
and by the stop parser's expectation. Each excerpt is stripped, so a slice landing on
whitespace cannot produce a signature its own validator rejects.

## Rejected patch-level alternatives

- *Reword the finding so it does not cut on a space.* The next message with a space at
  index 59 fails identically, and no author can be expected to count characters.
- *Loosen `_text` to accept surrounding whitespace.* That weakens a check on every
  published text field to accommodate one producer, and leaves the two implementations
  free to drift.
- *Slice on a word boundary.* Still two implementations; still divergeable.

## Validation

`src/tests/contract/test_gate_report_signature.py` — pins that the observed text really
does slice onto a space; asserts the signature satisfies `_text`; asserts producer and
reader agree; parametrises over texts that cut on a space, are entirely whitespace-padded,
are shorter than the slice, and start with a tab; asserts a clean report signs empty; and
asserts distinct findings still sign differently. Reverting the strip fails five of ten.

## Release and rollback

Signatures of reports whose findings cut on whitespace change value. The signature is a
convergence identity compared between rounds of one loop, not durable authority, so a
changed value costs at most one non-converging round detected a round later. Rollback is
reverting the commit.

## Remaining limitations

- The signature remains a prose slice, so two findings differing only after 60 characters
  still collide. That is pre-existing and acceptable for loop detection; it would matter
  if the signature were ever promoted to durable identity.
