# Shot-aware image-to-3D asset planning

Status: provisional design proposal. This note leaves research only through the exit records
named in "Leaving research"; nothing in it is binding rule text.

## Summary

VFX Harness should not ask a builder to construct every shot from scratch, and it should not
send an entire reference frame to an image-to-3D model and accept the result as a scene.

The proposed capability is **shot-aware asset planning**, owned by the geometry layer's
materialization. It reads the layer's due references through typed instruments, decomposes the
visible scene into semantic objects and reusable parts, and chooses the cheapest reliable
construction route for each part from a closed set:

- deterministic procedural construction;
- image-to-3D generation: witness crops, a typed view-prep loop, then a
  multi-image adapter that actually consumes the prepared angles;
- retrieval of an existing qualified asset;
- instancing of an accepted asset;
- a simplified carrier (proxy mesh, card plane, volume) whose appearance is dressed later;
- omission when a part cannot affect required evidence; or
- abstention when the references do not support a safe choice.

Image-to-3D is therefore one bounded sourcing instrument. Its output is an untrusted candidate
until it passes typed qualification, is promoted into the accepted chain, and replays from an
empty scene.

The orchestration level gains no new concepts: a part is a `WorkUnit`, its route is a
gate-enforced construction field, its candidate is run-scoped evidence (ADR-0002), and its
accepted artifact is the unit's identity-derived replay script (HIR-0126) plus digest-pinned
bytes.

## Why build this capability

Shot construction currently risks spending agent turns and Blender work on questions that can
be answered before scene mutation:

- Which visible objects are actually required by the camera?
- Which objects are repeated instances of one reusable part?
- Which parts need exact dimensions or clean modular alignment?
- Which irregular forms would be cheaper to generate than model procedurally?
- Which background elements can be represented by proxies, cards, shaders, or omission?
- Which reference regions are too ambiguous to support generation?

Answering these questions first reduces unnecessary modeling, avoids monolithic generated
scenes, improves reuse, and gives builders bounded work units with explicit evidence
obligations.

This also improves agent capability in the intended VFX Harness manner: the model receives
measurements, enumerated choices, and read-back rather than a larger prompt asking it to guess
better.

## Product idea

The planner produces a typed **asset bill of materials** for the shot. It is not a flat count
of visible objects. It distinguishes:

- semantic object roles;
- unique source models versus instance counts;
- whole objects versus independently constructible parts;
- hero, supporting, background, and invisible geometry;
- required camera frames and projected importance;
- procedural, generated, retrieved, simplified, or omitted construction;
- geometry, material, rigging, animation, and interaction requirements;
- reference coverage and uncertainty;
- dependencies and acceptance evidence.

The bill of materials is materialization output: it compiles into staged work units through the
existing candidate transaction. It is not a new document class with its own authority, and each
accepted unit can be replaced or repaired without rebuilding the entire scene.

## Why not generate the whole scene

A monolithic image-to-3D result has the wrong control properties for this harness:

- repeated parts may be fused instead of instanced;
- scale and alignment are uncertain;
- hidden surfaces are invented without evidence;
- windows, doors, characters, furniture, and architecture may not separate cleanly;
- topology, UVs, and baked lighting may be unsuitable;
- a result that resembles one frame may fail elsewhere on the camera path;
- local repair becomes difficult because ownership is unclear;
- provenance and deterministic replay are harder to preserve.

The correct unit is the smallest reusable or independently repairable semantic part that still
satisfies the shot.

## Integration: ownership

Geometry-owning layer materialization owns the bill of materials. Builders execute one part.
Look never sources meshes.

| Stage | What changes |
| --- | --- |
| Global planner | Nothing. Register owned requirements and reserved roles (ADR-0006). No routes, no generator names, no part list. |
| Camera layer | Nothing. Judge frames and deferred rendered-subject `bbox_*` rows already give form layers their projected importance (HIR-0151, HIR-0158). |
| Form / layout materialization | New work. Consume typed reference measurements, propose parts, pick a closed route, stage normal units through the existing candidate transaction (HIR-0100). |
| Source unit | Procedural mesh, or a generated/retrieved candidate that is qualified, promoted, and published as a deterministic import script. |
| Instance / assembly unit | Consumes the source interface (HIR-0084). Owns placement, instancing, and in-scene composition evidence. |
| Look / animation | Dress published roles (ADR-0007). Same-layer dressable stays illegal (HIR-0161). |

A part is not a new unit type. It is a `WorkUnit` with a gate-enforced `construction` field:

