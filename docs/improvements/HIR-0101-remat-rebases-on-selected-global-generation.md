---
id: HIR-0101
title: Rematerialization rebases on the selected global generation
status: accepted
introduced_in: unreleased
date: 2026-08-29
failure_class: superseded_view_used_as_remat_base
mechanism: selected_generation_materialization_base
adr: ADR-0004
---

# Rematerialization rebases on the selected global generation

## Observed failure

Global planning run `20260829T080812Z-2ff54e` published bundle
`1d3b117b174814d2…`, replacing bundle `088b4a7ce199ed2b…`. The live JIT
pointer still named the old bundle and its Layer 1/2 materialized view, which is
legal superseded state until a replacement publishes. Layer 1 rematerialization
run `20260829T082822Z-65c13c` failed before model work:

```text
TypeError: expected str, bytes or os.PathLike object, not NoneType
```

`revert_materialization` asked `selected_view_artifact` for artifacts pinned to
the new bundle. The resolver correctly returned `None` because the live view
belonged to the previous generation; remat then called `Path(None)` while
constructing its unpublished design overlay. The run spent zero model turns and
published no replacement.

## Root cause

The normal plan consumer already implements the authority rule: a JIT view
pinned to another immutable bundle is inert, so consumers read the newly
selected bundle's sparse artifacts until that generation publishes a matching
materialized view. `_composed_documents` implemented the same fallback locally.
`revert_materialization` bypassed both and assumed that a live JIT pointer must
match the currently selected global bundle.

This is an orchestration/authority-resolution defect. A global plan pointer and
a JIT view pointer change in separate atomic transactions by design; the period
between them is representable and must have one deterministic base.

## Decision criteria

- Materialization and rematerialization resolve their base through one function.
- A matching JIT view remains the base for same-generation incremental work.
- A view pinned to another bundle contributes no bytes to the new generation;
  the verified selected sparse bundle is the base.
- `select=False` remat still leaves the live JIT pointer byte-identical until the
  replacement validates and publishes (HIR-0026).
- Malformed or stale same-generation JIT authority still fails closed; only an
  explicitly different immutable bundle identity is superseded state.
- No prior materialization, unit status, run output, or legacy shot-root file is
  copied into the new generation by proximity.

## General mechanism

`_consumer_base_artifact` is the single materialization-base resolver. It asks
`selected_view_artifact` for a matching verified view (or an explicit unpublished
remat overlay), and otherwise returns the already hash-verified selected bundle
member. Both `_composed_documents` and `revert_materialization` use it, so an
intentionally absent superseded-view result cannot reach `Path`.

Remat after global republication therefore writes an unpublished overlay derived
from the selected bundle's deferred authority. The old live pointer stays selected
until `publish_materialization` atomically selects the replacement.

## Rejected patch-level alternatives

- Repointing `state/jit-layers/current.json` to the new bundle before design would
  recreate HIR-0026's publication hole.
- Preserving old view rows under the new bundle hash would mix immutable
  generations and let stale ownership/contracts regain authority.
- Catching `TypeError` and retrying from bundle files would make authority depend
  on an exception path rather than one resolver.
- Deleting the old view pointer or hand-editing work-unit state would destroy
  audit evidence and bypass `apply_replan`.

## Validation

- `tests/unit/test_plan_records.py::test_unselected_revert_rebases_on_republished_bundle`
  publishes and materializes one generation, republishes a different bundle, then
  proves that unselected remat uses the new sparse authority while the old live
  pointer stays byte-identical.
- Existing HIR-0026 regressions prove same-generation remat still preserves the
  live pointer and that reverting the last layer with `select=False` does not
  unlink it.
- Targeted pointer/rebase result: `3 passed in 0.64s`.
- Relevant authority/materialization suite: `116 passed in 5.56s` before the
  lifecycle follow-up, then `163 passed in 7.21s` with both mechanisms present.
- Full repository suite: `549 passed in 43.61s`.
- `.venv/bin/ruff check src tests`: `All checks passed!`.
- `.venv/bin/vfx --help`: exit 0.
- Producing rerun `20260829T083336Z-91fc7b` executed the formerly crashing
  cross-generation boundary at time 0, wrote unpublished overlay `5a0048af…`
  without changing the old live pointer, materialized and gate-published clean
  replacement view `6c51b097…`. It used 12 materialization turns, 463.9 model
  seconds, and $1.3380 before exposing the distinct state-movement defect recorded
  as HIR-0102. Thus the original 0-turn `Path(None)` failure is removed on the
  producing path rather than hidden by a unit test.
- Final rematerialization `20260829T084759Z-067de2` again used the unpublished
  overlay path and completed successfully.

Implementation commit is recorded after the verified diff is committed.

## Release and rollback

No schema migration. Rollback restores a guaranteed process error whenever a
new global plan is selected while an older JIT view remains live, so rollback is
unsafe.

## Remaining limitations

The replacement materialization still has to move durable unit state through
`apply_replan`. This mechanism chooses the correct design base; it does not
preserve changed unit digests or make superseded sibling materializations valid
under a new global DAG.
