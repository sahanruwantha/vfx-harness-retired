---
id: HIR-0056
title: In-scope abstention must route to the semantic fault owner
status: proposed
introduced_in: unreleased
date: 2026-08-27
failure_class: incomplete_falsification_closure
mechanism: validated_fault_owner_seeds
adr: null
---

# In-scope abstention must route to the semantic fault owner

## Observed failure

Layer 2 detail proved that its f150 beauty was flat `254.9±0.3` under the accepted
`lighting_bloom` checkpoint and recorded `cannot_express_in_scope`. The immutable finding's
`affected` list nevertheless contained only `detail_scale_hierarchy_instancing`. A public
`vfx units replan --preview --falsification ...` therefore proposed preserving the checkpoint
whose sun and Glare controls caused the measured floor.
The checkpoint invalidation closure then exposed a second authority gap: detail inherited lighting
because serialized replay ran it first, but its published `depends_on` named only material. The
runtime pixel dependency was absent from the DAG, so invalidating lighting had no structural path
to detail.

## Root cause

`cannot_express_in_scope` could name contracts and prose, but not a typed semantic fault owner.
`record_hypothesis_falsification` always seeded invalidation from the active unit and its downstream
closure. Natural-language ownership evidence was deliberately not parsed, leaving no way to route
an upstream fault without a separate operator invalidation.
The plan also modeled two pixel-coupled units as siblings even though one consumed the other's
composed plate.

## General mechanism

The active scope card enumerates dependency-ordered `fault_owner_options` with unit ids and their
mutation roles/controls. `cannot_express_in_scope` accepts optional `fault_owner_units`, rejects ids
outside that compiled set, and preserves them in the typed finding. Falsification computes the full
downstream closure from the active unit plus those validated owner seeds. Existing passed checkpoints
remain accepted until the public replan transaction consumes the finding and atomically invalidates
the complete closure. Materialization must encode an observed runtime product dependency as a
`depends_on` edge; serialization order is not dependency authority.

## Rejected patch-level alternatives

Parsing owner names from the abstention reason is heuristic authority. Always reopening every prior
unit discards unrelated proof. Retrying only the active unit repeats a measured impossibility.

## Validation

Focused tests must prove unknown owner ids fail closed, a passed upstream checkpoint remains intact
while the finding is recorded, and replan preview includes the owner plus its downstream closure.
The already-recorded Layer 2 finding predates this schema and is recovered with an explicit audited
checkpoint invalidation; generated finding state is never hand-edited.

## Release and rollback

This adds an optional field to the live tool payload and finding construction, not a persisted schema
version. Rollback removes owner seeding; existing findings remain readable because the new field is
compiled into `affected`, which was already authoritative.