- `procedural` → the existing geometry builder path;
- `generate` / `retrieve` → run-scoped candidate under `runs/<id>/evidence/`, qualification,
  then promotion and an identity-derived `build/units/<layer>/<unit-id>.py` import script;
- `simplify` → a carrier unit (mesh or volume family) plus a dressable grant for later look
  work — never a fake "finished" mesh and never a mixed cluster;
- `omit` → a typed requirement decision;
- `abstain` → a materialization blocker or `cannot_express_in_scope`.

### Route legality is derived and witnessed, never authored

If `route` were a string the materializer can set to silence a gate, it would be HIR-0017
padding. The precedent is the derived write-cluster gate: authored family strings are padding
and the family is derived from typed mutation targets (HIR-0095, HIR-0112). Route enforcement
uses the same shape:

- `generate` and `retrieve` are legal only on a unit whose derived write cluster is
  mesh-family. The existing cluster derivation and the compiled capability vocabulary
  (HIR-0128) already refuse an emission, title, or compositor unit; no parallel keyword check
  is added.
- `generate` requires immutable reference-observation handles as **witnesses**:
  harness-issued crop/region handles from the measurement instrument, following the
  image-payment handle discipline (ADR-0008, HIR-0053). Witnesses authorize generation;
  they are not dumped straight into Meshy. Derived **plate handles** (isolate, relight,
  de-occlude, orbit) are the adapter inputs. Provenance is one chain: reference hash →
  crop handle → plate handle(s) → generation envelope → candidate hash → qualification →
  published interface. A plate without a parent crop handle is illegal. A whole frame is
  illegal. A prose subject with no crop is illegal.
- Procedural-forcing conditions derive from the unit's bound contracts — alignment,
  dimension, and instancing rows — not from a self-declared reason field.
- `construction` participates in the unit digest. Changing a route is a superseded unit
  through replan, never an in-place edit (HIR-0059, HIR-0102).
- The route gate is a cost saver, not the safety. A mis-routed part must still fail its
  qualification and acceptance contracts; the gate exists so the failure is cheap and early.
- The recorded `reason` string is audit context only. It never satisfies any gate.

Legality is checked at the three moments every other unit invariant already passes through:
local staging, `finalize_materialization`, and the plan gate.

## Why not the other shapes

- **A new global asset planner** — whole-shot design with no scene. Global publication is
  ownership-only by decision (ADR-0006), and global tools have no reference-measurement
  authority on purpose. Routes are layer-local, deferrable decisions under the ADR-0005
  deferral test.
- **A new assets layer** — duplicates form ownership, runs without a camera closure for its
  own judged evidence (HIR-0086), and recreates the legacy asset stage as a DAG node with
  ambiguous fault routing.
- **Let the builder choose at runtime** — that is today's `import_asset` tool: an untyped
  choice, gated only by session configuration, paid for after mutation, against shot-root
  bytes nothing pins to a unit digest.

## Candidate lifetime and accepted bytes

Candidates are run-scoped (ADR-0002). Shot-root `assets/<name>/model.glb` must not remain
build authority. But run evidence is not a replay input either: ADR-0002 rejected accepted
build artifacts inside runs because downstream replay needs a stable current build boundary,
and re-calling a generator is nondeterministic by definition.

The invariant this note commits to now, ahead of the exit ADR that fixes the exact layout:

- Replay of the accepted chain never reads `runs/`, shot-root `assets/`, or the network.
- Publication promotes qualified candidate bytes into the accepted chain under `build/`,
  content-addressed and immutable.
- The unit's import script verifies the content hash before import and fails closed on a
  mismatch.
- The content hash participates in the unit digest and the published interface digest, so
  swapped or superseded bytes invalidate every consumer exactly like any other digest change
  (HIR-0059, HIR-0084).
- Promotion is an explicit accepted transaction, never proximity (ADR-0002).

The exit ADR decides where under `build/` the bytes live and how unreferenced bytes are
collected. It does not get to weaken the invariant.

## Legacy surface migration

This design replaces live code, not a gap. The current mechanism is:

- `import_asset` in the warm-session Blender tools, which imports shot-root
  `assets/<name>/model.glb` by name at build time, gated only by session configuration;
- the freestanding `asset_builder` agent flow, which selects its own targets from the plan
  and writes the frozen `assets/` directory;
- the `vfx_harness.assets` package (Meshy, Higgsfield, image backends, normalization), which
  sits outside the declared repository boundaries. The live Meshy adapter is
  `POST /image-to-3d` with a single `image_url`; `MeshyBackend.to_glb` accepts a list and
  silently discards every view after the first. Meshy's own Multi-Image to 3D API
  (`POST /openapi/v1/multi-image-to-3d`) takes 1–4 views. That drop is the mechanical
  defect the view-prep loop exists to close.

