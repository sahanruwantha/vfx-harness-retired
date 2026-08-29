---
id: HIR-0110
title: Image debt needs optical-signal dependency closure
status: accepted
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

Focused result: 6 tests passed. Atomicity, plan-record, and plan-improvement suites:
151 passed. Full repository suite: 566 passed in 42.95 seconds.
`.venv/bin/ruff check src tests` and `.venv/bin/vfx --help` passed. The repository's
`pytest` launcher has a stale shebang pointing at a removed checkout, so verification used
the same environment through `.venv/bin/python -m pytest`; no test semantics changed.

The current selected view failed the new deterministic gate in 2.9 seconds with exactly
two `image-signal-bootstrap` findings: `iris_blade_geometry` and
`iris_rimlight_fixture`. The pre-fix producing build needed 849.5 seconds, 36 turns,
$2.3965, and seven accepted mutations before the same absence appeared as two 100%-black
frames. The gate therefore moved detection about 14 minutes earlier and before Blender or
builder spend.

Producing rematerialization run `20260829T134206Z-33c8f7` exercised the committed
mechanism against bundle `1d3b117b…`. At 956.4 seconds, validation rejected
`iris_rim_warning_lights`: its image debts preceded any signal provider, despite its light
name and appearance labels. The finding enumerated the registered witnesses. The model's
next attempted contract was `object_property(property=data.energy)`; HIR-0083 then exposed
the resulting light+keyframe mix, so the candidate transaction unstaged that unit and
published three independent units:

- `iris_blade_mechanism`: geometry/keyframe mechanism with no image debt;
- `iris_rim_chase_controller`: keyframe-only chase timing;
- `iris_rim_light_emitters`: light-family provider owning both image debts.

Validation passed at 1,155.7 seconds and finalization attested at 1,184.8 seconds. The
outer apply-replan replaced stale durable ids, selected view
`191752aa403bfd9ffa05df6f6c34876c9aeeeb6a371f45c3ef402805715fb620`
with layer-plan hash `c17dfd041f7fd21ec13970553ad6d3958b75cb69ecef1770876f875912bfaa29`,
and the first unit plan then passed its terminal gate. Layer 1 remained unchanged and
passed.

Materialization used 22 turns, 1,197.7 seconds, and $1.8962; first-unit planning used
4 turns, 356.1 seconds, and $0.3674. The materialization transcript recorded 21 tool calls
and seven typed rejections. Every rejection named the violated contract or legal next
action; there were no outside-card reads or scene mutations. Tool names with identical
empty inputs (`materialization_status`, `finalize_materialization`, `gate_preview`) were
called only after candidate revision/state changed, not repeated against the same state.
The largest remaining cost was one 778-second silent interval between initial vocabulary
reads and first staging, a model-latency outlier rather than schema discovery or transport
failure.

Implementation commit: `45121b3` (`fix: require optical signal before image debt`).

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
