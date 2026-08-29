# Atomic work-unit gate and typed publish-interface proposal

Status: promoted to HIR-0083 and HIR-0084  
Scope: materialization-time atomicity enforcement and typed successor interfaces  
Authority: historical; binding copy is `AGENTS.md` plus those HIRs

The derived-cluster predicate and reference-only interface registry are implemented.
Do not rematerialize Layer 2 to satisfy the gate. First acceptance materialization is
Layer 3.

## Scope correction

The pipeline architecture already provides the execution loop:

- sparse global ownership and dependency publication through ADR-0005 and ADR-0006;
- layer materialization of a bounded work-unit DAG;
- deterministic legal ready-set calculation;
- JIT planning of one selected work unit;
- scoped build, freeze, replay, checkpoint, and durable lifecycle transition;
- transactional DAG correction through `apply_replan`.

HIR-0054 already establishes that active context must scale with the current unit rather
than the shot. This proposal does not replace those mechanisms, add a new planning
level, or redesign the global plan.

The missing enforcement is narrower:

1. Materialization can still publish a heterogeneous work unit.
2. Accepted units do not yet expose sufficiently typed, digest-bound successor
   interfaces.

The useful change is to make oversized units unpublishable and make their accepted
outputs consumable without script inspection or a new model-facing catalog.

Nothing in HIR-0053–0082 implements this gate or this interface registry. Those records
own other load (context, recipes, helpers, black-frame diagnosis, image payment). This
proposal must not absorb them.

## Layer-2 classification

An HIR is failure→mechanism. The earlier draft used `lighting_atmosphere` as an example
without pinning which producing failures an atomicity gate would have prevented. This
pass classifies Layer 2 records against that question. Do not retrofit Layer 2; its
remaining failures already have owning records.

### Heterogeneity-shaped (Proposal A would have refused the published unit)

| Record / run | Mixed clusters | What the session then had to guess |
| --- | --- | --- |
| HIR-0018, `atmosphere_and_bloom`, generation `088b4a7c…`, runs `20260825T0447…` / `20260825T070153Z-f6cb7d` | Compositor bloom (`world.bloom` / `world.bloom.compositor`) and volume atmosphere under one repair owner | Role string vs compositor host, VolumeScatter/Background as density control, two mutation families in one script |
| `lighting_atmosphere` charter (later L2 generations), e.g. HIR-0076 run `20260827T232542Z-3c222e` | Declared mutation `world.lighting_rig` **and** `world.atmosphere_volume` plus controls `lighting_arc_intensity` and `atmosphere_density` | After black plates, renderer-policy writes; light placement vs World extinction in one session |
| HIR-0072, run `20260827T221413Z-6f548c` | Light-owned session created a material volume; Density socket label matched World-volume guards | Which graph owned `Density`; World vs local volume as one “atmosphere” lever |

HIR-0051 (volume-only unit owning required mesh `visible_fraction`) is a related
ownership hole. The vis-repair-owner lint already refuses it. An atomicity cluster that
separates volume writes from mesh-vis evidence would have refused the same published
shape; HIR-0051 remains the owning mechanism for that row.

### Not heterogeneity (do not cite as Proposal A evidence)

These Layer 2 failures have their own mechanisms. An atomicity gate would not have been
the earliest cause:

- unpaid / mis-metric image debts (HIR-0048, HIR-0064);
- black-frame diagnosis without a scene cause card (HIR-0066, HIR-0069, HIR-0082);
- node inspection, recipe bodies, control-vs-role tagging, renderer policy (HIR-0061,
  HIR-0062, HIR-0063, HIR-0065, HIR-0076 as the *write*, not the mixed charter);
- sealed-sibling grade poisoning atmosphere (protection / image-payment provenance;
  ADR-0008 / HIR-0053 territory);
- physically impossible materialization response rows and critic-only blocking axes
  (evidence design, not unit grain).

Proposal A is not architecture-for-elegance: HIR-0018 and the mixed
`lighting_atmosphere` charter are real two-family publishes. It is also not a blanket
explanation of Layer 2. Promotion fixtures must reproduce the mixed-cluster rows, not
the black-frame or image-debt rows.

## Proposal A: derived atomicity gate

