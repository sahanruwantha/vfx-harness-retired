---
id: HIR-0196
title: bbox and visibility metrics counted geometry the frame does not contain
status: accepted
introduced_in: unreleased
date: 2026-09-04
failure_class: ablation_loop_cannot_close
mechanism: rendered_subject_metrics_exclude_objects_hidden_from_render
adr: null
---

# bbox and visibility metrics counted geometry the frame does not contain

## Observed failure

In run `20260904T105849Z-0c9a45`, layer 2's `exterior_ground` was the dependency-complete
producer for `exterior.*` and so had to pay `bbox-ext-f1` (`bbox_height` in 0.15..0.35 at
frame 1). It measured 0.3936. It then ablated its own contribution the only way a builder
can, and recorded the result in its abstention:

> I toggled hide_render/hide_viewport on all 4 objects I created (ground_island,
> streetlight_0..2) and the bbox_height read back identically (0.3936) with them hidden

and concluded the overflow was unrelated to its geometry.

The reading could not have changed. `_projected` iterates every selected object and
projects its evaluated mesh; it never consults render visibility. The instrument was
incapable of answering the question the builder asked it, and the builder reasoned
correctly from an answer that meant nothing.

## Root cause

A missing instrument response, not a reasoning failure. AGENTS.md requires that framing,
bbox and visibility checks measure *rendered subjects*, and that every mutating tool have a
matching observation so an agent sees what its action did. An object hidden from render is
not a rendered subject, yet both rendered-subject metrics counted it:

- `evidence/scene_checks/probe.py::_projected` — the shared projection behind every
  `bbox_*` row.
- `blender/checks.py::surface_visible_fraction` — the canonical `visible_fraction` sampler.

The second is the more serious of the two: a subject hidden from render could satisfy a
required `visible_fraction`, which is precisely what the per-role AND rule (HIR-0051)
exists to prevent. A union could not hide a subject, but a `hide_render` flag could.

## Decision criteria

- A metric named for what the frame shows must measure what the frame shows.
- Close the loop: a builder's ablation must be able to change the number, or the tool is
  teaching it something false.
- A hidden subject is a failing measurement, not an instrument error (HIR-0019): it reads
  0.0 rather than raising.

## General mechanism

`checks.renders_in_frame(evaluated)` is the one predicate both metrics call.
`_projected` excludes hidden objects from the union and names them when nothing is left to
project, so "no object intersects the frustum" can no longer hide "they were all hidden".
`surface_visible_fraction` still samples a hidden subject into the denominator but never
counts it as seen, so a fully hidden subject reads 0.0.

## Rejected patch-level alternatives

- *Tell builders that hide_render does not affect bbox.* Prompt wording for an instrument
  that answers the wrong question, and it leaves the `visible_fraction` hole open.
- *Have the ablation use deletion instead of hiding.* Deletion is a destructive mutation
  inside a scoped unit; hiding is the safe, reversible probe, and the instrument should
  support it.
- *Respect `hide_viewport` too.* Viewport visibility does not decide the rendered frame;
  conflating them would make the metric disagree with the render it stands for.

## Validation

`src/tests/unit/test_rendered_subject_visibility.py` — the predicate over hidden, visible,
attribute-less, and a parametrised sweep of truthy/falsey flags; plus structural assertions
that both the projection helper and the visibility sampler consult it, each failing when
its call site is removed. Removing either call fails the suite.

## Release and rollback

A `bbox_*` or `visible_fraction` reading over a selection containing render-hidden objects
changes value — which is the fix. No durable state changes shape. Rollback is reverting the
commit.

## Remaining limitations

- Objects excluded from the view layer, or hidden by a collection's render flag rather than
  their own, are not yet consulted; the predicate reads the object's own `hide_render`.
- This does not settle whether `bbox-ext-f1`'s band is satisfiable for that shot. It makes
  the ablation that would answer it work.
