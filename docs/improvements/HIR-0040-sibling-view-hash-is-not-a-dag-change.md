---
id: HIR-0040
title: A sibling rematerialization's layers.json hash is not a DAG change
status: accepted
introduced_in: unreleased
date: 2026-08-27
failure_class: combined_view_hash_blocked_unchanged_layer
mechanism: initialize_adopts_plan_identity_when_unit_digests_match
adr: null
---

# A sibling rematerialization's layers.json hash is not a DAG change

## Observed failure

`vfx build /home/sahan/Desktop/vfx-test --layer 2` after layer 1 passed on
`20260827T034423Z-f1004f` (view `641fa0bd`) raised:

```text
ValueError: work-unit plan changed; apply a transactional replan instead of reinitializing
```

Layer 2 durable state still named plan hash `36ed1465` (view `a764e394`).
The selected view's `layers.json` hashed to `a257a7e5`. Layer 2 unit IDs and
`unit_digest` values were identical across those views; JSON for layer 2 was
byte-equal. Three units were `passed` (`materials_energy`, `detail_instancing`,
`lighting_bloom`); `atmosphere` was `failed`.

## Root cause

`plan_hash` is sha256 of the selected combined `layers.json`. Rematerializing
layer 1 publishes a new view that includes every already-materialized sibling.
`initialize` treated any hash mismatch as a DAG change after `validate_current`
had already proved IDs and digests match. Classification: wrong identity at
the work-unit state boundary.

`vfx units replan` against the sparse global bundle sees an empty base DAG
(unit-first). That path marks every current unit `added` and resets slots to
`pending`, including accepted checkpoints. That is the wrong transaction for
an unchanged unit DAG.

## Decision criteria

- When `validate_current` passes and only `plan_hash` differs, adopt the new
  hash through `apply_replan` with old units equal to new units (preserve-all).
- Passed, failed, and other statuses stay. Revision and a replan audit row
  record the identity change.
- ID mismatch, digest mismatch, or a different `digest_schema` still fail
  closed for an explicit DAG replan.
- Do not hand-edit `state/work-units`, and do not empty-base-replan an
  identical DAG.

## General mechanism

`initialize` calls `apply_replan` with the live unit tuple on both sides when
the selected `layers.json` hash changed and this layer's unit DAG did not.

## Rejected patch-level alternatives

- Hand-edit `layer_2.json` `plan_hash`: forbidden durable-state rewrite.
- `vfx units replan` on the published bundle: empty base DAG would add every
  unit and drop three passed checkpoints.
- `--discard-accepted` and rebuild layer 2: discards proven work the DAG did
  not invalidate.
- Per-layer file hashes as a follow-up identity: larger than the failing
  boundary; adoption at `initialize` is the transaction the raise already
  demanded.

## Validation

- `tests/unit/test_unit_admin.py`:
  `test_initialize_adopts_layers_hash_when_unit_dag_is_unchanged`,
  `test_initialize_still_refuses_digest_mismatch_as_replan`.
- Existing `test_populated_state_still_fails_closed_without_a_replan` still
  covers ID mismatch.

## Release and rollback

No schema migration. Rollback restores the raise on any `plan_hash` mismatch,
which again blocks an unchanged sibling after rematerialization.

## Remaining limitations

Preserving a sibling's passed checkpoints does not re-evaluate those scripts
against a rematerialized upstream layer. A later composition miss is a
revalidation question, not permission to reset the DAG here. Consuming a
JIT-layer falsification against the view identity, without empty-base
add-all, is HIR-0049.
