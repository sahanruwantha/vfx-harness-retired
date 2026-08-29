---
id: HIR-0072
title: Volume controls need graph provenance and closed-loop helpers
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: volume_control_misclassification
mechanism: graph_provenance_and_atomic_semantic_tagging
adr: null
---

# Volume controls need graph provenance and closed-loop helpers

## Observed failure

In build `20260827T221413Z-6f548c`, the active lighting unit created a bounded material
volume and proposed `volume.inputs['Density'].default_value = 0.05`. The black-frame guard
classified that assignment as an unmeasured *World* density and refused it. The subsequent
material probe matched no host because the helper had required the builder to add semantic
tags in separate free-form statements. Its immediate dimension readback also reported half
the requested size, hiding a scale error behind stale state.

## Root cause

The guard matched the socket label `Density` without proving which node graph owned the
socket. World and material volume nodes share that label but represent different causal
hypotheses. The volume helpers also returned before establishing all role/control identity
and a flushed dimensional readback. Finally, `_bvfx_volume(size=...)` treated the requested
full dimensions as half-extents even though `primitive_cube_add(size=1)` creates unit-width
geometry.

## General mechanism

Authored World-density detection now performs a bounded forward provenance pass from
`.world` expressions through local assignments. A literal Density socket is guarded only
when its target is derived from that World graph; `bvfx_volumetric_world(density=...)`
remains explicitly guarded. Unknown graph provenance abstains instead of assigning World
meaning to every identically named socket.

`bvfx_volume` now accepts and applies object, material, node, and control semantic tags in
the creation call, flushes the view layer before returning, and maps `size` to the cube's
full dimensions. `bvfx_volumetric_world` likewise applies World, node, and control tags as
part of creation. A helper call therefore returns an immediately addressable and truthful
semantic graph rather than requiring follow-up mutation and guessed readback.

## Rejected patch-level alternatives

Allowlisting density `0.05` would encode this shot and would still confuse every future
material volume. Teaching the model to spell tags after helper creation leaves partial
graphs representable and repeats the failure after any interruption. Merely documenting
the half-size convention would contradict the helper's public `size` contract and preserve
wrong coverage measurements.

## Validation

`test_authored_density_values_finds_socket_and_helper_literals` pins positive World
provenance. `test_authored_density_values_does_not_misclassify_material_volume` pins the
same socket label in a material graph as outside that guard. The helper-source test pins
atomic semantic tagging, view-layer flushing, and full-dimension scale math.

Producing validation in Blender 5.2.1 created `size=(8,6,4)` and read back dimensions
`(8,6,4)`, with the requested object, material, volume-node, World, and control tags on
their actual hosts.

## Release and rollback

No schema migration. Calls that relied on the erroneous half-size behavior must pass their
intended full dimensions explicitly; the helper now follows its documented contract.