A materialized work unit must be one independently testable semantic publish. The write
hook and plan gate refuse the candidate when **derived clusters** are heterogeneous
without a typed exception.

The gate’s load-bearing test is a **computable signature**, not a field the
materializer authors. Anything the materializer writes solely to satisfy a lint is
HIR-0017 padding: every rejection becomes answerable by declaring a “coherent family,”
and the gate decays to advisory. Nothing self-certifies, including atomicity.

Precedent: HIR-0057 refuses a geometry unit whose protected vis producer sits outside
its dependency closure. That predicate is computed from the candidate alone. Atomicity
is held to the same standard.

### Derived cluster signature

From the candidate `WorkUnit` and its bound contracts/claims only — no authored
`family`, `primary_subject`, or `mutation_family` fields — compute a frozen set of
clusters. Each cluster is:

```text
(role_namespace, host_class, evidence_domain, instrument_family)
```

**Role namespace.** For every `mutates.roles` selector, take the first two dotted
tokens (`world.lighting_rig` → `world.lighting_rig`; `lookdev.materials.primary` →
`lookdev.materials`). Distinct two-token prefixes are distinct namespaces.
`mutates.dresses` selectors do not create a write namespace; they are the dressing
exception input.

**Host class.** Derived from `provides` and whether the namespace is only dressed:

- `camera` if `provides` contains `camera`;
- `geometry` if `provides` contains `geometry`;
- `dress` if the selector appears only in `dresses`;
- `control_host` otherwise (lights, worlds, node graphs, empties — distinguished
  next by instrument family, not by a free-form host string).

**Evidence domain.** Union of required claims’ `asserts` / bound contract metric
domains (`scene`, `temporal`, `projected_composition`, `image`). Image-domain debts
do not by themselves create a second write cluster; they follow the mutation
namespace that must pay them.

**Instrument family.** Map bound required contract *kinds* and compiled helper
eligibility (the same inventory HIR-0025/HIR-0079 already compile from worker
source) onto a closed registry. Initial families:

| Family | Derived from |
| --- | --- |
| `mesh` | geometry provides; mesh/count/topology scene kinds |
| `light` | light object/data mutation; energy/shape/distance kinds; `bvfx_light` |
| `volume` | World or material volume graphs; density/scatter kinds; `bvfx_volume` / `bvfx_volumetric_world` |
| `compositor` | glare/vector-blur/compositor node kinds; `bvfx_vector_blur` / bloom helpers |
| `keyframe` | `keyframe_schedule`, `curve_derivative_max`, `bvfx_interp` |
| `shading` | material/node-tree mutation; `dresses` assignment |
| `camera` | camera provides; transform/lens kinds |

Unknown contract kinds and unknown helper stems fail closed (same pattern as
`vfx-harness.look-vector/v1`). The family table is a versioned registry, not shot
vocabulary.

**Rule.** More than one write-cluster, after applying typed exceptions, is a blocking
finding. The finding **names the clusters** (namespaces, host classes, families), the
applicable exceptions that were considered and rejected, and the legal next actions
(split, declare assembly by consuming an interface, bind dressing, reassign evidence
owner). The materializer’s only lever is to restructure the unit. There is no
`coherent_family` string to set.

**Declared coherent family** is not a field. A *family* exists only when it is
derivable: every write namespace shares a two-token prefix, **or** the extra
namespaces are consumed typed assembly interfaces (Proposal B), not additional
`mutates.roles`. A comment or title that says “lighting and atmosphere” is not a
family.

### Atomic publish rule

A normal work unit publishes only when all of the following hold:

- it has exactly one write-cluster after exceptions;
- its claims can prove the result without relying on an unbuilt sibling;
- it has one repair owner (`evaluation` repair_owner / unit id), which is the same
  ownership model HIR-0056 compiles into `fault_owner_options` for abstention;
- it publishes at least one named typed interface useful to a successor;
- it can be checkpointed and rolled back without discarding unrelated accepted work.

Create another unit when the derived signature would gain a second write-cluster, or
when dependency, primary moment, repair route, or rollback boundary changes so that
the existing cluster is no longer independently testable.

The rule is the smallest independently valid publish, not one Blender object,
primitive, node, frame, or light.

### Independently testable splits

A split is valid only when each resulting unit has useful completion evidence and a
downstream interface.

