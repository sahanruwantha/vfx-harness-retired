---
id: HIR-0079
title: Helper inventory must publish return contracts
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: helper_return_shape_guess
mechanism: compiled_return_annotations
adr: null
---

# Helper inventory must publish return contracts

## Observed failure

Run `20260827T234950Z-2d6f18` received `bvfx_light(...)` in the compiled helper inventory but
the inventory exposed only its parameter signature. The builder guessed that it returned
`(object, data)` and wrote `core, core_d = bvfx_light(...)`. The helper actually returns one
Blender Object, so the first complete scene mutation failed with `TypeError: cannot unpack
non-iterable Object object`. Transactional rollback prevented partial state, but the failed call
and correction still cost a model turn.

## Root cause

The helper compiler extracted names, parameters, and doc summaries from the authoritative worker
source but discarded function return annotations. Most worker helpers also had no annotations, so
the bounded unit card forced the model to guess the result shape it needed for the next line.

## General mechanism

Every injected `bvfx_*` helper now declares a string return annotation in `worker.py`, including
Blender host types, scalar/list results, and the camera rig's two-object tuple. The same AST
compiler that reads helper parameters appends that annotation to the published signature. The
worker source remains the single authority; no parallel prompt-side return table is maintained.

## Rejected patch-level alternatives

Adding a one-off warning for `bvfx_light` would leave every other helper ambiguous. Inlining full
docstrings into the kickoff would enlarge all unit contexts to fix a single missing type. Letting
the builder probe by unpacking is exactly the failed-call feedback loop the compiled card exists
to remove.

## Validation

`test_helper_inventory_is_worker_helpers_not_a_private_copy` now asserts that `bvfx_light`
publishes `Object` and `bvfx_camera_rig` publishes `tuple[Object, Object]` from worker annotations.

## Release and rollback

No persisted schema migration. Removing an annotation makes only that helper's result ambiguous
again and is caught by the inventory regression for the pinned helpers.
