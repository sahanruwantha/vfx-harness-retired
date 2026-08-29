---
id: HIR-0110
title: Image debt needs optical-signal dependency closure
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: image_debt_precedes_optical_signal
mechanism: derived_image_signal_dependency_gate
adr: null
---

# Image debt needs optical-signal dependency closure

## Observed failure

Layer 2 materialization run `20260829T125708Z-82ee50` published
`iris_blade_geometry` as a dependency root with `provides: ["geometry"]`, appearance
capabilities `detail`, `material`, and `lighting`, and required image-contract debts
`iris-blade-beauty-f001` / `iris-blade-beauty-f036`. Its one derived write cluster was
mesh. The same layer's `iris_rimlight_fixture` was a later independent root whose only
scene row was `object_count`; it therefore derived control, not light or shading.

Producing build `20260829T131109Z-36799e` spent 36 model turns, 849.5 seconds, and
$2.3965 on the first unit. Seven mutations eventually made all six scene contracts pass,
but EEVEE at frames 1 and 36 remained 100% black: the cumulative replay had no World,
light object, or prior emission. Candidate freeze recorded
`hf-abf6d2228d36dd6f3879` as `unpaid_image_debt` and stopped the layer.

This was the third instance in the generation. Earlier findings
`hf-1433641b761ae9036271` and `hf-aa35f130dc95351fd9b1` had already proved that blade
beauty or frame-delta debt was due before any unit capable of making the geometry visible.
Replanning changed unit names and split geometry from animation, but publication still had
no executable predicate for optical-signal ordering.

## Root cause

HIR-0046–0048 make appearance debt explicit and refuse unpaid freeze. HIR-0085/0086 make
camera availability a typed dependency precondition. No corresponding publication rule
required an image-debt owner to have an in-scope way to affect pixels. Materializers could
therefore attach look labels and image claims to mesh/control units and defer light or
emission until a sibling which was not in their dependency closure.

`look_capabilities` cannot be that precondition: it is authored feedback ownership, and
the failed unit already declared it. Role strings such as `iris.rim_light` and an object
count are likewise not evidence of a Blender Light, emissive material, World volume, or
compositor write. Trusting either would repeat HIR-0086 self-certification.

## Decision criteria

- Every unit owing required `image_contract` debt has a pixel-affecting provider in its
  own/transitive same-layer dependency closure or an earlier materialized layer.
- Provider identity is derived from the canonical HIR-0083 write cluster, never from a
  unit label, look capability, role token, prose, or warm scene state.
- Pixel-affecting families are the closed set `light`, `shading`, `volume`, and
  `compositor`. Geometry, camera, control, and keyframe authority do not bootstrap signal.
- Materialization publication and the independent plan gate enforce the same predicate.
- Rejection names the owed ids, any same-layer providers outside the closure, and legal
  contract witnesses derived through the canonical instrument-family resolver.
- Earlier-layer availability is resolved from the selected consumer view's typed units
  and scene-contract rows, not from a prior render or checkpoint proximity.
- Runtime no-signal detection remains authoritative for a provider implemented badly;
  this rule proves structural mutation capacity and ordering, not successful pixels.

## General mechanism

`domain/image_signal.py` compiles provider ids by passing each unit through
`atomicity.write_clusters` and retaining the four pixel-affecting families. It walks the
image-debt owner's dependency closure, including the owner itself, and emits an
`ImageSignalDependencyGap` when no provider is reachable. The witness card calls
`instrument_family_for_row` over registered row variants, so feedback cannot drift into a
second family registry.

`validate_materialization` composes current rows with the selected base view, derives
earlier-layer signal availability, and reports a pointer-addressed finding on the owning
unit's `depends_on`. `_check_evidence_coherence` independently emits blocking
`image-signal-bootstrap`. The materializer's legal response is to split/reorder the DAG
and bind typed light/shading/volume/compositor evidence, not to add an authored capability.

## Rejected alternatives

- Trust non-empty `look_capabilities`: the observed unit already carried material and
  lighting labels while its executable scope was mesh-only.
- Infer signal from `light`, `emission`, or `world` substrings in ids or roles: semantic
  names are not host or mutation authority.
- Insert a default World or temporary light at build time: undeclared mutation would not
  exist in cumulative replay authority.
- Move the debt to a later unit by prompt advice alone: two replans reproduced the shape;
  publication must make it unrepresentable.
- Treat Workbench/matcap form diagnostics as payment for EEVEE beauty debt: that changes
  the evidence domain and defeats HIR-0046–0048.

## Validation

Focused fixtures cover:

- mesh and `iris.rim_light` control units whose names/look labels do not create signal;
- a real light-family provider outside versus inside the debt owner's dependency closure;
- an image-debt owner whose own shading-family row is sufficient;
- inherited earlier-layer signal authority;
- registry-derived light, shading, World-volume, and compositor witness feedback;
- materialization rejection with a JSON pointer and independent plan-gate rejection.

Focused result before the production rerun: 6 tests passed. Atomicity, plan-record, and
plan-improvement suites: 151 passed. Full repository and producing-run evidence are added
after the mechanism is committed and exercised through the public CLI.

Implementation commit: pending.

## Release and rollback

This is stricter publication validation with one optional internal base-scene input; no
persisted schema changes. Rollback would again allow image debt to spend a full builder
session before discovering that no reachable unit can affect beauty pixels.

## Remaining limitations

The gate proves that some legal mutation family can affect pixels, not that a particular
implementation produces useful or non-black output. Candidate optical-signal checks and
immutable adversary payment remain the runtime boundary. The producing run also exposed a
separate possible gap between planned write-cluster authority and actual journal/script
writes; HIR-0110 does not claim to attest runtime mutation families.
