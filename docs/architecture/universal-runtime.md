# Universal Harness Runtime Flow

> **Status: target architecture and migration plan.** This document turns the overhead and
> failure modes observed during the fresh Beacon run into scene-independent runtime
> requirements. It complements [`staged-pipeline.md`](staged-pipeline.md),
> which defines the broader work-unit and evidence architecture, and
> [`pipeline-end-goal.md`](pipeline-end-goal.md), which defines the pipeline's north star.
>
> This design is intentionally not backward-compatible with the former monolithic runtime.
> Obsolete plan, role, milestone, and judge contracts must fail closed rather than being
> guessed or silently adapted.

## Executive conclusion

Runtime overhead is not harmless bookkeeping. It makes the harness less accurate by:

- increasing irrelevant context and hiding the active unit's authority;
- forcing agents to rediscover paths, selectors, and lifecycle rules;
- turning a one-field contract mismatch into speculative scene edits;
- exposing sibling and future work to the wrong judge;
- encouraging early departments to compensate for unfinished later departments;
- making runtime grow with the total shot and its history instead of the active task;
- creating more opportunities for interruption, drift, and invalid recovery.

The fix is not another larger prompt. The harness must compile a bounded execution contract
before launching an agent, expose explainable typed interfaces, route each claim to the
correct decision authority, and treat unit, layer composition, and shot acceptance as
different lifecycle boundaries.

The governing scalability requirement is:

> Runtime context, cost, and uncertainty must scale with the active work unit's complexity,
> not with the total scene, global-plan length, or accumulated run history.

## Evidence from the fresh run

The fresh Beacon run revealed several generic architectural failures:

1. The JIT planner repeatedly loaded a roughly 650-line global plan, the whole brief, and
   whole-shot contract files for small atomic units. It also tried stale repository-root
   paths before recovering to the actual shot directory.
2. An already-current unit plan was reread and rewritten instead of being accepted through
   provenance and input hashes.
3. The first Layer 1 unit was initially judged against contracts belonging to the pedestal
   and hero-placeholder units. The builder correctly refused to expand scope, but the
   harness still paid for invalid judgment and repair.
4. Executable-only units were initially sent to a visual critic even though exact bound
   checks could decide them deterministically.
5. A generic minimum-brightness image gate was applied to an intentionally unlit layout
   unit, despite solid and matcap diagnostics already proving its form.
6. A passed work-unit milestone such as `1@room_shell` reached layer-outcome code that still
   assumed every passed milestone ID was a numeric layer ID.
7. Composed-layer review inherited no active cost context after unit reporting cleared it.
8. A material contract returned `value=None`. The builder spent more than four minutes
   trying node names, labels, arrays, socket variants, and ownership fields because the
   checker did not report which selector stage failed.
9. The plan instructed the builder to write `bvfx_material_role`, while the authoritative
   resolver uses the universal `bvfx_role` property for material-role selection.

These failures are not specific to an orb or a beacon. The same patterns will recur with
unfamiliar assets, larger shader graphs, denser animation, more layers, and more complex
dependency graphs unless the runtime flow changes.

## Why the present flow does not generalize

### Model-driven discovery is being used as runtime compilation

The active agent searches files and reconstructs which constraints matter. This makes the
model responsible for dependency resolution, context selection, path correction, and
authority discovery every time it starts.

Those are deterministic harness responsibilities. Leaving them to the model causes context
size and latency to grow with the whole shot, even when the active task remains small.

### Evidence is insufficiently explainable

A final reading such as `None` says only that the check could not produce a value. It does
not distinguish among:

- no object matched the semantic role;
- the object matched but had no material;
- no material matched `material_roles`;
- several materials matched when exactly one was required;
- no node matched `node_roles`;
- several nodes matched;
- the requested graph did not exist;
- the socket name or direction did not resolve;
- the socket resolved but its value had an unsupported type.

Without this trace, the builder must reverse-engineer the checker. That is slow and unsafe.