```text
iris_blade_master ----+
                      +--> iris_assembly
iris_frame -----------+
```

`iris_blade_master` may publish its mesh, hinge origin, pivot, dimensions, and
instance interface before the assembly exists. `iris_assembly` consumes those
interfaces and proves radial placement. Both are independently meaningful.

Splitting an inseparable generator into one unit per generated primitive creates
administrative boundaries without independently valid publishes and is refused or
merged.

### Typed exceptions to one write-cluster

Atomicity must not erase existing cross-owner mechanisms. Exceptions are *derived*
from existing typed fields, never from an `exception: dressing` authoring knob.

#### Dressing

ADR-0007: owner-granted `dressable` plus consuming `dresses`. Assignment-only
appearance on another owner’s geometry is not a second geometry write-cluster. The
dressed owner’s geometry contracts remain protected. An ungranted selector still
fails dressing validation; atomicity does not reopen it.

#### Visibility repair and protection

HIR-0051 / HIR-0057: a `visible_fraction` repair owner provides camera or
mutates/dresses every named role; geometry providers freeze-protect lifecycle-active
vis, and same-layer producers must sit in the dependency closure. Protecting or
observing a ray-dependent contract is not a second write-cluster. A volume-only unit
still cannot own required mesh-visibility repair.

#### Bounded coordination

An interaction claim may balance exact enumerated shared controls when coordination
owner, protected atomic claims, and permitted controls are declared. That is a typed
exception, not permission to combine arbitrary modeling, lighting, volume, and
compositor writes.

#### Assembly

An assembly unit may consume typed publish interfaces and author relationships,
instances, or placement without permission to edit source assets’ internal geometry.
Consumed interface namespaces do not count as additional write-clusters because the
assembly does not mutate those producer export roles. A `depends_on` edge cannot
strip a mixed cluster or grant mutation of an accepted producer.

### Initial rejection examples

The gate should reject (derived clusters named in the finding):

- one unit whose `mutates.roles` span `world.lighting_rig` and
  `world.atmosphere_volume` with light and volume instrument families;
- one unit that binds compositor glare kinds and World-volume density kinds under one
  repair owner (`atmosphere_and_bloom`);
- one unit that creates an asset and performs unrelated shot lighting;
- one unit whose proposed split cannot publish independently;
- one unit with no named successor interface;
- one unit whose repair owner cannot legally change the evidence it owns.

## Proposal B: typed publish interfaces

Today a sealed unit exports roles, controls, capabilities, and accepted contract
outcomes, but a successor may still infer operational protocol from scripts. That is
the same guess HIR-0025 killed for helper inventories and HIR-0079 killed for return
contracts, one level up. Extending HIR-0054’s compact predecessor interface with
typed, digest-bound kinds is the next instrument.

### Kind registry

Interface kinds are a closed canonical registry (`vfx-harness.publish-interface/v1`),
unknown kinds rejected — the same identity rule as `vfx-harness.look-vector/v1`
(ADR-0003). Initial kinds, added only when a successor actually consumes them:

- `asset_source`
- `instance_source`
- `placement_control`
- `material_slot`
- `light_control`
- `volume_control`
- `cache_output`
- `render_pass`

### Schema invariant: references, never producer prose

Every exported value is a validated role token, control token, or sealed contract id.
Any scalar (dimensions, pivot, origin, count) is exported **only** as the contract id
that measured it. A string such as `"origin": "hinge"` is illegal: it is a producer
assertion with a schema stamp, not an executable fact.

Illustrative legal shape:

```json
{
  "id": "iris.blade.instance_interface",
  "kind": "instance_source",
  "schema": "vfx-harness.publish-interface/v1",
  "producer": {
    "layer_id": "3",
    "unit_id": "iris_blade_master",
    "unit_digest": "…"
  },
  "exports": {
    "role": "asset.iris.blade_master",
    "pivot_control": "iris.blade.hinge",
    "mesh_contract_id": "iris-blade-mesh-valid",
    "origin_contract_id": "iris-blade-hinge-origin",
    "dimensions_contract_id": "iris-blade-dimensions"
  },
  "digest": "…"
}
```

### Staleness rides `apply_replan`

