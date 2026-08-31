---
id: HIR-0162
title: Construction-route authority replaces ungoverned asset import
status: accepted
introduced_in: unreleased
date: 2026-08-31
failure_class: ungoverned_asset_import
mechanism: construction_route_gate
adr: ADR-0009
---

# Construction-route authority replaces ungoverned asset import

## Observed failure

The live image-to-3D path is not on `vfx run`. `vfx asset` greps a global plan for
`bvfx_import_asset` / `.glb`, defaults `isolate=regen` (an image model redraws a
white-background plate from a prose `subject`), posts Meshy `POST /image-to-3d` with
one `image_url`, and writes shot-root `assets/<name>/model.glb`. Acceptance is a
model looking at `preview.png`. `MeshyBackend.to_glb(images, out)` takes a list and
silently uses `images[0]`. Builder `import_asset` then loads those ungoverned GLBs.

A geometry unit had no typed way to declare generate versus procedural, so a shading
or instancing unit could have claimed the same import, and extra prepared views never
reached the API.

## Root cause

Construction route was untyped. Generation lived beside the pipeline as a
freestanding stage whose targeting, isolation, HTTP payload, and acceptance were
heuristic. The adapter's list signature implied multi-view support while the client
dropped extras. Prompting "use multiple angles" cannot make a single-`image_url`
POST carry four plates.

## Decision criteria

- Route enum is closed and derived: generate/retrieve are mesh-family source units
  with witnesses; instancing is a successor; omit/abstain are not units.
- Default procedural does not bump `DIGEST_SCHEMA`.
- The adapter that receives N views (1–4) must POST N `image_urls` to
  `/multi-image-to-3d` or fail before HTTP. `should_texture` is false on source units.
- Do not treat Meshy thumbnails or `preview.png` as acceptance. `vfx asset` is
  retired; generate units cannot choose `import_asset`.

## General mechanism

`ConstructionSpec` on `WorkUnit` plus `construction_route_gaps` at staging,
collectable materialization validation, and the plan gate. `multi_image_to_3d`
is the adapter contract; `to_glb` may not keep `images[0]`.

## Rejected patch-level alternatives

- Prompt the builder to pass several files into the existing client: extras are
  dropped before HTTP.
- Keep `image_url` for one view and add a parallel multi-image helper: two
  authorities, silent fallback remains representable.
- Teach `vfx asset` to grep better: targeting remains plan-text inference.
- Raise isolate-regen quality: a prose subject is not a crop witness.

## Validation

A geometry unit with `construction.route: generate` and `refobs-*` witnesses plus
`object_count` eq 1 is legal. The same route on a shading write family, or with
`object_count` minimum 80, fails domain, staging, materialization, and plan-gate
checks. `omit`/`abstain` as unit routes fail parse. Four plate paths through
`MeshyBackend.to_glb` fail the test if the POST body has `image_url` or
`len(image_urls) != 4`. Default procedural units keep the schema-4 golden digest.
Minting a whole-frame box or a non-`refs/` source fails. An identity-failing orbit
never appears in Meshy arguments; an all-white plate never calls Meshy. Promotion
writes `build/construction/<sha256>.glb` and a digest-bound pointer; a hash
mismatch fails closed. Generate unit cards expose `bvfx_import_construction` and
hide `bvfx_import_asset`. `vfx asset` exits with the retirement rule.

## Release and rollback

No digest-schema migration. Rollback would restore silent `images[0]` fallback and
untyped generate. `vfx asset` fails closed; generate units cannot choose `import_asset`.

## Remaining limitations

Silhouette fingerprint bands and Blender qualification of scale, fused parts, and
baked lighting remain later ADR-0009 phases. `retrieve` is a closed unit route that
fails at build until a later amendment wires it. Cursor MCP
(`https://mcp.higgsfield.ai/mcp`) is a coding-agent connector, not the `vfx run`
adapter.
