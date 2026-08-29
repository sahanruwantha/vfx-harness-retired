---
id: HIR-0105
title: JSON pointer rejection enumerates the live list
status: accepted
introduced_in: unreleased
date: 2026-08-29
failure_class: candidate_patch_list_location_guessing
mechanism: json_pointer_list_location_card
adr: ADR-0006
---

# JSON pointer rejection enumerates the live list

## Observed failure

Layer 2 rematerialization run `20260829T094605Z-a1b1cb` attempted to append a
scene contract through `/scene_contracts/9` and received only `index 9 is out of
range`; a later call using `/-` succeeded. Producing run
`20260829T101336Z-ac6aa6` repeated the same discovery twice: index 7 failed at
677.2 seconds and was corrected at 689.3, then index 10 failed at 914.1 and was
corrected at 925.2. The two failures consumed two model turns and 23.2 seconds in a
session that subsequently exhausted one call after repairing its terminal gate.

## Root cause

The patch boundary knew the exact live list, its length, every row's stable `id`, and
whether the pointer was a final write location, but discarded all of that information.
The generic range error forced the materializer to guess whether it should replace an
existing row or append, and which numeric index named a row after prior candidate
mutations.

This is a missing read-back instrument in the revision-checked patch transaction, not a
reason to expose the candidate document for raw reads or permit list holes.

## Decision criteria

- An invalid list pointer remains fail-closed and leaves the document unchanged.
- The rejection reports list length and the complete valid existing index range.
- When rows have stable `id` fields, it maps each live index to that id.
- A final write location explicitly names `-` as the append token.
- Read traversal never treats `-` as a location.
- Negative indices are rejected rather than inheriting Python's from-the-end indexing.
- Guidance comes from the exact in-memory revision being mutated, not from a parallel
  candidate summary or model-maintained count.

## General mechanism

The canonical RFC 6901 helper now compiles a list-location card whenever an index token
is invalid or out of range. The card contains the live length, valid range (or states
that no indices exist), and `index:id` mappings for identifiable rows. At the final
`set_at` token it also names `-` as the only append action. The same parser accepts only
non-negative decimal index tokens, closing the previous accidental negative-index
mutation surface.

Because materialization patches already return this exception verbatim from inside the
locked transaction, every caller receives guidance derived from the exact candidate
revision without a new schema, prompt catalog, or read tool.

## Rejected alternatives

- Adding `/-` examples only to the materializer prompt would not identify replacement
  indices and would duplicate pointer semantics outside the owning boundary.
- Returning only list length would still force row-identity guessing after inserts or
  removals.
- Allowing an index equal to length as append would diverge from RFC 6901 patch syntax
  and preserve two ways to express the same mutation.
- Exposing the whole candidate for Read would expand context and bypass the bounded
  status/patch policy.

## Validation

Unit fixtures prove that an empty-list miss names length zero and the append action; an
identified two-row miss reports range `0..1` and both `index:id` mappings; and `-1` is
rejected without mutating the last row. Existing JSON-pointer and materialization patch
fixtures remain passing.

- JSON-pointer, materialization, and tool-policy suites: `107 passed in 4.64s`.
- Full repository suite: `554 passed in 43.12s`.
- `.venv/bin/ruff check src tests`: `All checks passed!`.
- `.venv/bin/vfx --help`: exit 0.
- Producing run `20260829T105346Z-03ae9a` attempted
  `/scene_contracts/6` against a live six-row candidate at 477.6 seconds. The rejection
  named length 6, range `0..5`, all six `index:id` mappings, and `-` as the final-token
  append action. The next patch at 495.2 seconds used `/scene_contracts/-`, passed the
  pointer boundary, and reached structural validation. There was one rejected pointer
  call—the same count as an isolated baseline miss—and 17.6 seconds to correction versus
  12.1 and 11.1 seconds in the two prior guesses; the mechanism improved determinism and
  row identity without demonstrating a latency reduction.
- The same run completed Layer 2 materialization and unit-plan publication through a
  clean deterministic gate in 21 materialization turns plus four unit-planning turns.

Implementation commit: `e6ea0c4` (`fix: teach JSON pointer list locations`).

## Release and rollback

No schema or authority migration. Successful pointers retain their existing semantics;
only invalid-pointer diagnostics become more precise, and negative indices now fail
closed. Rollback would restore a hidden mutation alias and recurring list-location
guessing, so rollback is unsafe.
