---
id: HIR-0133
title: Direct materialization reconciles durable unit state
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: direct_materialization_published_then_hit_stale_unit_state
mechanism: selected_jit_digest_backed_state_reconciliation
adr: ADR-0004
---

# Direct materialization reconciles durable unit state

## Observed failure

Room 1046 run `20260830T053936Z-8c05a3` invoked the documented bounded command
`vfx plan … --layer 2`. Materialization published a clean three-unit Layer 2 view
`eeb60644…` against the selected sparse bundle, then `generate_layer_plan` called
`unit_state.initialize`. Durable Layer 2 state still named ten prior-generation facade
units at plan hash `fc5ad84b…`; the selected replacement named `building_mass`,
`building_roof`, and `ground_site`. Initialization failed closed with:

```text
work-unit state IDs do not match the active layer DAG; apply a transactional replan
```

No builder ran, but the clean materialized view was already selected and the ordinary
public command had no automatic state-move path.

## Root cause

HIR-0102 implemented the correct digest-bound `apply_replan(state_backed_base=True)`
mechanism inside `_rematerialize_layer`. `generate_layer_plan` only entered that function
when the caller supplied explicit `--rematerialize` authority. A first materialization of
a `jit_deferred` row used `_materialize_deferred_layer` directly and then unconditionally
initialized state. The same gap prevented a plain rerun from recovering a process death
between publication and state movement.

The predecessor identity was not missing: current-schema durable state carried the old
plan hash plus every exact unit digest. The selected JIT view was not a candidate: it had
passed materialization validation, terminal attestation, and the independent plan gate.

## Decision criteria

- Before initialization, every selected materialized layer compares its typed unit ids
  and digests with durable state.
- Matching identity is a no-op; `initialize` retains HIR-0040 plan-hash adoption.
- A mismatch with populated current-schema state moves through
  `apply_replan(state_backed_base=True)` using durable state as old authority and the
  selected JIT DAG as replacement authority.
- Matching digests preserve checkpoints; changed/removed ids are superseded and the
  replacement downstream closure starts pending. Accepted removal is a validated DAG
  effect, not an orphan and not `--discard-accepted`.
- Missing/cross-schema hashes still fail closed. No state file is deleted or hand-edited.
- The same step recovers a previous publication whose process died before state movement.

## General mechanism

`_reconcile_materialized_layer_state` loads durable state, calls `validate_current`, and
returns immediately for matching identities. On mismatch it calls the existing audited
state-backed `apply_replan` transaction with the durable `plan_hash`, the active selected
view hash, fixed harness ownership, and state/JIT evidence locators. The helper runs after
materialization/rematerialization and before `initialize` for every direct layer-plan
invocation.

The helper does not design or publish authority and does not infer an old DAG from files,
transcripts, or recency. It consumes only the exact state digests already accepted by
HIR-0102 and the selected materialized WorkUnits.

## Rejected alternatives

- Require the operator to rerun with `--rematerialize`: the documented legal first
  materialization command already selected valid authority, and repeating model design is
  not a state transaction.
- Reinitialize or delete Layer 2 state: this discards accepted history and checkpoints.
- Use `--discard-accepted`: current-schema digests are comparable, so the heavy wipe door
  is neither necessary nor legal.
- Infer the prior DAG from old JIT views or transcripts: durable state is the singular old
  identity.
- Move state before publication: an invalid/unpublished candidate cannot supersede
  accepted work.

## Validation

Focused fixtures reproduce a selected replacement over passed prior-generation units and
prove the direct helper records removed, added, superseded, and pending identities through
state-backed `apply_replan` without discard. A matching-digest fixture proves the helper
does not mutate state, leaving HIR-0040 adoption to `initialize`.

Validation on 2026-08-30: focused planner outcome, unit administration, and staged
architecture suites passed 62 tests in 4.38 seconds; the full repository suite passed
628 tests in 43.24 seconds. `.venv/bin/ruff check src tests` passed.

Production validation is the next bounded `vfx plan … --layer 2`: it must reconcile the
already selected clean Layer 2 view, preserve the supersession audit, and continue to the
first ready unit plan without rematerializing or touching Layer 3.

## Release and rollback

No schema migration. Rollback restores a deterministic publish-then-initialize failure
whenever a sparse selected generation meets prior durable state, so rollback is unsafe.

## Remaining limitations

Publication and state movement remain two ordered atomic writes. This mechanism makes the
gap idempotently recoverable; a future multi-file transaction could eliminate the gap
instead of recovering it.
