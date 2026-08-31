---
id: ADR-0009
title: Construction-route authority for geometry source units
status: accepted
date: 2026-08-31
supersedes: null
---

# Construction-route authority for geometry source units

## Context

[docs/research/shot-aware-image-to-3d-asset-planning.md](../research/shot-aware-image-to-3d-asset-planning.md)
places image-to-3D inside geometry-owning layer materialization: a closed construction
route on a normal `WorkUnit`, then ordinary instance/assembly successors. The live
alternative is an ungoverned path — grepping a global plan for import calls, regenerating
a product plate from prose, posting one Meshy `image_url` while discarding extra views,
and treating a preview look-over as acceptance ([HIR-0162](../improvements/HIR-0162-construction-route-replaces-ungoverned-asset-import.md)).

Global planning already publishes an ownership DAG only. Camera layers already own judge
frames and deferred subject composition. Form/layout materialization is the boundary that
can choose how a unique source mesh is obtained without inventing a department layer or
letting the builder pick `import_asset` at runtime.

## Decision

A work unit may declare `construction` with a closed route:

- `procedural` (default when omitted)
- `generate`
- `retrieve`
- `simplify`

`omit` is a typed requirement decision, not a unit. `abstain` is a blocker or
`cannot_express_in_scope`, not a unit. `reason` is audit text and never satisfies a gate.

Route legality is derived from the unit's one write cluster and bound required evidence:

- `generate` and `retrieve` require a mesh family (`provides: ["geometry"]` residual or
  mesh-kind write evidence). Shading, lights, cameras, and keyframes cannot generate.
- `generate` requires non-empty `refobs-*` witnesses. Those handles authorize generation;
  they are not the Meshy payload and not acceptance evidence.
- `generate` cannot bind a required `object_count` whose minimum exceeds 1. Instance
  assembly is a successor that consumes the source interface.
- `simplify` requires a mesh or volume family.

Default procedural with empty witnesses and empty reason is omitted from `unit_digest` so
existing schema-4 durable hashes stay comparable. A non-default route or witness list
participates in the digest.

Promoted generate bytes live at `build/construction/<sha256>.glb` with a digest-bound
pointer at `build/units/<layer>/<unit-id>.construction.json`. Replay imports only that
hash-verified relative path through `bvfx_import_construction()`. `runs/`, shot-root
`assets/`, and network paths are not construction authority (ADR-0002). Before the
builder session the harness crops minted `refobs-*` witnesses, identity-gates
Higgsfield plates, posts surviving views (1–4) as `image_urls` to `/multi-image-to-3d`,
and qualifies the GLB (`glTF` magic, non-empty, SHA-256). Silently keeping `images[0]`
is a contract violation. Source-unit generation sets `should_texture: false`.
`vfx asset` and generate-unit `import_asset` fail closed (ADR-0004).

This decision does not authorize a global asset planner, an `assets` department layer,
runtime builder selection of a generator, or treating Meshy thumbnails as acceptance.

## Consequences

Materialization can stage a generate source unit that the plan gate can refuse when the
derived family or bound count is wrong, before any paid Meshy call. Existing procedural
units need no authored field. Durable state for default-procedural units does not migrate.
A generate unit cannot publish until witnesses are minted, plates pass identity, and
promoted bytes hash-match the pointer. `retrieve` remains a closed enum value that
fails at build until a later amendment wires it.

## Rejected alternatives

- A new global planner or `vfx asset` on the `vfx run` critical path: global planning
  owns the layer DAG, not construction routes or refs inspection.
- An `assets` department layer: dressing already assigns appearance on owner-granted
  geometry (ADR-0007); generation is mesh construction, not look.
- Builder-time `import_asset` as the route chooser: mutation authority would expand
  past the published unit.
- Authored family strings or `reason` as legality: HIR-0017 padding.
- Parallel legacy shot-root `assets/` plus typed construction: ADR-0004 forbids silent
  dual authority. `vfx asset` and generate-unit `import_asset` fail closed.

## Validation and review trigger

Domain, staging, materialization, and plan-gate tests refuse generate on a shading
cluster, generate with `object_count` minimum greater than 1, generate without
`refobs-*` witnesses, and omit/abstain as unit routes. A four-view adapter fixture
fails if the HTTP body contains `image_url` or fewer than four `image_urls`. Review
this ADR when silhouette fingerprint bands, Blender mesh qualification, or a wired
`retrieve` route land as amendments.