Interface validity is **derived** when the ready set and successor cards are compiled:
the producer’s durable `unit_hash` must equal `producer.unit_digest` on the interface,
and the producer must not be `superseded` or `blocked`. There is no parallel interface
lifecycle document. `apply_replan` remains the single invalidation closure (HIR-0040,
HIR-0049, HIR-0052). A digest change or supersession makes prior interface bytes stale
because the ready-set compiler will not emit them, not because a second state machine
marked them invalid.

### Compiled successor consumption

Publish interfaces are durable harness state. They are **not** a new raw `Read`
surface for the materializer or builder. When a successor is selected, the compiled
kickoff card (HIR-0054) contains only the exact interfaces named by its dependency
closure. The builder reads that card. It does not contain:

- the global interface catalog;
- unrelated subject inventories;
- producer scripts;
- producer evaluator internals;
- sibling or future interfaces;
- a directory path the model can explore.

A successor addresses a missing, stale, or incompatible interface through a typed
dependency or plan defect. It does not inspect the producer script and guess the
protocol.

## Scheduling remains unchanged

The scheduler continues to calculate the legal ready set from accepted durable state
and derived interface validity. It should use tuple-stable declared order for multiple
ready units.

This proposal does not add critical-path, earliest-beat, fan-out, cost, or risk
heuristics. Additional scheduling policy requires evidence of starvation or material
throughput loss and its own decision record.

Authoritative mutation remains serialized. Independent frozen evidence may still run
in parallel.

## JIT behavior remains unchanged

After a unit passes:

```text
freeze and replay
  -> publish accepted unit and typed interfaces
  -> update durable lifecycle
  -> calculate tuple-stable legal ready set (interfaces derived from producer digests)
  -> select the next unit
  -> compile its exact charter and dependency interfaces
  -> run its JIT unit plan
```

The unit planner does not reread a subject inventory, redesign the layer DAG, or
choose among arbitrary future models. If the selected unit cannot be expressed as one
derived write-cluster, it emits a typed plan defect and routes through `apply_replan`.

## Shot-level implication

This proposal does not require replacing story-time layers 3–9 with a global
asset-department DAG. Those layers may remain ownership boundaries for their beats.
Their internal DAGs should be independently testable subjects and operations.

**Do not retrofit Layer 2.** It is mid-flight; mixed-charter residue is historical
evidence for fixtures, not a live rematerialization mandate. Remaining Layer 2
failures route to their owning HIRs.

**Acceptance run is Layer 3.** It is the first assembly-heavy layer. The worked
example is that layer’s grain, and the first materialization under the rule is also
its validation. Prefer discovering an over-eager gate on a fresh materialization
rather than on a replan of accepted work.

```text
iris_blade_master ----+
iris_frame -----------+--> iris_assembly --> iris_unlock_animation
rim_light_interface --+                         |
                                                +--> ingress_readability_integration
```

Each unit stays inside the ingress owner and publishes a typed interface to its
successors.

## Non-goals

This proposal does not:

- redesign global planning;
- replace ADR-0005 or ADR-0006;
- add a new subject-inventory or interface-catalog raw `Read` surface;
- author atomicity fields the materializer can pad;
- replace beat layers with fixed department layers;
- hardcode model, asset, department, or unit counts in core;
- grant downstream units authority to repair upstream work;
- weaken claim closure, image debt, freeze protection, replay, or provenance;
- add scheduler optimization heuristics;
- treat smaller prompts as proof of better visual quality;
- rematerialize Layer 2 to satisfy the gate.

## Fail-without validation fixtures

Promotion requires tests that fail before the mechanism and pass only with the
intended gate and interfaces.

### Atomicity rejection (Proposal A)

A materialized unit whose derived clusters include both `light` (`world.lighting_rig`)
and `volume` (`world.atmosphere_volume`) under one repair owner must be rejected at
the materialization write hook and the plan gate. The finding must name both clusters.

The same for compositor-bloom kinds plus World-volume kinds (`atmosphere_and_bloom`).

An authored title or comment that calls those roles one “family” must not publish.

### Authored-field padding (Proposal A)

If a candidate could satisfy an old lint by setting a `coherent_family` string while
keeping two write-clusters, that candidate must still fail. The suite pins that
padding is unrepresentable.

### Independently valid asset publish (Proposal B)

