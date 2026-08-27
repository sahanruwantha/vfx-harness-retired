---
id: HIR-0049
title: A JIT-layer falsification identity is the view hash, not the sparse bundle
status: accepted
introduced_in: unreleased
date: 2026-08-27
failure_class: jit_falsification_identity_used_sparse_bundle_hash
mechanism: view_plan_hash_identity_and_finding_reopen
adr: null
---

# A JIT-layer falsification identity is the view hash, not the sparse bundle

## Observed failure

HIR-0048's documented resume is `vfx units replan --falsification
hf-92d9d1904858b85d9198` then a fresh `vfx build --layer 2`. The public
command failed closed before any state transaction:

```text
falsification identities do not match the explicit base bundle
```

Finding `identities.plan_hash` is `493d31b8…`, which is sha256 of the
selected view `layers.json` and of durable `state/work-units/layer_2.json`
`plan_hash`. The named global bundle's `layers.json` hashes to `40e893d8…`
and layer 2 has empty stages (`jit_deferred`). `unit` is therefore also
absent from the bundle DAG, so the unit-identity check would fail next.

Layer 2 statuses were `materials_energy=hypothesis_falsified` and the three
dependants `blocked`. None were `passed`.

## Root cause

`record_hypothesis_falsification` pins `plan_hash` from durable state, which
under unit-first authority is the materialized view hash. `_replan` compared
that to sha256 of the explicit bundle file *before* the deferred-base
substitution that already treated state `plan_hash` as the truthful old
identity. Classification: wrong identity at the falsification consumption
boundary, the same class as run 20260825 hashing bundle vs view for
`new_plan_hash` (`active_plan_hash`).

A second defect sits behind that raise: empty-base `replan_effects` against
a sparse bundle marks every current unit `added`. That resets passed
siblings, which HIR-0040 already forbade. Consuming a typed finding is not
a DAG change and is not permission to empty-base-replan.

## Decision criteria

- Bundle identity stays `finding.bundle_hash == named bundle content_hash`.
- When the named bundle layer has no stages and durable state has units,
  `finding.plan_hash` is durable state `plan_hash` (the view), not the
  sparse file.
- Unit identity in that case is the selected view's unit digest.
- When that view DAG still matches durable state, it is the old DAG for
  the diff. Empty-base remains only when IDs or digests actually diverge.
- Consuming a finding unions `unit` and `affected` into the invalidation
  closure even when digests are unchanged. `vfx units retry` still does
  not accept `hypothesis_falsified`.
- Do not hand-edit the finding or `layer_2.json`.

## General mechanism

`_replan` loads durable state before identity checks. Deferred layers
compare the finding to the view identity, use the matching view as
`old_units` when `validate_current` passes, and pass `reopen` into
`apply_replan`. Unrelated passed units stay preserved.

## Rejected patch-level alternatives

- Rewrite the finding's `plan_hash` to the sparse file hash: the record
  would no longer match the state it was sealed against.
- `--evidence` without `--falsification`: skips identity verification and
  still empty-base-replans.
- `--discard-accepted`: discards proven work the DAG did not invalidate.
- Rematerialize layer 2 so a digest change looks like a DAG amendment:
  the published claims did not change; 0048's payer is a build-time card.
- `vfx units retry` on `hypothesis_falsified`: that status only
  transitions to `superseded`.

## Validation

- `tests/unit/test_unit_admin.py`:
  `test_public_replan_consumes_jit_falsification_against_view_identity`
  (sparse bundle vs view hash; passed sibling preserved),
  `test_public_replan_reopens_falsified_unit_when_dag_bytes_are_unchanged`
  (same-digest DAG still reopens the finding's unit).
- Existing `test_public_replan_consumes_exact_typed_falsification` still
  covers an amended Layer 1 DAG plus `--hard-constraint-approval`.

## Release and rollback

No schema migration. Rollback restores the pre-substitution identity
check, which again makes JIT-layer `--falsification` unexecutable.

## Remaining limitations

Empty-base still applies when `validate_current` fails (a real unit-DAG
change on a JIT layer). Composed mixed-look critic remains HIR-0039.
The on-disk finding may still carry `check:`-prefixed `contract_ids`;
readers strip them (HIR-0048).
