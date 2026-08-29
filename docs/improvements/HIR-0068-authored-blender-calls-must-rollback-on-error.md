---
id: HIR-0068
title: Authored Blender calls must roll back on error
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: partial_mutation_after_tool_error
mechanism: per_call_blend_transaction
adr: null
---

# Authored Blender calls must roll back on error

## Observed failure

Run `20260827T213426Z-dd2f26` assigned World density `0.01`, then failed while converting a
POINT light to SPOT. The tool returned an error, but the next black-frame cause card proved the
density assignment remained. The journal correctly omitted the failed call, so live state and
its replay authority had already diverged.

## Root cause

`worker.h_run` journalled only successful calls but executed arbitrary Python directly against
the live data API. Blender background mode cannot use editor undo (`bpy.ops.ed.undo.poll()`
fails without an editor context), and no other rollback boundary surrounded the call.

## General mechanism

Every authored `run_bpy` and import call now saves one overwriteable, uncompressed scratch
`.blend` immediately before execution. Success removes it and appends the journal entry. Any
Python exception reopens the copy before surfacing the enriched error, removes the scratch file,
and leaves the journal untouched. Internal read-only probes remain non-transactional so the
mechanism's cost scales with authoritative mutation, not observation.

## Rejected patch-level alternatives

Reordering the failing script would only move the partial-write boundary. Asking builders to
write `try/finally` cannot cover unknown exceptions or helper internals. Blender editor undo is
not callable in the headless worker. Ignoring the live divergence until canonical replay wastes
the whole build and violates the immediate closed-loop contract.

## Validation

`test_failed_transactional_run_restores_scene_and_never_journals` injects an exception after a
write and pins restoration, journal exclusion, and scratch cleanup. A Blender 5.2 producing
probe repeated the injected failure through `BlenderSession`, read back the accepted marker,
and confirmed it was restored.

## Release and rollback

No persisted schema migration. The transaction copy is scratch and has one stable path per
serial worker. Do not remove it without an equivalent headless rollback primitive.

