---
id: HIR-0177
title: Materialization budgets scale with owned requirements and patches address rows by id
status: accepted
introduced_in: unreleased
date: 2026-09-03
failure_class: materialization_budget_exhaustion
mechanism: proportional_turn_budget_and_id_addressed_patch_pointers
adr: null
---

# Materialization budgets scale with owned requirements and patches address rows by id

## Observed failure

Run `20260903T002758Z-1b6807` on `artifacts/room_1046_opening` (main `62fd434`): the
layer-2 materialization session (four units, 21 owned requirements) hit its 24-turn cap
after 12 staging calls, 6 patches, and 16 rejections, with a candidate of nine blocking
findings and nothing attested. The run stopped with the typed `authority_defect`
envelope naming a new validated candidate as the action, exactly as designed, and the
$3.39 session produced no authority. Three of the rejections were JSON-pointer index
misses on lists the materializer had just grown (`index 999`, `index 7 of 7`,
`index 13 of 13`); the miss card named the ids, and the next patch still had to guess.

Two further teaching-by-rejection rounds recurred across the same lineage:

- Three layer-2 sessions (runs `1b6807`, `e20545`'s successor, `6316b4`) each staged a
  claim with `authority: qualified_qualitative_required` and paid a rejection
  ("qualification must be an object ... rubric-file-backed"). No qualification suite
  registers a rubric artifact an authored claim could cite; only the harness mints that
  authority, for provisional judgment debt.
- Run `20260903T023810Z-8509f9` (layer-1 rematerialization) staged `camera_targets`
  cleanly and learned at its first `finalize_materialization` that `camera.targets`
  had no required claim. The predicate is unit-local, but only claim closure at the
  terminal gate applied it.

## Root cause

1. `generate_layer_plan` passes a fixed `max_turns=24` into every materialization session
   regardless of the sparse row: a two-unit camera layer and a four-unit, 21-requirement
   form layer had the same cap, and each staged unit costs roughly three turns plus a
   teaching rejection.
2. `patch_materialization` pointers addressed list rows only by index. After every
   staging call the lists change length, so the materializer had to re-derive indices
   from status output or guess; HIR-0105 taught the ids in the miss but offered no way
   to use them.
3. `work_unit_authoring_schema` enumerated every parser authority, including one no
   authored claim can satisfy; the schema advertised an option the shot cannot pay.
4. The staging transaction enforced write-cluster atomicity, dressing, projection
   ownership, and vocabulary predicates per unit, but the equally unit-local
   "every mutated role needs a required claim" predicate lived only in
   `evidence/claim_evidence.claim_closure`, so it fired one finalize round late.

## Decision criteria

- Budgets scale with the active delta the session must author, not with the shot
  (AGENTS.md: choose turn budgets for the role and measured uncertainty).
- A cap remains a cap: an exhausted session still publishes nothing.
- Enumerate, don't imagine: the row id the harness already prints is the address.

## General mechanism

1. `agents/planner/budget.materialization_turn_budget(requested, owned_requirements)`
   returns the larger of the requested cap and `24 + 2 × owned requirements`, capped at
   96; `_materialize_deferred_layer` derives the owned count from the selected bundle's
   sparse row and logs the budget in the kickoff. Rematerialization shares the path.
2. `domain/json_pointer` accepts `id=<row id>` as a list token for get and set; a miss
   names the indexed ids, an ambiguous id refuses, and the location card advertises the
   form. The patch tool description names it.
3. `domain/work_units/parsing.STAGEABLE_CLAIM_AUTHORITIES` (`executable_required`,
   `advisory`) is the authority enum the staging schema offers; its description names
   why `qualified_qualitative_required` is harness-minted, and the materialization
   kickoff states the same next to the judgment-property authority line. The parser
   still accepts harness-minted qualified claims.
4. `domain/work_units/graph.uncovered_mutation_roles` is the one coverage predicate:
   `claim_closure` reports it as before, and `_validate_local_staged_units` refuses the
   stage call with the uncovered roles, the unit's required-claim subject roles, and
   `MUTATION_CLAIM_COVERAGE_RULE`.

## Rejected patch-level alternatives

- Raising the fixed cap for every layer (spend without proportion, and still a guess).
- Letting an exhausted session publish a partial candidate (max-turns is a failed
  transaction, HIR-0027).
- Teaching the materializer to re-read `materialization_status` before each patch
  (prompt wording for a missing address form).

## Validation

- `src/tests/unit/test_materialization_turn_budget.py`: floor, scaling, requested cap,
  ceiling, rejection of a non-positive request.
- `src/tests/unit/test_json_pointer.py::test_list_tokens_may_name_a_row_by_id`.
- Live: the next layer-2 materialization of the same shot runs under a 66-turn budget
  (run `6316b4` kickoff: 66 turns; four units and 21 requirements staged in 18 minutes).
- `src/tests/unit/test_materialization_kickoff_blocks.py::test_staging_schema_offers_only_payable_authorities`.
- `src/tests/unit/test_staging_claim_coverage.py`: the predicate matches closure
  semantics, and the stage call refuses an uncovered mutated role without writing the
  candidate while the covered unit stages.

## Release and rollback

Unreleased; no schema change. Rolling back restores the fixed cap and index-only
pointers.

## Remaining limitations

- The budget scales with owned requirements, not with the units the session will
  choose; a layer that splits into many small units may still exhaust the ceiling.
- A partially staged candidate is not resumed by the next session; ADR-0010's
  controller may add candidate resume once its receipt exists.
