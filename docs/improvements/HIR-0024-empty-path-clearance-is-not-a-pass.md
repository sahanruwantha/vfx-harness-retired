---
id: HIR-0024
title: Empty path clearance is not a passing collision proof
status: accepted
introduced_in: unreleased
date: 2026-08-26
failure_class: unmeasured_clearance_sentinel_sealed_as_pass
mechanism: empty_obstacle_selection_fails_closed
adr: null
---

# Empty path clearance is not a passing collision proof

## Observed failure

Shot `vfx-test`, layer 1, run `20260825T143912Z-0b5ab4`. A `path_clearance_min`
collision row sealed at **1e9**. The probe's empty-obstacle branch assigned the
sentinel `1e9` and `_holds` treated it as a real minimum distance, so every
`op: min` bound held. The contract claimed clearance from mesh the selection
never found. Persistent lifecycle was the documented excuse: HIR-0017 said empty
"reads vacuously clear until obstacle geometry exists."

## Root cause

Absence was encoded as an enormous passing number. That is the same class as
HIR-0014 (required evidence passing by silence) and the inverse of HIR-0019
(nothing-on-screen is a failing 0.0, not an instrument success). Classification:
evidence / fail-closed gap. `lifecycle: persistent` correctly re-evaluates the
row as later layers add geometry; it does not authorize a PASS against nothing.

## Decision criteria

- Absence fails closed. An empty `compare_roles` match is not a clearance
  measurement.
- Rejections teach: the miss names the requested obstacle selector and the mesh
  roles that exist (HIR-0018).
- Make the failure unrepresentable: authoring refuses a bound at or above the
  sentinel, and a leaked 1e9 reading cannot `_holds`.
- Persistent re-evaluation stays. It is a later measurement, not a current PASS.

## General mechanism

- The probe raises when no mesh matches `compare_roles` (both-sides miss) or
  when matched meshes produce no `closest_point_on_mesh` distance. A real
  distance is the only value.
- `_evidence` and `_holds` refuse `value >= 1e9` even if a stale reading
  carries the sentinel.
- `validate_row` refuses `min` with `lo<=0` or `lo>=1e9`, and `max` with
  `hi>=1e9`.
- Vocabulary copy (kind definition, `evidence_vocabulary` note) no longer
  teaches empty=clear.

## Rejected patch-level alternatives

- Keeping 1e9 and documenting "ignore it until obstacles exist": detect-and-continue;
  sealing already treated it as proof.
- Returning 0.0 for empty: fails `min lo=0.5` but PASSes a `max` bound and
  looks like contact.
- Loosening the collision threshold so 1e9 is out of band: the sentinel would
  still be a number, and the next bound would absorb it.

## Validation

- `tests/unit/test_evidence_vocabulary.py`: `_holds(1e9)` is false; `_evidence`
  on a 1e9 reading is not a pass and names the sentinel; the probe no longer
  assigns `1e9` on empty and names present mesh roles; `lo=0`, `lo=1e9`, and
  `hi=1e9` are vacuous at authoring.
- A measured clearance against real mesh is unchanged: the probe still returns
  `best` when `closest_point_on_mesh` hits.
- `.venv/bin/ruff check src tests`: All checks passed.
- `.venv/bin/python -m pytest -q`: 315 passed.
- `.venv/bin/python -m tests.integration.test_harness`: ALL PASS (0 failed).
- `.venv/bin/vfx --help`: ok.

## Release and rollback

No schema migration. Existing sealed 1e9 rows fail on the next evaluation —
that is the point. Rollback is restoring the sentinel assignment.

## Remaining limitations

- This does not add a critic/image completeness gate (P3-c) or feasibility
  lint for other kinds.
- Production layer 1 still has to rematerialize (HIR-0019 / `l1-remat4`) before
  a new collision row is judged; this HIR only stops the sentinel from counting
  as a pass when that evaluation runs.