Strict migration applies (ADR-0004): the route mechanism and the legacy tools must not coexist
as parallel authorities. Landing the source-unit path retires `import_asset` and the
freestanding asset-builder flow in the same change, or behind a compatibility window with a
declared deterministic expiry — never as a silently surviving alternative. The package
re-homes with the migration: generator HTTP adapters belong in `infrastructure/`,
import and normalization at the `blender/` boundary, and candidate, route, provenance, and
qualification contracts in `domain/`.

## Decision policy

For every proposed part, materialization chooses among the closed construction routes.

### Procedural construction

Prefer procedural construction when the part is:

- repetitive or modular;
- dimension-critical;
- required to align with a camera path or another object;
- topologically simple;
- driven primarily by a shader, animation control, or instancing system;
- expected to support many variants from one definition.

Examples include walls, tiled floors, windows, façade grids, columns, benches, city blocks,
tower floors, transition tunnels, and camera-aligned openings.

### Image-to-3D candidate

Prefer image-to-3D when the part is:

- irregular, sculptural, or organic;
- sufficiently isolated in the reference;
- supported by useful views or only required from a bounded view range;
- expensive to construct procedurally relative to its shot importance;
- not dependent on exact deformation topology unless the generator can prove it.

Examples include statues, rigid props, ornament, background people, rocks, furniture, and
selected hero objects.

### Existing asset retrieval

Prefer retrieval when a matching accepted asset already exists. Retrieved assets pass the same
qualification boundary as generated assets, and their candidate envelope carries the same
provenance obligations plus licensing metadata. A candidate without provenance or license
authority fails qualification; it is not waved through because it already existed.

### Simplified carrier

Use a proxy, card plane, or volume when projected size, occlusion, depth, and interaction
requirements show that full geometry cannot affect acceptance. A simplification is two
authorities, not one: the form layer stages the carrier — a mesh or volume family, one derived
write cluster — and its appearance is a later look layer's work through an explicit dressable
grant (ADR-0007, staged through the existing `layer_updates.dressable` surface). A "card" is a
mesh plane plus a texture; the texture is never the carrier unit's write.

### Omission

Omit a proposed part only when required camera and evidence coverage proves it cannot affect
the accepted shot. Omission is an evidence-backed typed requirement decision, not an assumption
based on a single frame.

### Abstention

If reference support is insufficient, materialization must report the missing evidence and
legal next actions — a materialization blocker, or `cannot_express_in_scope` at build time. It
must not invent hidden structure or force a generation route.

## Proposed pipeline

### 1. Resolve shot authority

Read the selected brief, exact reference frames, frame range, camera requirements, declared
constraints, and current accepted replay prefix. Historical output and nearby generated files
are not authority.

### 2. Measure reference coverage

Reference access at materialization is instrument-shaped, not a raw `Read` (HIR-0054). The
existing `measure_ref` instrument measures canonical look fingerprints; this capability
extends it (or adds a sibling instrument) to produce typed region observations: image region
and projected size, visibility and occlusion, silhouette importance, temporal coverage,
apparent repetition, available viewpoints, and uncertainty.

Each observation is returned with an immutable harness-issued handle. Those handles are the
route witnesses and the only legal generator inputs. Free-form visual judgment may propose
regions and identities; it cannot mint handles and cannot self-certify.

Reference measurement is evidence production: it parallelizes ahead of authoritative staging
and compiles into the materialization kickoff, so decomposition does not scale the staging
session itself. The staged-pipeline design already reserves exactly this slot — independent
asset preparation and reference analysis run in parallel before integration. Staging stays
incremental through the locked candidate transaction (HIR-0100); an exhausted session is a
failed transaction (HIR-0027) that loses no measured observation, because remeasuring is cheap
and the staged prefix survives.

### 3. Produce semantic decomposition proposals

Group observations into stable semantic objects and reusable parts. The planner separates:

- unique model count from instance count;
- structural parts from surface treatment;
- repeated modules from hero exceptions;
- rendered subjects from controls;
- independently animated or repairable components.

Every proposal includes witness handles and confidence. Ambiguous merges and splits remain
explicit alternatives until resolved.

### 4. Select a construction route

Apply the closed decision policy to each part. The selection records its evidence — witness
handles, bound contracts, consumed interfaces — and route legality is checked as derived
conditions at staging, finalization, and the plan gate. The recorded reason is audit prose;
it satisfies nothing.

