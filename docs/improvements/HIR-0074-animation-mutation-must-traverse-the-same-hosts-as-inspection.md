---
id: HIR-0074
title: Animation mutation must traverse the same hosts as inspection
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: animation_host_asymmetry
mechanism: object_role_and_data_block_curve_traversal
adr: null
---

# Animation mutation must traverse the same hosts as inspection

## Observed failure

In build `20260827T223035Z-44cedd`, `list_keyframes(role='world.lighting_rig')`
correctly reported `data.energy` at frames 72 and 150. The matching helper then returned
zero for both `bvfx_interp('world.lighting_rig')` and `bvfx_interp(light_object)`. Only
the third spelling, `bvfx_interp(light_object.data)`, touched the curve.

## Root cause

Keyframe inspection deliberately traversed object and object-data animation, but the
mutation helper inspected only the exact Python value passed to it. A semantic role string
was never resolved, and an object target did not fan into its data-block. The read and write
instruments therefore disagreed about where the same observable animation lived.

## General mechanism

`bvfx_interp` now resolves a string first as an object name and then as an exact semantic
role. For every resolved object it traverses both object and data-block animation, deduplicates
hosts, and applies the requested interpolation to all discovered curves. Passing a raw
data-block remains supported. Its return count now covers the same host closure that
`list_keyframes` enumerates.

## Rejected patch-level alternatives

Adding another prompt note to pass `.data` preserves a mechanical spelling trap and does
not help role-addressed calls. Silently accepting zero would retain Bezier overshoot while
claiming success. Changing inspection to hide data curves would remove truth rather than
closing the mutation loop.

## Validation

The helper-source test pins role resolution and data-block traversal. Blender 5.2.1
producing validation keyed a Light data energy curve, called
`bvfx_interp('proof.light', mode='LINEAR')`, returned one touched curve, and read both
keyframes back as `LINEAR`.

## Release and rollback

No schema migration. Existing calls that already pass raw data-blocks retain their behavior;
object and role calls gain the previously missing curves.
