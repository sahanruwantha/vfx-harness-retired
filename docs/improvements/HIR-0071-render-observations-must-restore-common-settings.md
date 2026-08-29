---
id: HIR-0071
title: Render observations must restore common settings
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: read_only_render_state_leak
mechanism: common_render_state_transaction
adr: null
---

# Render observations must restore common settings

## Observed failure

In retry `20260827T215915Z-ac43b0`, `render_frame(mode='solid')` changed the live engine from
EEVEE to `BLENDER_WORKBENCH` and left it there. The next authored `bvfx_vector_blur` call enabled
the Vector pass but a Workbench Render Layers node could not expose the Vector socket, so the
otherwise valid typed helper failed. Earlier sessions had noticed the leaked engine and manually
set it back, turning a read-only observation into hidden mutation work.

## Root cause

Extended render modes registered local undo thunks, but the common `h_render` path directly
changed engine, frame, resolution percentage, output path/format, Workbench shading, and draft
sample count without capturing them. Its `finally` block restored only extended-mode state.

## General mechanism

`h_render` now snapshots every common setting before selecting a diagnostic mode and restores all
of them in the unconditional `finally` block after rendering. Warnings are captured at the target
frame before restoration. The Vector Blur helper also selects the harness EEVEE engine before
materializing pass sockets, closing the compositor operation around its own prerequisite.

## Rejected patch-level alternatives

Teaching builders to reset the engine makes observation state part of model memory. Fixing only
the engine leaves frame, sample count, output path, and resolution as equivalent latent leaks.
Wrapping solid mode alone misses draft and crop paths.

## Validation

`test_render_handler_restores_common_scene_settings` pins the full state card and unconditional
restore. Producing validation renders a solid frame in Blender 5.2, reads back the original EEVEE
engine/settings, and then creates a wired Vector Blur without any manual reset.

## Release and rollback

No persisted schema migration. Render files remain outputs; only live scene settings are rolled
back.

