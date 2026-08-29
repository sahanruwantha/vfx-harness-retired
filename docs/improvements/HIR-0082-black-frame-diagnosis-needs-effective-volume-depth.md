---
id: HIR-0082
title: Black-frame diagnosis needs effective volume depth
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: wrong_optical_scale_and_missing_renderer_coverage
mechanism: effective_volume_span_cause_card
adr: null
---

# Black-frame diagnosis needs effective volume depth

## Observed failure

In run `20260828T001601Z-9ca8f6`, the active camera clipped at 20,000 units but canonical
EEVEE ended volumetrics at 100. The sealed semantic meshes were roughly 180–1,330 units
from the camera at the judged frames. The automatic black-frame card reported
`Density × camera.clip_end = 400`, omitted `volumetric_end`, and sent the builder through
density and light-placement searches. It discovered `volumetric_end=100` only after about
twelve minutes by separately inspecting renderer state. Its earlier attempt to extend the
range was correctly denied because the scoped unit did not own renderer policy.

## Root cause

The diagnostic treated camera visibility range as volume integration range. Camera clipping
and EEVEE volumetric sampling are different renderer facts. The card also lacked distances
from the evaluated camera to named semantic subjects, so it could not show whether canonical
volume depth reached the scene whose atmosphere the image contract described.

## General mechanism

The Blender black-context instrument now returns canonical `volumetric_start` and
`volumetric_end` plus evaluated camera distances for semantic mesh hosts. The cause card
computes extinction against `min(camera.clip_end, volumetric_end - volumetric_start)` and
names subjects beyond `volumetric_end`. It labels this as renderer depth coverage—not a
density target—so a scoped builder can distinguish an owned density decision from an
unowned renderer-policy constraint before spending on coordinate guesses.

The instrument reports facts and abstains from declaring impossibility: a light beyond the
range can still illuminate nearer sampled volume. A hard scope conflict still requires typed
`cannot_express_in_scope` and audited replanning.

## Rejected patch-level alternatives

Adding `volumetric_end` to the prompt would copy mutable Blender state into prose and become
stale. Using `camera.clip_end` preserves the wrong physical quantity. Automatically changing
renderer depth would violate HIR-0076 and silently broaden the unit's authority.

## Validation

Unit tests pin canonical volume-range reporting, named subject distances, and the effective
99.9-unit span for the producing-run geometry. Existing density and placement closure tests
remain the causal-search ratchet. The next Blender producing run must show the cause card
contains the renderer range before the first density or placement decision.

## Release and rollback

No persisted schema migration. `black_context` adds diagnostic fields; existing consumers
continue to read the prior fields.
