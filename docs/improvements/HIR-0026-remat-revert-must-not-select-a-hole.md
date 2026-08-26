---
id: HIR-0026
title: Remat revert must not select a hole before the replacement publishes
status: accepted
introduced_in: unreleased
date: 2026-08-26
failure_class: failed_transaction_left_durable_authority
mechanism: unpublished_revert_overlay_publication_selects
adr: ADR-0004
---

# Remat revert must not select a hole before the replacement publishes

## Observed failure

Shot `vfx-test`, `vfx plan . --layer 1 --rematerialize --discard-accepted`
(`l1-remat4`, run `20260826T141502Z-3af3b7`). `revert_materialization` wrote
and **selected** a view that restored layer 1 to global deferred authority
while keeping layer 2. The session then died `process_error` / `[Errno 32]
Broken pipe` before `publish_materialization`. Live pointer
`state/jit-layers/current.json` named view `745967a6…` with
`materialized_layers: ["2"]` only. Layer 1 in that view is `jit_deferred` with
empty stages. `state/work-units/layer_1.json` still listed `camera_rig` and
`blockout_proxies` as `passed`.

The same session spawned an Explore subagent (`Task` / `Agent` /
`ListAgents` / `ScheduleWakeup`). `MATERIALIZATION_DENIED_TOOLS` was only
`Bash`, `Edit`, `Glob`, `Grep`. The child tried to Read prior runs, ADRs, and
HIRs; planner confinement denied those paths. That is not how the pointer
moved, but it is how the budget burned before the pipe broke.

## Root cause

Same class as HIR-0016: a failed transaction left durable authority. Remat
needed a design base where the replaced layer is globally deferred (or
owned-means-owed trips on the predecessor's register). The implementation
**selected** that overlay as the live pointer, then asked the model to
publish a replacement. Crash / truncation / broken pipe after select and
before publish is a hole: consumers see a layer that is no longer
materialized, while unit state still claims the discarded DAG passed.

Selecting the design base was a publication. Publication of a remat is the
replacement, not the revert.

## Decision criteria

- The live pointer moves only when the replacement validates and publishes
  (ADR-0004). Crash, truncation, SDK failure, or a broken pipe leaves the
  previously selected view.
- Remat still designs against global authority for the replaced layer.
  Sibling materializations stay in the unpublished overlay. The write-hook,
  `patch_materialization`, `gate_preview`, and `publish_materialization` read
  that overlay, not the live pointer.
- `select=True` remains the explicit-discard path used by tests that assert
  the reverted view is live.
- Empty `still_materialized` plus `select=False` still returns the written
  overlay and does not unlink `current.json`.
- Restoring discarded view `a764e394…` by editing `current.json` is not a
  repair. That view is the falsified HIR-0019 spine.
- Task/Agent are not remat repair instruments. Historical bundles, ADRs, and
  HIRs are not readable inside the confined session (HIR-0023).

## General mechanism

- `revert_materialization(..., select=False)` writes
  `state/jit-layers/views/<hash>/` and returns that directory. It does not
  write `current.json`.
- Remat passes that directory as `overlay_root` through
  `selected_view_artifact`, `_composed_documents`, `stage_candidate_view`,
  `publish_materialization`, the write-hook's `inspect_materialization`, and
  `patch_materialization`.
- Remat of an already-deferred layer (a prior attempt selected a hole)
  still enters `_rematerialize_layer` so `--discard-accepted` and
  `apply_replan` run. Unpacking the authority tuple as three values and
  skipping unit-state movement is how a replacement would publish then
  die.
- `publish_materialization` is the only select.
- `MATERIALIZATION_DENIED_TOOLS` includes `Task`, `Agent`, `ListAgents`, and
  `ScheduleWakeup`.

## Rejected patch-level alternatives

- Hand-edit `current.json` back to `a764e394…`: that re-selects the
  falsified A2 spine as a safety net.
- Leave `select=True` and "retry quickly": the next crash hollows another
  layer.
- Loosen `max_turns` or enable Glob: the session still cannot legally Read
  prior runs, and a longer walk does not make the pointer transactional.
- Detect-and-continue after a hole: consumers would still be on invalid
  authority until an operator notices.

## Validation

- `tests/unit/test_plan_records.py`:
  `test_unselected_revert_leaves_live_pointer` — after layer 2 publishes,
  `select=False` leaves `current.json` bytes unchanged, the overlay has
  layer 2 `jit_deferred`, the selected view still has layer 2 `ready`.
  Publishing the same payload against the live pointer raises owned-means-owed
  (`already resolved concretely`); publishing with `overlay_root` succeeds
  and then selects. `test_revert_materialization_restores_global_authority`
  keeps default `select=True`.
  `test_unselected_revert_of_last_layer_does_not_unlink_pointer` — reverting
  the only materialized layer with `select=False` does not unlink the
  pointer.
- `tests/unit/test_planner_outcomes.py`:
  `test_already_deferred_rematerialize_still_runs_the_transaction` — remat
  is not gated on `execution == "ready"`; the 3-unpack is gone.
- `.venv/bin/ruff check src tests`: All checks passed.
- `.venv/bin/python -m pytest -q`: 322 passed.
- `.venv/bin/python -m tests.integration.test_harness`: ALL PASS (0 failed).
- `.venv/bin/vfx --help`: ok.

Production `l1-remat5` (`975cb6`) died `error_max_turns` after writing a
candidate; this HIR held the pointer on `745967a6…`. Publication was still
attempted because `run_session` treated the mtime bump as success
(HIR-0027). Judge this HIR on the pinned tests. Do not restore
`a764e394…`.

## Release and rollback

No schema migration. Rollback is restoring `select=True` at the remat call
site and dropping `overlay_root`. That reintroduces the hole.

## Remaining limitations

- A remat of a layer that is already `jit_deferred` in the live view
  still enters `_rematerialize_layer`: discard check, unpublished overlay
  (identity for that layer), publication select, then apply_replan. The
  live view is not the only design base, and unit state is not left
  pointing at the discarded DAG.
- Denying Task does not prevent every SDK-side spawn if a future runtime
  adds another delegation name; the list is a ratchet, not a capability
  probe.
- Publication after a successful remat still discards the previous layer
  DAG; `--discard-accepted` remains an explicit operator act.