### 5. Materialize bounded asset work units

Create dependency-ordered units for sourcing, qualification, cleanup, owner-granted material
work, rigging where required, and assembly. Do not give an asset generator scene-wide mutation
authority. Each unit owns one stable identity, one derived write cluster, and publishes only
through the typed transaction bound to its exact digest.

### 6. Prepare views, then generate or construct candidates

A `generate` unit does not call Meshy on the crop. It runs the view-prep loop below until it
has a legal 1–4 plate set, then one adapter call. Procedural builders skip this loop and
receive exact modular and interface requirements. Candidate generation publishes no
authority.

### 7. Qualify candidates

Qualification reuses the freeze/replay machinery rather than inventing a parallel checker, and
it runs against the candidate bytes in an isolated evidence session first, so a bad candidate
is rejected before it consumes the unit's live mutation budget. It verifies, as applicable:

- file readability and deterministic import;
- immutable source and output hashes and complete provenance: generator, model, settings,
  license;
- scale, origin, orientation, and bounding dimensions — measured against targets from the
  unit's bound contracts or consumed producer interfaces, never against the generator's own
  output or an uncorroborated proposal field (HIR-0017);
- mesh or volume carrier presence (HIR-0160);
- object separation and semantic-role assignment;
- topology, manifold, UV, and material-slot expectations;
- rig or animation compatibility where declared;
- asset-local silhouette from required views;
- material normalization: imported materials are stripped to neutral so the accepted import is
  a pure mesh-family write (HIR-0083, HIR-0112). Baked lighting is rejected outright — a
  generated GLB is a mesh carrier for later look, not optical signal (HIR-0160). Qualified
  texture maps survive only as side artifacts a look layer may later bind through its own
  dressing authority (ADR-0007); they are not part of the source unit's scene write;
- performance bounds;
- replay from a clean scene.

Evidence classes stay honest: `inspect_view` is diagnostic-only, mints no payment handle, and
cannot satisfy a contract (HIR-0148); Workbench comparisons guide form but pay no beauty debt
(HIR-0131). Acceptance is executable rows plus qualification-gated qualitative claims only.

Failure rejects the candidate or creates a bounded repair unit, naming the violated contract,
the observed value, and the legal next actions. It never silently broadens authority or lowers
acceptance thresholds.

### 8. Publish and assemble

Accepted assets promote their bytes (see "Candidate lifetime and accepted bytes"), publish a
digest-bound interface, and own a deterministic import script as the unit's identity-derived
replay file. Scene assembly consumes those interfaces in stable dependency order, instances
reusable parts, and owns the in-scene composition evidence: placement, instancing counts, and
composed silhouette at judge frames.

Asset-local qualification silhouette and in-scene composed silhouette are different
propositions with different owners; they are named differently so one cannot pay the other's
debt. Cumulative empty-scene replay crosses the standard per-artifact barrier (HIR-0117)
before any successor script, interface check, or evidence reader runs.

## View-prep loop and multi-image generation

This is the missing instrument on a `generate` source unit. Today's live path is one
prose-described `isolate_regen` plate into single-image Meshy. That is both too dumb and too
loose: it invents a product shot from a sentence, then throws away every extra view the
adapter already accepted as a list.

The loop is closed-loop mutation with read-back, the same standard as `run_bpy` plus
`inspect_scene`. Each step is a typed tool. Free-form "make it look more 3D" is not a step.

### Authority split

| Artifact | Authority |
| --- | --- |
| Crop / region handle from `measure_ref` | Witness. Authorizes that this part may generate. Never optional. |
| Derived plate handle | Untrusted generator input. Parent crop + closed operation + prompt/settings hash. |
| Meshy `thumbnail_urls` | Diagnostic read-back of the GLB. Not acceptance. |
| Blender qualification | Acceptance. Empty-scene import, dimensions, roles, silhouette. |

A plate that cannot name its parent crop is not a legal Meshy input. A text-to-image of the
subject with no crop is not a legal Meshy input. Regenerating a white-background product
shot from a prose `subject` and no region handle is the legacy defect, not a fallback.

### Closed plate operations

The source unit may apply only this vocabulary. Each call returns a new plate handle and a
read-back (the image plus a cheap identity card against the parent crop).

1. **`isolate_crop`** — lossless pixel crop of the witness region. Prefer this when the
   subject is already separated and lit.
