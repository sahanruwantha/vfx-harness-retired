---
id: HIR-0247
title: A finalization failure names the judgment, not just the status
status: accepted
introduced_in: unreleased
date: 2026-09-06
failure_class: the_publication_check_held_the_terminal_receipt_and_reported_one_field_of_it
mechanism: the_refusal_is_rendered_from_the_failing_canonical_rows_and_their_owning_groups
adr: null
---

# A finalization failure names the judgment, not just the status

## Observed failure

`hansa_silk_road`, run `20260906T095020Z-3ba2cc`. What reached the operator:

```
harness_defect: The 'build' boundary returned without typed stop authority; it raised
RequestedExit: layer 2 has no complete receipt-bound publication:
layer 2 terminal finalization is 'failed', not 'passed'
```

What the receipt one attribute away actually held, read from
`checkpoints/layer-finalizations/lfc-4cae2516….evaluation.json`:

```
final_status: failed
canonical[3..5]  frames 1, 51, 151   decided_by: no_optical_signal
                 issue: "candidate plate has no optical signal (black/empty)…"
evaluation_groups[2]  debt_id jd-06fa28ea…   requirement_ids ['R50']
                      canonical_start 3, canonical_end 6
```

Three named judgments, their frames, the decider, the issue text, and the debt and
requirement they were paying. `layer_publication.py:476` had the whole receipt bound and
reported `final_status`.

Same shape as HIR-0242's stale claim: **a frame holding exactly what the operator needs
and naming only the condition.**

## Mechanism

`describe_failed_finalization(canonical, groups)` renders the refusal from the rows that
failed, grouping by decider, naming the frames, mapping each row to the group whose
`canonical_start`/`canonical_end` bracket it, and quoting the first issue. On hansa's real
receipt:

```
3 judgment(s) failed at f1, f51, f151 decided by 'no_optical_signal' -- the evidence could
not be produced; group 2 paying requirement R50 / debt jd-06fa28eac2ac05f79. First issue:
candidate plate has no optical signal (black/empty)…
```

**The classification is the part a reader acts on.** A verdict decided by a contract-gap
or judgment-only decider did not weigh the work: the authority was incomplete, or the plate
could not be judged. Anything else that fails is a negative judgment of work that *was*
evaluated. Those are different failures with different owners, and the string now says
which one it is. `EVIDENCE_UNAVAILABLE_DECIDERS` is derived from the one vocabulary
HIR-0240 established rather than a fourth hand-kept list.

## Validation

`src/tests/unit/test_finalization_failure_names_the_judgment.py`, on the shape read from
hansa's own receipt. The test that carries the record is
`test_unavailable_evidence_and_a_negative_verdict_read_differently`: the same function over
a `no_optical_signal` row and over a `visible_fraction reads 0.19 against >= 0.30` row must
produce different classifications, and the negative one must still name what failed and who
owns it.

Also pinned: a row with no owning group says "no group" rather than inventing one; several
deciders are reported separately rather than merged; and nothing failing returns the empty
string, because a caller reporting a non-passed status with no failing row has a different
problem and must say so in its own words.

## A note on how this was verified, because the first attempt was a no-op

`git checkout <parent> -- src/vfx_harness` **does not remove a file the parent never had.**
`layer_finalization_diagnosis.py` is new, so the revert left it in place and all seven tests
passed against a tree that still contained the mechanism. The revert has to remove the
directory first (`rm -rf src/vfx_harness && git checkout <parent> -- src/vfx_harness`),
under the same per-path precondition, and then it discriminates.

Third form of that trap in one day, after a stash that stopped discriminating once the work
was committed and a parent-revert that ate uncommitted work. Recorded in AGENTS.md.

**And the discrimination it then gives is weak, which is worth stating rather than
dressing up:** these tests exercise the new function directly, so on the pre-fix tree they
fail on a missing module rather than on wrong behaviour. The behavioural form would drive
`layer_publication`'s refusal end to end, which needs a complete terminal-receipt fixture.
The distinction the user asked for -- unavailable evidence versus an ordinary negative
verdict -- *is* tested, on the shape read from hansa's own receipt; it is the
pre-fix failure mode that is uninformative.

## What this does not fix

**The stop is still classified `harness_defect` and routed to engineering.** The message now
identifies the failed judgment and its owner; the envelope does not. `RequestedExit` is a
typed *exception* and not a complete typed *stop envelope*, and passing a `terminal_cause`
through would improve the diagnostic without closing that gap -- completion means the
boundary publishes a source-backed stop whose action names the unresolved prerequisite and
its owner.

That half is owed and is deliberately not attempted here, on the same reasoning as HIR-0242:
this change is consumed today by the string an operator reads, and the envelope work
introduces its typed value together with the consumer that reads it.

It also does not repair hansa's schedule. Two of that layer's requirements are appearance
propositions about a lit scene on a layer with no illumination, and naming the failure
precisely does not make it satisfiable.
