---
id: HIR-0160
title: Image-contract debt requires a rendered carrier
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: shading_image_debt_on_camera_only_prefix
mechanism: image_subject_bootstrap
adr: null
---

# Image-contract debt requires a rendered carrier

## Observed failure

Layer 2 unit `hero_shade` on Hansa Silk Road (run `20260830T163643Z-5d7372`) paid
its scene contracts (one `hero.shade` material, roughness 0.45) then called
`cannot_express_in_scope` on `hero-facade-module-image-debt` and
`hero-facade-reflection-image-debt`. Live inspect showed 0 mesh / 0 verts / 0
tris; only the sealed layer-1 camera existed. `fault_owner_options` listed only
`camera_rig`. Finding `hf-659a7d927e678747c925` recorded `fault_owner_units: []`.

`hero_shade` was a DAG root. Image-signal bootstrap passed because its own
write-cluster is `shading`. Optical signal is not a rendered subject.

## Root cause

HIR-0110 requires a pixel-affecting family (`light`, `shading`, `volume`,
`compositor`) before image-contract debt. A shading-only unit satisfies that
predicate with no mesh in the replay prefix. Beauty of a facade cannot be paid
on a camera-only plate.

## Decision criteria

- Required image-contract debt publishes only when the unit's dependency
  closure, or an earlier materialized layer, derives a `mesh`, `volume`, or
  `compositor` write family.
- Shading and lights remain optical-signal providers; they are not carriers.
- Same-layer carriers outside the closure are named on the finding.
- Do not expand `cannot_express` options as the publication gate.

## General mechanism

`image_subject_dependency_gaps` mirrors `image_signal_dependency_gaps` against
`IMAGE_SUBJECT_FAMILIES`. Materialization validation and the plan gate fail
`image-subject-bootstrap`.

## Rejected patch-level alternatives

- Consume the finding and rematerialize the same DAG: shade would still be a
  root.
- Add same-layer geometry ids to `fault_owner_options` without a publication
  predicate: replan could reopen massing while leaving shade unordered.
- Drop the image debts from `hero_shade` by prompt: the DAG would remain
  representable.

## Validation

A shading-only unit with image-contract debt and a sibling geometry unit not in
`depends_on` fails domain, plan-gate, and materialization checks. Depending on
that geometry unit, or inheriting an earlier-layer mesh/volume/compositor
family, clears the gap. The HIR-0122 polish image-debt fixture now includes a
geometry predecessor.

## Release and rollback

No schema migration. Rollback would restore shading-only image debt on a
camera-only prefix.

## Remaining limitations

Carrier presence is write-cluster family, not a proof that the image claim's
`subject_roles` occupy that mesh. Dressing still has to close role assignment
separately.
