---
id: HIR-0115
title: Candidate read-back is unit scoped
status: accepted
introduced_in: unreleased
date: 2026-08-29
failure_class: candidate_probe_exposed_sibling_contracts
mechanism: compiled_unit_frame_probe_boundary
adr: null
---

# Candidate read-back is unit scoped

## Observed failure

Held-out Room 1046 run `20260829T175349Z-882888` finalized the dependency-root
`camera_target` unit after its only bound height contract had passed. The disposable
`probe_candidate` replay correctly rebuilt that Empty and owed no raster, but returned
every Layer 1 scene row at every judge frame. Its response therefore included failing
`camera.main` projection and animation contracts owned by the future `camera_path`
successor. The finalizer ignored them and published the correct target artifact, so the
run passed, but a different model could legally mistake those rows for repair authority
and mutate or reject a unit that had already satisfied its exact contract.

## Root cause

Live and canonical evidence flow through `_scope_unit_evidence`, which compiles the
active unit's binding ids at the current moment and adds only lifecycle-active geometry
visibility protections. Candidate read-back called layer-wide scene and image producers
directly and serialized their complete output. The finalizer instrument therefore had a
broader evidence boundary than the gate it was meant to explain. Classification:
bounded-context / tool-policy defect.

## Decision criteria

- A bounded unit's candidate response contains only evidence ids bound at that judge
  frame, plus typed geometry visibility protection rows and builder-state rows.
- An empty compiled id set stays empty; it never falls back to layer-wide evidence.
- A missing active unit is the sole layer/composed compatibility case and preserves the
  layer-wide stream.
- Scene and image rows share the same filter after their authoritative producers run.
- Filtering changes observation only. It grants no mutation authority and does not
  suppress a row that can block the active unit.

## General mechanism

`_unit_evidence_ids_by_frame` compiles one immutable id list for every judge using the
same `_unit_evidence_ids` authority as canonical evaluation and the existing
`_geometry_protected_vis_ids` exception. The compiled map is placed in `probe_ctx` before
the script agent starts. The disposable candidate server applies `_scope_bound_evidence`
to the union of scene and image rows before serializing them. `_scope_unit_evidence`
delegates to that same primitive, preserving the meaningful distinction between `None`
(layer-wide authority) and an empty set (bounded unit with no matching rows).

## Rejected alternatives

- Tell the finalizer to ignore sibling rows: the tool would still present them as
  authoritative findings and prompt compliance is not an evidence boundary.
- Filter by role names: contracts may bind controls, global scene state, or interfaces;
  selectors do not define evidence ownership.
- Filter only failing rows: passing sibling rows are equally outside the active context
  and could hide a missing active binding.
- Recompute bindings inside the disposable worker: selected WorkUnit authority already
  compiled the exact boundary before the session and must not be rediscovered from scene
  state.

## Validation

- `test_candidate_probe_evidence_is_exactly_unit_and_frame_scoped` proves two different
  judge moments compile different exact id sets, a successor failure is absent,
  builder-state survives, an empty set does not widen, and `None` alone preserves the
  layer-wide compatibility path.
- Fresh held-out producing run `20260829T182128Z-6c2496` rebuilt Layer 1 after an
  audited invalidation of `camera_target` and its `camera_path` dependant. The target
  finalizer's candidate response contained exactly one evidence row,
  `camera-target-height=5.5` passing `3..8`; none of the successor's projection,
  animation, or derivative rows appeared. The target canonical gate passed at 5.0.
  `camera_path` then passed 23/23 live contracts, all seven canonical frames at 5.0,
  and the composed empty-scene replay passed all seven frames. The run exited 0 and
  published Layer 1.

Implementation commit: `80e0039` (`fix: scope candidate evidence to active unit`).

## Release and rollback

No persisted schema changes. `probe_ctx` is process-local and the candidate MCP remains
session-scoped. Rollback would again give finalization and repair more evidence authority
than the active unit.

## Remaining limitations

The probe still evaluates layer producers before filtering; this keeps one canonical
metric implementation and avoids a second selector engine. It may cost deterministic
measurement time, but out-of-scope rows no longer enter model context or authorize work.
