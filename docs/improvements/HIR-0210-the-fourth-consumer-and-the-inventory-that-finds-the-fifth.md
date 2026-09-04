---
id: HIR-0210
title: The fourth consumer of the autonomy flag, and the inventory that will find the fifth
status: accepted
introduced_in: unreleased
date: 2026-09-05
failure_class: a_truncated_search_made_a_partial_conversion_look_complete
mechanism: the_record_of_record_path_decides_through_the_shared_predicates_and_every_remaining_raw_read_is_inventoried
adr: null
---

# The fourth consumer of the autonomy flag, and the inventory that will find the fifth

## Observed failure

hansa run `20260904T191131Z-2b6787`, immediately after `hero_facade` published its outcome
cleanly — the very unit HIR-0205 was written to unblock:

```
File ".../domain/layer_replay_observations.py", line 510, in mint
    raise ValueError(
ValueError: required executable evidence must be harness-authoritative: hero-look-f1-mean
```

`hero-look-f1-mean` is builder-origin and therefore non-authoritative by design
(`checks.py:641`), exactly as intended. `LayerReplayPointObservation.mint` demanded
autonomy of it. That is the HIR-0205 defect verbatim, at a call site the conversion never
reached.

The same function held the mirror of it four lines below: `failed` was derived with
`row["authoritative"] is True and row["pass"] is False`, so a *failing* builder-paid image
row was not counted as a failure and the replay point would have read `passed`. The first
direction blocks a good run; the second would pass a bad one.

## Root cause

Not the conversion — the search. HIR-0205's audit ran

```
grep -rn '"authoritative"\|\bauthoritative\b' src/vfx_harness/ | ... | head -60
```

and the visible sixty lines were treated as the complete inventory. The full count is
twenty-eight raw reads across fourteen modules. `layer_replay_observations.py` was below
the cut, and its site does read the field, so the guess that it encoded the requirement
differently is wrong: it was simply never seen.

That makes the cause a verification failure of the same family as HIR-0207 and HIR-0208 in
this campaign: a check that could not have failed was treated as evidence. A truncated
search returns results, and nothing in the result says it is partial.

The deeper reason the conversion was partial-able at all is that the flag is readable
everywhere. One leaf owns the two questions, and nothing prevented — or counted — a
consumer asking the raw field instead.

## Decision criteria

- The record-of-record path decides through the shared predicates only. A receipt that
  seals what a layer proved must not re-implement the question.
- Producer and consumer of a derived field convert together: `evidence_failures` is
  rebuilt by the builder and re-derived by the receipt, and they must agree.
- Reading the flag stays legal where the row is genuinely unbound. The rule is not "never
  read it"; it is "never ask it about a bound id".
- Completeness is asserted, not assumed: the remaining reads are inventoried by count and
  module, so the next one is a deliberate edit rather than an oversight.

## General mechanism

1. `LayerReplayPointObservation.mint` requires each bound id to be a typed measurement
   rather than an autonomous one, and names the offending `source` when it is not. Its
   `failed` set counts every recorded reading that did not pass.
2. `verify.py`'s two `evidence_failures` producers and
   `layer_evaluation_receipts.py`'s re-derivation convert together, so a failing bound
   image contract appears in the sealed failures on both sides.
3. `layer_finalization_receipts._authoritative_point_evidence` keeps every typed
   measurement, matching the revalidation projection HIR-0205 already converted.
4. `src/tests/architecture/test_autonomy_flag_reads_are_inventoried.py` pins the exact
   remaining inventory — fourteen modules, twenty-two reads — and asserts the five
   record-of-record modules contain none. A new read fails the suite with the reason;
   removing one requires updating the list, so the count only shrinks deliberately.

## Rejected patch-level alternatives

- Converting all twenty-eight sites in this change: several concern genuinely unbound rows
  where autonomy is the right question, and converting them unexamined would trade a
  known defect for an unreviewed one.
- Forbidding the raw read outright: makes the legitimate unbound case unexpressible.
- Re-running the original grep and calling it done: the same check that already failed
  once, with no record of what it covered.

## Validation

- `src/tests/architecture/test_autonomy_flag_reads_are_inventoried.py`: the inventory is
  exact, and the five sealing modules read the flag nowhere while using the shared
  predicates. It fails on an added read, a removed one, or a changed count.
- The pre-existing layer finalization, evaluation and replay suites pass unchanged, which
  is the check that the producer/consumer pair for `evidence_failures` still agrees.

## Release and rollback

No schema or digest change. A failing bound image contract now appears in sealed failures
where it was previously omitted, so a layer that would have sealed on an unnoticed image
failure now fails — which is the point. Rollback restores both directions of the defect.

## Remaining limitations

Twenty-two raw reads remain across fourteen modules and none has been examined against the
bound/unbound distinction. The inventory makes them visible and countable; it does not
classify them.

The builder-facing summaries in `blender/tools/` were the predicted next instance — they
filter evidence for a builder deciding whether its work is done, and a builder's image rows
are exactly the ones the flag excludes. That prediction is wrong, and the check that
falsified it is worth recording. Five runs of build transcripts show two counters kept
deliberately distinct:

```
AUTHORITATIVE SCENE CONTRACTS: 0/1 pass · failing: hero-window-mask-nodes
BOUND IMAGE CHECKS: 0/1 pass
```

A builder-paid image check, non-authoritative by construction, reported to the builder as
failing. Source agrees: `reports.interface_state` derives `may_seal` from the autonomy
predicate and sets only `scene_interfaces_ready`, while the image side runs through
`compare._pixel_contract_gate` over the bound-and-paid ids and gates
`pixel_contracts_passed` separately. The `0/0` case appears only before any debt is bound,
which is the correct reading of "none owed yet" rather than "none countable". So the
separation HIR-0205 restored at the receipt sites was already present on the builder path.

What remains unexamined there is narrower than the original suspicion: the `compare_frame`
and `render_pass` metric paths report their own numbers and have not been audited against
this distinction.