### Judgment is not one operation

Atomic facts, qualitative appearance, interactions, temporal continuity, and final shot
quality require different decision boundaries. Treating them as one generic critic call
causes premature judging, sibling leakage, and double jeopardy.

### Lifecycle types remain implicit

A work-unit pass, composed-layer pass, revalidation pass, and final acceptance pass are not
interchangeable. Encoding all of them as loosely formatted milestone strings lets obsolete
numeric-layer assumptions survive inside a staged runtime.

## Target architecture

```text
brief + references + amendments
              |
              v
        typed Intent IR
              |
              v
      claim/contract graph
              |
              v
     dependency-ready scheduler
              |
              v
        Unit Packet compiler
              |
              v
  one bounded Blender transaction
              |
              v
 claim-authority decision router
              |
              v
 sealed Unit Outcome + checkpoint
              |
              v
      composed Layer Outcome
              |
              v
       full Shot Acceptance
```

### 1. Typed Intent IR

Compile the brief and references once into a typed intermediate representation containing:

- semantic subjects and relationships;
- approval moments and transition laws;
- axes and ownership boundaries;
- reference sources and measured fingerprints;
- semantic roles and controls;
- layer and work-unit dependencies;
- explicit unresolved questions and amendments.

Runtime agents must not reconstruct this structure by searching prose files.

### 2. Claim and contract graph

Each claim must bind:

- an exact claim ID;
- subject roles or controls;
- a property;
- one or more moments;
- required or advisory status;
- decision authority;
- exact evidence IDs;
- repair owner;
- mutation owner;
- protected upstream interfaces;
- interaction participants and coordinator when applicable.

The graph is validated before Blender starts. Unknown semantic properties, unresolved
selectors, impossible thresholds, missing evidence, ambiguous repair ownership, and invalid
authority/evidence pairings are planning failures.

### 3. Dependency-ready scheduler

The scheduler selects one work unit whose declared dependencies have sealed outcomes. It
does not infer readiness from filenames or transcript history.

The scheduler owns:

- legal state transitions;
- interruption recovery;
- retry and rollback decisions;
- dependency invalidation after replanning;
- selection of protected upstream checkpoints;
- publication order.

### 4. Unit Packet compiler

The Unit Packet is the only runtime authority given to a builder. It is generated
deterministically and contains only:

- the active unit identity and typed run identity;
- active required and advisory claims;
- exact evidence bindings for the active moments;
- permitted semantic roles, controls, and script span;
- resolved dependency outcomes and protected interface readings;
- relevant brief clauses;
- relevant reference frames or measured crops;
- locked comparison settings;
- resolved technique snippets and verified API signatures;
- explicit non-scope;
- stop, checkpoint, repair, and rollback rules.

It excludes:

- sibling and future claims;
- the complete global plan;
- full predecessor transcripts;
- stale exploratory reasoning;
- the entire recipe library;
- unrelated reference frames;
- whole-layer mutation authority.

The packet must be sufficient to execute and decide the unit without filesystem discovery.
If the agent needs a missing technique or fact, it requests that item through an explicit
retrieval interface; it does not load the entire shot context.

### 5. Bounded Blender transaction

One work unit receives one warm Blender mutation stream. Before each mutation, the harness
checks the target against the unit's mutation manifest.

The transaction rules are:

- one authoritative scene writer;
- semantic targets resolved before execution;
- no mutation outside declared roles, controls, or script spans;
- read-only diagnostics remain available after convergence;
- no script publication during live mutation;
- one replayable delta artifact published during finalization;
- canonical replay from the declared dependency checkpoint before acceptance.

### 6. Explainable semantic interfaces

Use one universal semantic property:

```python
bvfx_role(target, "semantic.role", owner_layer="2")
bvfx_control(target, "semantic.control", owner_layer="2")
```