2. **`isolate_cutout`** — background removal of crop pixels. Preserves shot photometry.
3. **`isolate_relight`** — image-edit of the crop: same silhouette and markings, even studio
   light, plain background, subject complete in frame. Use when the crop is dark, motion-
   blurred, or cluttered *and* cutout is not enough. Identity-gated against the crop.
4. **`remove_occluder`** — image-edit that erases overlapping geometry that is not this
   part (a hand in front of a statue, a neighbouring column). The remaining silhouette of
   the named part must still match the crop.
5. **`complete_unseen`** — image-edit that fills faces the camera did not see. Highest
   risk. Records `hidden_geometry: inferred` on the candidate envelope. Illegal when the
   unit's bound contracts require proven hidden topology; then the legal action is abstain
   or procedural. Identity-gated on every *visible* contour; unseen contours are marked
   inferred and cannot self-certify later qualification.
6. **`orbit_view`** — image-edit that mints one additional declared camera of the *same
   object*: `front`, `three_quarter`, `left`, `right`, or `back`. Parent is the primary
   isolated plate, not a random ref. Identity-gated against that primary.

Illegal operations: restyle, change identity, add ornament absent from the crop, generate
from text only, send a whole reference frame, invent a fourth angle when a real extra crop
of the same part already exists.

Real extra crops from other judge frames of the same part outrank minted orbits. The loop
collects every witness crop first, isolates each, and only then mints orbits to fill the
Meshy slot count.

### Identity gate

Before a plate may enter the Meshy envelope it must pass a deterministic identity card
against its parent:

- subject still occupies the plate (not an empty white field);
- silhouette / structure fingerprint stays within a declared band of the parent crop or
  primary isolate;
- no second semantic object appeared.

Failure names the operation, the parent handle, the observed drift, and the legal next
actions: retry that operation once, try a cheaper operation (`cutout` instead of
`relight`), or abstain. The remaining plate-prep budget is finite and compiled into the
unit card; exhausting it is `cannot_express_in_scope`, not another session. A model saying
the plate "looks like the statue" does not pass the gate.

### Meshy adapter, inspected rather than assumed

The live client is wrong. `image_to_3d` posts `image_url` (singular) to
`/openapi/v1/image-to-3d`. `MeshyBackend.to_glb(images, out)` keeps `images[0]`. Extra
angles the view-prep loop paid for never leave the process.

The documented Multi-Image to 3D contract (Meshy OpenAPI, 2026) is:

- `POST /openapi/v1/multi-image-to-3d`
- `image_urls`: 1–4 images of the **same object from different angles**; data URI or public
  URL; `jpg` / `jpeg` / `png`
- for `meshy-7` / `latest`, the **first** image is the primary (front) view; remaining
  order does not matter
- `ai_model`: `meshy-5` | `meshy-6` | `meshy-7` | `latest` (`latest` resolves to Meshy 7,
  which conditions on all input views together)
- `should_texture`: source units set this **false**. A generated GLB is a mesh carrier;
  imported materials would mix shading into the mesh write-cluster (HIR-0083, HIR-0112).
  Texture maps, if requested at all, are look-layer side artifacts through dressing
  (ADR-0007), not this unit's scene write
- `texture_image_urls` (1–4, `meshy-7` only) is therefore not a source-unit input
- `remove_lighting` exists for texture jobs; irrelevant when `should_texture` is false
- `multi_view_thumbnails: true` returns `thumbnail_urls` {front, right, back, left} of the
  **generated mesh**. That is adapter read-back, analogous to `inspect_view`, and mints no
  payment handle
- `input_task_id` may point at a succeeded Meshy Image-to-Image (or Text-to-Image) task,
  including one created with `generate_multi_view: true` (Meshy returns three angle URLs).
  That is an allowed **adapter-internal** chain when the harness has only one isolated
  plate and chooses Meshy to mint the extra angles. The isolate plate still has to pass
  the identity gate first. Text-to-Image with no reference image is not a legal
  `input_task_id` source for this harness
- empty, unreadable, or truncated GLB is envelope failure before Blender (HIR-0080 applied
  to adapters)
- polling has a positive idle deadline (HIR-0138 applied to adapters)
- no silent retries; a failed task is a recorded candidate failure

Pin `ai_model` in the envelope. Do not send `latest` as an unbound moving target once a
unit digest exists; `latest` at call time is recorded as the resolved model id.

Two legal call shapes, in preference order:

1. **Harness plates → `image_urls`.** One to four identity-gated plates. First slot is the
   primary isolate (front). Used whenever the loop produced the views.
