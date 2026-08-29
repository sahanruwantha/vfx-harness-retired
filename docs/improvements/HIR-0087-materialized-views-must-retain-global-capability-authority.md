---
id: HIR-0087
title: Materialized views must retain global capability authority
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: materialized_view_forgets_global_interface
mechanism: dual_sparse_and_execution_layer_read
adr: ADR-0005
---

# Materialized views must retain global capability authority

## Observed failure

Layer-1 materialization run `20260829T045319Z-cee4af` correctly fulfilled the selected
global camera promise with a `camera_path_curve` unit mutating `camera.main`. The
materialization validator passed. Its terminal preview then reported six
`global-capability` blockers, claiming that Layer 1 omitted `jit.provides` and that all
four layers were judged before a camera existed.

The candidate still contained `jit.provides: {"camera": ["camera.*"]}` and the selected
global bundle contained the same typed declaration. The cumulative materialized view did
not: replacing the deferred Layer-1 row with a ready execution row deliberately removed
`jit`, because the ready-layer schema forbids deferred planning fields. The gate then
mistook that execution row for global ownership authority.

## Root cause

Schema 5 has two simultaneous layer projections after materialization: immutable sparse
global authority (DAG, reserved roles, capabilities, owned requirements) and cumulative
ready execution authority (units and claims). HIR-0086 added the capability predicate but
read it from the latter projection. Materialization therefore erased the predicate from
the gate's input even though the selected global bundle remained authoritative.

## General mechanism

The deterministic gate now resolves global capability closure and reserved namespaces
from the hash-verified selected sparse bundle. It continues to read units, claims, and
concrete contracts from the cumulative materialized consumer view. A run-scoped consumer
marker is accepted only when its shot resolves to the same selected bundle root and
content digest; malformed, stale, or mismatched markers fall back to the existing
fail-closed reading.

This does not copy `jit` onto a ready row or create a second lifecycle. It makes the two
existing projections explicit at their respective decision boundaries.

## Rejected alternatives

Keeping `jit` on ready rows violates the strict execution schema and conflates deferred
planning state with executable units. Copying only `provides` into a new ready-row field
duplicates immutable authority and creates a digest-consistency problem. Suppressing the
gate finding for materialized ids would also suppress real capability loss.

## Validation

Fail-without regression fixtures materialize both a product-camera namespace and a motion
rig namespace. In each case the ready overlay has no `jit` block, while the gate recovers
the role-bound camera capability from the verified sparse bundle and emits no
`global-capability` finding. Existing tamper and stale-marker behavior remains fail closed.

On the production shot, the selected materialized view produced six blockers before this
mechanism. The same selected bytes must retain only the expected stale-unit-state finding
after the fix.

## Release and rollback

No schema migration is required. The change corrects which already-authoritative
projection supplies global predicates. Rollback would make every valid
capability-producing materialization unpublishable and is unsafe.