`target` may be an object, material, node group, node, world, collection, camera rig, or
other supported Blender custom-property host. Do not invent type-specific alternatives such
as `bvfx_material_role`.

Every contract result must include a resolution trace. For a material-node socket check,
the trace should include at least:

```json
{
  "object_matches": ["hero_orb"],
  "material_matches": ["orb_glass"],
  "graph_matches": 1,
  "node_matches": ["Principled BSDF"],
  "socket": "Roughness",
  "socket_direction": "input",
  "value": 0.42,
  "failed_at": null
}
```

When resolution fails, `failed_at` must identify the first failed stage and include the
requested selectors. A bare `None` is not an actionable result.

### 7. Claim-authority decision router

Decision routing is deterministic:

| Claim authority | Deciding mechanism | Appropriate boundary |
|---|---|---|
| Executable required | Exact bound scene/image/semantic checks | Atomic unit or composed regression |
| Qualified qualitative required | Qualified visual judge with declared evidence shape | Unit or composed layer, as declared |
| Human required | Explicit human decision artifact | Declared approval boundary |
| Advisory | Non-blocking diagnostics or polish notes | Any relevant boundary |
| Interaction claim | Coordinator-owned combined evidence | After all participants have sealed |

Consequences:

- executable-only atomic units do not call a visual critic;
- unit evidence is filtered to exact active bindings;
- sibling and future contracts cannot authorize a repair;
- generic beauty or brightness gates do not apply unless explicitly bound;
- the composed layer receives visual judgment only after all required subjects exist;
- final acceptance judges cross-layer relationships and temporal continuity;
- an atomic fact already proven by authoritative evidence is not re-litigated visually.

### 8. Typed lifecycle records

Replace overloaded string IDs with explicit records such as:

- `UnitRunId(shot, layer, unit, attempt, run_id)`;
- `LayerRunId(shot, layer, attempt, run_id)`;
- `AcceptanceRunId(shot, moment_or_suite, attempt, run_id)`.

Each lifecycle type owns its own:

- state machine;
- ledger rows;
- cost attribution;
- transcripts;
- checkpoints;
- outcomes;
- resume policy;
- report format.

Unit success does not imply layer success. Layer success does not imply shot acceptance.
Revalidation is a distinct deterministic path rather than a disguised new build.

### 9. Layer composition boundary

After all required units pass:

1. Replay their artifacts from the layer's declared input checkpoint.
2. Revalidate every protected upstream interface.
3. Evaluate interaction claims among the units.
4. Render the layer's declared judge moments with locked settings.
5. Run the layer's qualified visual judgment where required.
6. Publish one composed layer artifact and sealed Layer Outcome.

The composed judge sees the completed subject set. It does not send feedback to an arbitrary
unit; each actionable observation must resolve through claim bindings to the correct repair
owner.

### 10. Shot acceptance boundary

After every layer seals:

1. Start from an empty Blender scene.
2. Replay the complete artifact chain.
3. Verify persistent structural and semantic interfaces.
4. Evaluate approval moments and transition corridors.
5. Judge cross-layer interactions and final qualitative residuals.
6. Reject downstream compensation for an upstream-owned defect.
7. Publish a final acceptance outcome with complete evidence provenance.

## Planning cache and provenance

Planning is a content-addressed compilation step. A unit-plan cache key should include:

- relevant Intent IR slice hash;
- active claim/contract slice hash;
- dependency-outcome hashes;
- protected-interface hashes;
- amendment hash;
- planner prompt/schema version;
- planner model and relevant tool versions.

When the key matches and the plan passes the deterministic gate, launch no planner model and
do not rewrite the plan. A changed dependency outcome invalidates only its transitive
dependants, not the entire shot.

## Parallelization boundary

Parallelize work that consumes frozen inputs and produces independent evidence:

- planning for independent dependency-ready units;
- reference measurement and crop generation;
- rendering isolated frozen frames when resources permit;
- deterministic checks over frozen artifacts;
- multi-frame or multi-axis critic calls;
- plan and outcome audits.