`iris_blade_master` must publish a valid mesh/pivot/instance interface without
`iris_assembly` existing. An assembly that uses that source declares `consumes` for the
interface id/kind and remains unready until it seals and the producer digest matches durable
state. A genuine ordering-only `depends_on` remains legal without inventing a consumed
interface and exposes no producer interface. Changing authored `publishes` must change the
producer digest and invalidate the downstream closure.

A changed blade unit digest must drop the prior interface from the successor card
through the existing replan/ready-set path, not a parallel stale flag.

### Reference-only exports (Proposal B)

A publish interface whose `exports` contain a non-token, non-contract-id scalar
(`"origin": "hinge"`) must be refused at authoring.

### Dressing preservation (Proposal A)

A material unit with a valid owner-granted `dresses` selector must still publish. The
gate must not count dressing as a geometry write-cluster. An ungranted selector must
still fail.

### Visibility preservation (Proposal A)

A camera-shaped visibility binding and a geometry provider carrying required active
visibility protection must still publish. A volume-only unit owning required mesh
visibility repair must still fail (HIR-0051). A geometry unit whose protected vis
producer is outside its dependency closure must still fail (HIR-0057).

### Compiled-context boundary (Proposal B)

The successor kickoff must contain its exact required publish interfaces and exclude
unrelated interfaces, raw inventories, producer scripts, and catalogs. No new `Read`
path to an interface directory.

### Transactional replan

Splitting an oversized unit through `apply_replan` must preserve unaffected accepted
units, supersede the replaced digest, omit stale interfaces from successor cards, and
reopen only the required closure.

## Evaluation

The promotion HIR names these decisive metrics; the rest of the earlier list is
observability, not the acceptance bar:

1. **Rejection precision** — mixed-cluster fixtures refuse; legal single-cluster
   units publish.
2. **Exception correctness** — dressing, visibility protection, assembly, and
   coordination still publish; ungranted dressing and volume-owned mesh vis still
   refuse.
3. **First-pass unit acceptance** on Layer 3 materialization under the gate
   (heterogeneous held-out grain, not only iris).
4. **Script-inspection attempts** — successor sessions do not Read producer scripts
   to recover origin, pivot, or instance protocol.

Tokens, retry width, replay, and look quality remain logged. A cheaper run that
lowers replay or look quality is not an improvement.

## Promotion boundary

Keep this document in research until the named fixtures exist.

**Proposal B** may become an HIR when the kind registry, reference-only export
invariant, digest-derived staleness, and successor-card compilation have fail-without
tests. Failure evidence already exists in harness history (successors inferring
protocol from scripts; HIR-0025/HIR-0079 at the wrong altitude).

**Proposal A** may become an HIR only with the derived-cluster predicate in code (no
authored family field) and fixtures cloned from HIR-0018 / mixed
`lighting_atmosphere` charters. Do not open that HIR on elegance or on Layer 2
failures owned elsewhere.

Neither HIR introduces a new global architecture. They extend JIT materialization,
ready-set, replay, HIR-0054 compiled context, ADR-0007 dressing, HIR-0051/0056/0057
ownership, and `apply_replan`.

## Related authority

- `docs/decisions/ADR-0003-explicit-metric-and-temporal-evidence-identity.md`
- `docs/decisions/ADR-0005-sparse-global-publication-contract.md`
- `docs/decisions/ADR-0006-unit-first-evidence-materialization.md`
- `docs/decisions/ADR-0007-dressing-authority.md`
- `docs/improvements/HIR-0017-lifecycle-aware-authority-and-agent-instruments.md`
- `docs/improvements/HIR-0018-selector-diagnostics-say-both-sides.md`
- `docs/improvements/HIR-0025-unit-scope-was-a-translation-job.md`
- `docs/improvements/HIR-0051-occlusion-needs-a-ray-changing-owner.md`
- `docs/improvements/HIR-0054-active-unit-context-must-not-scale-with-the-shot.md`
- `docs/improvements/HIR-0056-abstention-must-route-to-the-semantic-fault-owner.md`
- `docs/improvements/HIR-0057-geometry-vis-protection-must-be-dependency-satisfiable.md`
- `docs/improvements/HIR-0079-helper-inventory-must-publish-return-contracts.md`
- `docs/architecture/pipeline-end-goal.md`
- `docs/architecture/staged-pipeline.md`