2. **Identity-gated isolate → Meshy Image-to-Image `generate_multi_view` →
   `input_task_id`.** Used only when a single real view exists and minted harness orbits
   failed identity, or when the compiled unit card names Meshy as the orbit mill. The
   isolate plate remains in the envelope as the witness parent.

An adapter that receives N>1 plates and posts single-image `/image-to-3d` is a contract
violation, not a backend quirk. Tests inject four plates and fail closed if the HTTP body
contains `image_url` instead of `image_urls`, or if `len(image_urls) != 4`.

### Loop shape on the source unit

```text
witness crop handles (required)
        -> isolate each real crop (crop, then cutout or relight if needed)
        -> identity gate vs parent crop
        -> if more Meshy slots remain, mint orbit_view from the primary isolate
        -> identity gate vs primary isolate
        -> freeze 1–4 plate handles + envelope
        -> POST multi-image-to-3d (or input_task_id chain)
        -> persist GLB, task id, settings, input hashes, thumbnail_urls
        -> compare mesh thumbnails to plates (diagnostic; may reject the candidate)
        -> Blender qualification (authoritative)
```

Parallelize isolate/orbit evidence; serialize the Meshy call and later import. One
authoritative candidate per attempt. Thumbnail disagreement can spend one remaining
generation attempt with a repaired plate set; it cannot lower Blender thresholds.

### What this replaces

| Legacy | Replacement |
| --- | --- |
| `isolate_regen` from a prose `subject` | Plate ops on a crop handle |
| One view into `/image-to-3d` | 1–4 views into `/multi-image-to-3d` |
| Adapter drops `images[1:]` | Adapter fail-closed unless every plate is in the body |
| Agent eyeballs `preview.png` | Identity gate, then Meshy thumbnails as diagnostics, then Blender qualify |
| `should_texture: true` default | `should_texture: false` on the source unit |

## Minimum contract shape

The exact schema belongs to the exit ADR, but every asset proposal should carry fields
equivalent to:

```yaml
part_id: facade.window_bay.standard
semantic_roles:
  - building.facade.window.standard
unique_models_required: 1
estimated_instances: 80
required_frames: [38, 75, 112, 150, 188]
projected_importance: supporting
reference_support:
  strength: strong
  observations:
    - handle: refobs-9f2c41       # immutable harness-issued observation handle
      ref: refs/frame_4p5s.jpg    # resolved source, audit context
      region: [0.12, 0.18, 0.76, 0.72]
construction:
  route: procedural               # closed enum; legality derived, not declared
  reason: repeated_dimension_critical_grid   # audit context only
requirements:
  geometry: rigid
  materials: [pale_stone, dark_window]       # look-layer vocabulary, not this unit's write
  animation: none
  hidden_geometry: not_required
dependencies:
  - building.mass
acceptance:
  - deterministic_clean_scene_import
  - facade_grid_alignment
  - required_view_silhouette
uncertainty: low
```

A `generate` proposal names its witness crop handles, the closed plate operations it is
allowed to run, the expected Meshy slot count (1–4), expected output carrier, topology class,
required cleanup, and generator-specific qualification obligations. Derived plate handles are
produced at build time, not authored at materialization. The proposal compiles into ordinary
`WorkUnit` fields plus the `construction` extension; it is not a parallel document class with
its own authority.

## Application to the reference fixtures

These three scenes are fixtures for evaluating the mechanism. Their names, counts, and
vocabulary prove reproduction only and never appear in runtime code, prompts, or gates.

### Caesar Curia

Recommended strategy: hybrid.

Procedural or Blender-native parts: hall shell; checkerboard floor system; tiered bench
system; dais and steps; reusable columns; simple architectural trim.

Good image-to-3D candidates: classical statue silhouettes; ornate focal and side chairs;
rigid or minimally animated background senator bases; hero figures only if the output can
satisfy rigging and close-view requirements.

Expected planning result: approximately 14–18 semantic asset definitions implemented through
roughly 8–11 reusable source models, depending on how characters and chairs are shared.

### Hansa Silk Road

Recommended strategy: mostly procedural.

Procedural parts: hero tower mass and stacked floor modules; façade masks or modules; podium;
secondary tower variants; city-block scatter; cloud cards or volumes; green energy, emission,
and title systems.

Possible image-to-3D use: silhouette exploration for selected secondary towers, followed by
qualification or procedural reconstruction.

The shot's difficulty lies in camera motion, skyline composition, emission timing, and
transformation — not in sculptural asset construction. A monolithic generated tower would
provide little benefit, and the energy/emission/title systems are not mesh-family work at all.

### Room 1046

Recommended strategy: modular procedural construction.

