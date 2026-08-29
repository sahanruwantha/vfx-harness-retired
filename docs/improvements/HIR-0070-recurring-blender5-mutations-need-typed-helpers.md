---
id: HIR-0070
title: Recurring Blender 5 mutations need typed helpers
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: blender_api_rediscovery_loop
mechanism: closed_loop_light_and_vector_blur_helpers
adr: null
---

# Recurring Blender 5 mutations need typed helpers

## Observed failure

Run `20260827T213426Z-dd2f26` spent four authored attempts discovering that Blender 5 compositor
groups use Group Output, Vector Blur's Samples/Shutter are sockets, the depth input is `Depth`,
and Render Layers must be refreshed after enabling the Vector pass. It then failed converting a
POINT light because the pre-conversion Python reference retained its `PointLight` RNA subtype.
The builder also attempted a blocked read-only Python loop solely to learn object material slots.

## Root cause

The harness documented Blender 5 differences but still required the model to compose coupled API
operations correctly. These operations have one mechanical answer and matching read-back, so
free-form adaptation was the wrong boundary.

## General mechanism

`bvfx_vector_blur` creates or reuses the compositor group, declares its Image interface, enables
Vector/Depth passes, refreshes Render Layers when necessary, sets socket values, wires all four
links, tags the semantic node, and returns it. `bvfx_light` creates or reconfigures a semantic
local light, re-fetches its data-block after type conversion, applies only valid type-specific
properties, optionally aims it, and preserves role/control authority. `inspect_scene` material
rows now enumerate object slot consumers directly.

## Rejected patch-level alternatives

More Blender API prose still leaves ordering and partial-write behavior to recall. Adding more
exception hints spends at least one mutation per known mistake. A shot-specific compositor script
would not generalize to another unit or role.

## Validation

`test_blender5_helpers_encode_closed_loop_light_and_vector_blur` pins the coupled invariants and
`test_material_status_names_object_slot_assignments` pins the read-only assignment instrument.
A Blender 5.2 producing probe converted POINT→SPOT, set type-specific fields, created Vector Blur
with `Samples=16` and `Shutter=0.5`, and read back four valid links.

## Release and rollback

No persisted schema migration. Helper signatures are compiled into every active unit scope card.