Keep these operations sequential and single-writer:

- mutation of one Blender scene;
- artifact publication;
- checkpoint acceptance;
- dependency sealing;
- rollback and replan commits;
- repair of shared controls;
- composition of the authoritative chain.

The rule is: **parallelize frozen evidence production; serialize authoritative mutation and
integration.**

## Implementation order

### P0 — eliminate ambiguity at the active unit boundary

1. Create a semantic-role registry and remove `bvfx_material_role` and other alternative
   role properties.
2. Add selector-resolution traces to every semantic scene check and tool response.
3. Build the deterministic Unit Packet compiler.
4. Prevent runtime builders from reading `plans/global.md` directly.
5. Centralize authority-aware judge routing for unit, composition, and acceptance.
6. Scope live tool feedback and convergence guards to the active unit's exact evidence IDs.

### P1 — make staged execution durable

1. Introduce typed Unit, Layer, and Acceptance run identities.
2. Separate their ledgers, transcripts, costs, outcomes, and resume policies.
3. Add deterministic interrupted-unit recovery.
4. Add content-addressed JIT-plan caching.
5. Make composed-layer verification a first-class phase with its own context and reporting.
6. Revalidate protected same-layer dependency interfaces at each unit boundary.

### P2 — scale throughput safely

1. Add resource-aware frozen-render scheduling.
2. Parallelize independent evidence and judge calls.
3. Add selective regression based on mutation risk and dependency edges.
4. Add performance budgets for context size, planner invocations, check diagnosis, and
   recovery latency.

## Required success signals

The redesign is successful when:

- an atomic unit receives no sibling or future evidence;
- an executable-only unit makes zero visual-critic calls;
- a semantic selector failure identifies the failed resolution stage in one tool response;
- one universal role helper works for every supported Blender host type;
- an unchanged valid unit plan launches no planner model;
- a builder does not read the global plan at runtime;
- interruption resumes from the latest legal checkpoint without manual state edits;
- unit cost depends on its own packet and work, not on total shot history;
- composed judges see every required interaction participant;
- no department compensates for an unfinished later department;
- a full empty-scene replay remains the final deliverable.

Recommended operational budgets should be established empirically, but the harness should at
least report:

- Unit Packet size and token estimate;
- planner cache hit/miss and reason;
- selector-resolution steps and failure stage;
- number of active versus suppressed contracts;
- judge calls by authority and boundary;
- model and Blender time per unit;
- recovery source and replay distance;
- regression checks selected and skipped with reasons.

## Generalization verification

Beacon is a test fixture, not the architecture target. Validate the pipeline on a matrix of
unrelated shots varying:

- primitive versus imported assets;
- small versus large object counts;
- simple versus layered material graphs;
- static, keyed, and continuous motion;
- still, moving, and rolling cameras;
- bright, dark, volumetric, and stylized lighting;
- single-subject and multi-subject interactions;
- shallow and deep work-unit DAGs;
- short and long frame ranges;
- deterministic and qualified qualitative claims.

Use adversarial harness tests as well:

- correct node with wrong material role;
- correct material with duplicated node role;
- valid socket on the wrong graph;
- future sibling intentionally absent;
- visual judge inventing a measurable defect contradicted by evidence;
- failed upstream interface tempting downstream compensation;
- interruption during planning, live build, finalization, and composed review;
- dependency amendment invalidating only part of the DAG;
- unchanged plan and outcome eligible for a zero-model fast path.

The pipeline generalizes when additional scene complexity creates more explicit units and
evidence—not larger ambiguous prompts, broader mutation authority, or scene-specific code in
the harness.

## Immediate run decision

The fresh run should remain stopped at Layer 2 until the P0 semantic-interface and
explainable-check changes land. Layer 1 is passed and sealed. Resuming without those fixes
would mostly measure known harness friction and could train future prompts around an obsolete
interface rather than validate the universal architecture.