Procedural parts: tower and corner mass; window bay, pier, sill, cornice, and entrance
modules; roofline assembly; hero window opening; transition tunnel; aligned interior wall;
camera path and targets.

Possible image-to-3D use: paneled hero door; isolated roof ornament; streetlight or other
rigid dressing if a reusable asset is unavailable.

The façade grid and hero-window alignment are dimension-critical, so generating the full
building would work against the shot's main requirements.

## Implementation sequence

### Phase 1: Typed reference measurement and decomposition proposals

Extend the reference instrument to region observations with immutable handles. Run
decomposition on the three fixtures as materialization evidence input — typed part proposals,
counts, route decisions, witnesses, and abstentions. No scene mutation and no standalone CLI:
the output is judged as compiled materialization input.

Acceptance: repeated modules are distinguished from instances; every route decision carries
witnesses or bound-contract derivations; the planner can conclude that image-to-3D is not
useful; ambiguous parts remain explicit; no fixture name, count, or vocabulary is encoded in
runtime code.

### Phase 2: Route authority in the unit schema and gates

Add the `construction` field to the `WorkUnit` contract, its digest participation, and derived
legality at staging, `finalize_materialization`, and the plan gate. Land the negative gates
first — they are cheap and generator-free: a unit with alignment/instancing contracts cannot
route `generate`; a non-mesh-family unit cannot route `generate`.

### Phase 3: Asset candidate and provenance boundary

Add the candidate artifact contract for generated and retrieved assets: immutable inputs
(crop handles and plate handles), output hashes, generator identity and settings, licensing
metadata, cost, and exact unit digest, plus the adapter failure discipline (zero-work
fail-closed, idle deadline, no silent retries). Candidates remain under run evidence until
qualification and explicit promotion.

### Phase 4: View-prep loop and multi-image adapter

Add the closed plate operations, identity gate, finite plate-prep budget, and a Meshy client
that posts `image_urls` (1–4) to `/multi-image-to-3d` with `should_texture: false` and
`multi_view_thumbnails: true`. Inject four plates and fail closed if the body is single-image
`/image-to-3d` or drops views. Inject an identity-failing `orbit_view` and prove Meshy is
never called. Retire `isolate_regen`-from-prose as a legal path in the same change.

### Phase 5: Deterministic Blender qualification

Add clean-scene import, normalization to a pure mesh-family write, semantic-role binding,
mesh/material inspection, target-provenanced dimension checks, required-view diagnostic
rendering, and rejection reporting with contract, observed value, and next actions. Every
mutation has a typed read-back.

### Phase 6: Promotion, digest-bound publication, and replay

Promote accepted bytes into `build/`, publish the hash-verified import script as the
identity-derived unit file and the digest-bound interface, and prove that cumulative replay
reconstructs the same scene state before successors and evidence readers run. Retire
`import_asset` and the freestanding asset-builder flow in this phase (strict migration,
ADR-0004).

### Phase 7: Broaden asset classes

Start with rigid props and background dressing. Then consider architectural ornament and
modular secondary assets. Characters, deformable assets, and close-up hero models come only
after topology, rigging, and motion qualification are proven.

## First vertical slice

The slice runs through the real pipeline — a published unit DAG, not a standalone
decomposition CLI:

1. Decompose all three fixtures with no mutation, as typed materialization input.
2. A repeated dimension-critical grid cannot route to `generate` — derived from its bound
   alignment and instancing contracts.
3. Energy, emission, and title work cannot become an image-to-3D request — its derived write
   cluster is not mesh-family.
4. One isolated rigid prop runs view-prep: crop, isolate, identity-gated orbits or extra
   real views, then `multi-image-to-3d` with those plates, then qualifies. A single dropped
   extra view is a failed adapter, not a successful one-image generate.
5. An injected identity-failing orbit never reaches Meshy.
6. An injected bad mesh — wrong scale, fused parts, missing provenance, or retained baked
   lighting — fails with the violated contract, the observed value, and the legal next
   actions.
7. The accepted import script replays from empty, through the standard barrier, before the
   instance successor runs.
8. A unit script referencing run-scoped candidate bytes fails promotion; only `build/`-homed,
   hash-verified bytes publish.

Steps 2 and 3 land first: they are cheap and generator-free. Step 4's adapter contract
(four plates in the HTTP body) can be proven with a fake transport before a paid Meshy call.
Steps 4 through 8 are the critical path. Together the slice tests the important mechanism:
the planner can choose generation, reject generation, prepare multiple legal views, refuse
illegal plates, and distinguish geometry from look or animation work.

