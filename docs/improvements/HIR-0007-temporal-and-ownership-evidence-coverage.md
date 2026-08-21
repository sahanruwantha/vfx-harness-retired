---
id: HIR-0007
title: Add temporal evidence primitives and static coverage warnings
status: accepted
introduced_in: unreleased
date: 2026-08-21
failure_class: motion_claims_backed_by_static_evidence
mechanism: typed_temporal_contracts_and_coherence_gate
adr: ADR-0003
---

# Add temporal evidence primitives and static coverage warnings

## Observed failure

The shot's onset ordering, radial convergence, return transform, and final stillness were backed
by static object counts. Those checks can pass while the animation is wrong. Camera units also
owned multiple judge frames without projected composition contracts, and controls had no typed
relationship to the roles they govern.

## Root cause

The work-unit schema could declare `temporal_evidence: motion`, but the scene-check registry had
no temporal kinds and the gate did not verify that motion claims bound temporal evidence.
Composition and ownership coverage were prompt requests without deterministic visibility.

## General mechanism

- Add generic `onset_order`, `radial_distance_trend`, and `transform_return_delta` scene kinds.
  They operate on semantic roles and declared frame windows.
- Add rendered `frame_delta` evidence for holds and exact locks.
- Block a motion work unit that binds no temporal executable contract.
- Warn per judge frame when a camera/composition/framing owner has no projected bbox contract.
- Add optional typed `control_roles` mappings to mutation scope and warn when declared controls
  remain unmapped.
- Warn when at least 90% of four or more image checks route faults to one finishing layer despite
  multiple owner layers.

## Rejected alternatives

- Treat `animation_count` as motion proof: it proves only that animation data exists.
- Make bbox coverage universally blocking: POV, environment, and deliberately abstract frames may
  have legitimate non-bbox composition authority.
- Infer control ownership from names: semantic labels are identifiers, not a relationship graph.

## Validation

Unit tests validate every temporal schema, compile the generated Blender probe, execute a
two-frame rendered delta against a fake session, enforce temporal binding, and exercise the
composition and ownership warnings.

## Remaining limitations

Temporal probes are validated outside Blender in the unit suite; end-to-end Blender execution is
covered when a build first activates these contracts. Composition and missing control mappings
remain warnings so heterogeneous shots can adopt the stronger coverage without a blind migration.
