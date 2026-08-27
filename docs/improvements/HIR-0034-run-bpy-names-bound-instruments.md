---
id: HIR-0034
title: run_bpy errors that reinvent a bound instrument name the instrument
status: accepted
introduced_in: unreleased
date: 2026-08-26
failure_class: freeform_bpy_reinvented_an_existing_check
mechanism: run_bpy_instrument_hint_on_known_reinventions
adr: null
---

# run_bpy errors that reinvent a bound instrument name the instrument

## Observed failure

Run `20260826T170413Z-ba2b4c`. The builder wrote a `BVHTree.FromMesh` nearest-point
probe to debug clearance. Blender 5.x has no `FromMesh` (the constructor is
`FromBMesh`). `path_clearance_min` already measures the same quantity
(`closest_point_on_mesh`, obstacles via `compare_roles`). `check_scene(kind='motion')`
already measures smoothness. A second clearance implementation is not a fix.

## Root cause

`run_bpy` is the hands. Rejections taught the API miss (`FromMesh`) and not the
bound instrument. Classification: missing teaching at the adapter boundary.
HIR-0018 requires naming both sides; here the missing side is the contract /
`check_scene` kind the agent should have called.

## Decision criteria

- A recurring guess is a missing tool — here the tool already exists; the
  error must name it.
- Do not add a second BVH clearance implementation in `run_bpy` helpers.
- Worker `_ATTR_HINTS` and the SDK `BlenderError` path share the same teaching.

## General mechanism

- `_run_bpy_instrument_hint` appends on `FromMesh` / `BVHTree` / `find_nearest`
  / `closest_dist` (and `to_mesh` + clearance): name `path_clearance_min` and
  `check_scene(kind='motion')`, and that `FromBMesh` is the 5.x constructor,
  not the instrument.
- Worker `_ATTR_HINTS["FromMesh"]` carries the same sentence for in-process
  attribute errors.

## Rejected patch-level alternatives

- Implement `BVHTree.FromMesh` as a compatibility shim: a second clearance
  path, and it is not the bound contract.
- Prompt "use check_scene for clearance": the error still only said
  `AttributeError`.
- Ban `run_bpy` meshes: builders still need geometry; they must not reinvent
  the measurement.

## Validation

- `tests/unit/test_builder_instruments.py`:
  `test_frommesh_error_names_path_clearance_instrument`;
  `test_unrelated_run_bpy_error_is_not_rewritten`.

## Release and rollback

No schema migration. Rollback is a bare `AttributeError`, which again invites
a second probe.

## Remaining limitations

The hint is pattern-matched on the error and script text. A legitimate
`FromBMesh` failure still sees the teaching line; that is cheap. New
reinventions (a private projection loop, a private vis ray) need their own
hint when they recur.