## Evaluation strategy

Generality must be proven on heterogeneous fixtures and injected failures.

Required fixture categories should include:

- modular architecture with a moving camera;
- organic or sculptural props;
- repeated background populations;
- procedural city or landscape fields;
- a scene with inadequate reference coverage;
- a scene where no image-to-3D call is justified.

Injected failures should include:

- wrong scale or orientation;
- fused semantic objects;
- missing or invalid provenance or licensing;
- topology incompatible with declared use;
- an asset that matches one frame but fails another required view;
- a generated output with baked lighting;
- a generated output whose retained materials would make the source unit a mixed
  mesh-plus-shading cluster;
- a generator success envelope with empty or unreadable output;
- four identity-gated plates posted as a single `image_url`;
- an `orbit_view` whose identity card fails, yet Meshy is still called;
- a plate whose parent crop handle is missing;
- a prose `isolate_regen` with no crop handle treated as a legal input;
- a stale candidate bound to a superseded unit digest;
- a unit import script referencing run-scoped or shot-root bytes instead of promoted bytes;
- an attempted monolithic scene import where bounded parts were required.

Useful measurements include:

- unique models proposed versus final accepted unique models;
- instances created from accepted reusable parts;
- generated candidates accepted, repaired, and rejected;
- Blender and model cost per accepted visible asset;
- camera-frame coverage of accepted parts;
- replay success rate;
- repair isolation: whether one rejected part can be replaced without rebuilding siblings;
- plate identity failures that stopped Meshy versus those that leaked through;
- extra views prepared versus extra views present in the adapter HTTP body;

## Architectural boundaries

- Domain contracts define asset proposals, route decisions, provenance, qualification
  findings, and publication interfaces.
- Reference interpretation and qualitative decomposition live behind bounded agent roles and
  typed instruments that mint witness handles.
- Generator HTTP adapters live in `infrastructure/`; the current `vfx_harness.assets` package
  dissolves into the declared boundaries as part of the migration. The Meshy client must
  implement Multi-Image to 3D; single-image `/image-to-3d` is not a compatible substitute
  when more than one plate is frozen.
- View-prep (isolate, relight, de-occlude, orbit) lives behind bounded builder tools that
  mint plate handles. Image backends (Codex / Higgsfield) are infrastructure. Identity
  cards are evidence.
- Blender owns import, normalization, geometry inspection, and deterministic replay.
- Evidence owns diagnostic renders and comparisons; a model verdict cannot replace executable
  checks.
- Observability owns immutable inputs, outputs, costs, transcripts, and provenance.
- Orchestration serializes authoritative asset publication and scene integration while
  candidate and evidence production parallelize.
- Generated run output never becomes authority by proximity (ADR-0002).

If implementation changes core planning or publication authority, it requires the relevant
ADR/HIR update and migration rather than an implicit compatibility path.

## Leaving research

Two records graduate this note:

- [ADR-0009](../decisions/ADR-0009-construction-route-authority.md) for construction-route
  authority and candidate lifetime: the closed route enum and its derived legality, where
  promoted bytes live under `build/`, digest participation of non-default routes, and the
  retirement of shot-root asset authority.
- [HIR-0162](../improvements/HIR-0162-construction-route-replaces-ungoverned-asset-import.md)
  for the legacy asset mechanism — the freestanding asset-builder flow, shot-root `assets/`
  authority, runtime `import_asset` selection, prose `isolate_regen`, and the Meshy client
  that posted one `image_url` while discarding extra views. Captured failure modes: plan-grep
  targeting, isolated regeneration without a crop handle, preview self-certification, and
  silent single-image fallback. ("Stage 2.5" is informal and appears in no record; the HIR
  names the mechanism, not a stage number.)

Fixture names and counts (Caesar, Hansa, Room 1046) prove reproduction only and never enter
runtime code, prompts, or gates.

## Recommendation

Proceed with the shot-aware asset planner, not a generic "generate the scene from an image"
feature. The most valuable capability is the decision system around image-to-3D:

1. measure what the shot actually needs, through instruments that mint witnesses;
2. decompose it into reusable semantic parts;
3. choose the construction route per part, with legality derived rather than declared;
4. treat generated output as an untrusted candidate behind a disciplined multi-image
   adapter, after a typed isolate / complete / orbit loop with identity read-back;
5. qualify, normalize, and promote only evidence-backed, replayable assets;
6. assemble the final scene from bounded accepted units.

This approach reduces manual construction while preserving the VFX Harness principles of
explicit authority, local repair, deterministic replay, provenance, and earned acceptance.
