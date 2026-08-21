# Staged, Evidence-Driven Pipeline Architecture

> **Status: target architecture.** This document describes the intended redesign. It is
> not a claim that every mechanism below is already implemented. Current behaviour remains
> documented in `README.md` and `docs/ARCHITECTURE.md` until each migration phase lands.

## End goal

The pipeline must handle simple and complex VFX shots with the same approach:

1. Decompose the work into the smallest meaningful dependency-ordered units.
2. Give each unit one bounded semantic mutation objective.
3. Validate that objective with the narrowest trustworthy evidence.
4. Save an accepted checkpoint.
5. Repeatedly prove that the growing scene still contains everything accepted earlier.
6. Replay the full chain from an empty scene before final acceptance.

Complexity should increase the number of planned work units, not the size of one prompt,
one agent conversation, or one unbounded repair. The harness must support an arbitrary
planner-generated dependency graph without containing assumptions from the shot used to
develop it.

The governing principles are:

- **Plan dynamically; execute narrowly.**
- **Persist evidence; do not depend on conversation memory.**
- **Parallelize evidence production; serialize authoritative mutation and integration.**
- **Measure what can be measured; use visual judges for qualitative residuals.**
- **Repair the semantic owner; never compensate downstream.**
- **Fail closed; do not silently revive obsolete contracts.**

## Why the current layer boundary is insufficient

A department layer remains useful because it owns a coherent responsibility such as layout,
materials, animation, lighting, FX, or finishing. The problem is treating that entire layer
as one planning, agent, validation, and repair transaction.

A complex layer may contain several subjects, control families, reference moments, evidence
modes, and regression risks. In one open-ended session:

- the agent must retain too many constraints at once;
- unrelated fixes become coupled;
- passing frames are easy to regress;
- repair prompts must rediscover large amounts of context;
- expensive full-layer judgement repeats work unrelated to the defect;
- a process interruption can strand a partially edited artifact;
- one broad score can hide a failed requirement behind several passing ones.

The solution is not to discard layers. It is to keep the layer as the **ownership boundary**
and add planner-generated **work units** as first-class execution boundaries inside it.

## Work hierarchy

```text
shot
  -> department layer (semantic ownership boundary)
       -> planner-generated work-unit DAG
            -> bounded mutation
            -> local evaluation
            -> accepted checkpoint
       -> cumulative layer regression
  -> cross-layer regression
  -> full-chain acceptance from an empty scene
```

### Layer

A layer declares:

- the axes and semantic responsibilities it owns;
- upstream interfaces it consumes;
- downstream interfaces it publishes;
- its work-unit dependency graph;
- the final cumulative script artifact;
- its layer-wide acceptance boundary.

### Work unit

A work unit is the smallest useful planning, build, validation, checkpoint, and repair
transaction. It declares:

- one concrete goal;
- dependencies on accepted units;
- semantic roles and controls it may mutate;
- contracts and interfaces it must protect;
- reference moments and an explicit primary moment;
- static, keyframe, or motion evidence policy;
- executable claims and qualitative claims;
- completion, checkpoint, repair-owner, and rollback rules.

Create another work unit when any of these change materially:

- semantic subject or scene region;
- control family or mutation mechanism;
- dependency order;
- reference moment or temporal regime;
- required render/evidence mode;
- repair ownership;
- risk of breaking accepted work;
- ability to state the work as one bounded objective.

The number and names of units are shot data. They are never fixed in core code.

## Target schema

The staged schema is strict and versioned. The following is illustrative; names, frames,
subjects, and unit count come from the planner:

```json
{
  "schema": 4,
  "layers": [
    {
      "id": "N",
      "title": "Department title",
      "script": "build/NN_department.py",
      "owns": ["axis_a", "axis_b"],
      "stages": [
        {
          "id": "bounded_unit",
          "plan": "plans/NN_department/01_bounded_unit.md",
          "depends_on": [],
          "mutates": {
            "roles": ["department.subject.part"],
            "controls": ["control.subject.property"],
            "script_spans": ["build/units/NN_department/01_bounded_unit.py"]
          },
          "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze"
          },
          "evaluation": {
            "primary_judge": 40,
            "judge": [{"frame": 40, "ref": "refs/f040.png"}],
            "temporal_evidence": "none",
            "claims": [
              {
                "id": "claim.subject.property",
                "proposition": "One independently decidable proposition.",
                "axis": "axis_a",
                "property": "property",
                "subject_roles": ["department.subject.part"],
                "subject_controls": ["control.subject.property"],
                "moments": [40],
                "kind": "atomic",
                "required": true,
                "authority": "executable_required",
                "repair_owner": "bounded_unit",
                "evidence": [{"kind": "scene_contract", "id": "contract-id"}]
              }
            ]
          },
          "completion": "all_required_claims_and_protected_contracts_pass"
        }
      ]
    }
  ]
}
```

There is no implicit primary frame. The current loader sorts judge frames numerically, which
can silently change a plan's declared primary moment. Schema 4 requires the primary moment
explicitly and rejects missing, duplicated, or inconsistent declarations.

Evidence policy is also explicit. The harness does not infer whether a unit needs motion
evidence from axis names or keywords.

For multi-unit layers, every unit owns one distinct replayable script. The layer-level
`script` is a composed publication artifact and is never a shared mutation target. It is
written only after all unit transactions pass, then replayed from empty with prior layers.

## Just-in-time planning

The global plan is a dependency and ownership map, not an execution script. Each layer has a
short charter, and each work unit receives its own just-in-time plan after its dependencies
have sealed outcomes.

```text
plans/NN_department.md                 layer charter and work-unit DAG
plans/NN_department/01_first_unit.md   bounded execution ticket
plans/NN_department/02_next_unit.md    created after unit 1 seals
...
```

A unit plan contains only:

- its goal and non-scope;
- accepted dependency outcomes;
- permitted semantic mutation surface;
- required and protected claims;
- relevant references and evidence modes;
- known rejected mechanisms;
- explicit convergence and escalation clauses.

This prevents a stale whole-layer plan from becoming execution authority and keeps context
proportional to the current decision.

## Transactional replanning

Execution is allowed to prove that a plan is wrong. A missing work unit, incorrect dependency,
misowned claim, or mutation scope that cannot express the required fix is a **plan defect**, not
permission for the builder to work around the DAG.

Replanning is a first-class transaction:

```text
execution raises a typed plan defect
        -> freeze the last accepted state
        -> planner proposes a versioned DAG amendment
        -> validate ownership, scope, and dependency effects
        -> compute the complete downstream invalidation closure
        -> mark replaced units superseded and terminal dependants blocked
        -> preserve unaffected accepted checkpoints
        -> publish the amended plan and ledger record atomically
        -> resume at the earliest invalidated or newly inserted unit
```

The amendment records its owner, trigger evidence, old/new plan hashes, added/removed units and
edges, changed claims/scopes, explicitly preserved outcomes, invalidated outcomes, and reason.
An accepted plan is never edited silently in place. If amendment validation fails, the current
accepted plan and checkpoints remain authoritative.

Reopening sealed work is legal only through this transaction. Downstream units whose dependency
cannot currently pass become `blocked`; outcomes replaced by the new DAG become `superseded`.
Neither state may be interpreted as a failure of the scene or as permission to compensate later.

## Validation pyramid

Validation widens as accepted work accumulates:

1. **Mutation validation** — the intended semantic state actually changed.
2. **Work-unit validation** — focused contracts and visual claims prove the local goal.
3. **Interface regression** — active persistent upstream interfaces still hold.
4. **Layer regression** — every accepted unit in this layer still works in combination.
5. **Cross-layer regression** — the new layer preserves earlier visual and structural claims.
6. **Shot acceptance** — an empty-scene replay passes every final approval moment.

Running the entire expensive suite after every small mutation wastes time. A unit runs its
own evidence plus a risk-selected protected subset. Broad suites run at accepted checkpoints,
layer boundaries, and final acceptance.

Passing a warm render is never sufficient. The deterministic script and its empty-scene replay
are the artifacts of record.

## Claims: the unit of judgement

The judge evaluates **claims**, not vague tickets or broad layer quality. An atomic claim is
one proposition, not necessarily one object or one frame.

Examples:

- one subject has visible adjacent-plane separation at one moment;
- one relationship inverts between two moments;
- one motion remains monotonic over an interval;
- one hierarchy holds among several subjects.

Every claim declares:

```json
{
  "id": "department.property.relationship",
  "owner": "layer.work_unit",
  "subject_roles": ["semantic.role"],
  "property": "one testable proposition",
  "moments": ["moment_id"],
  "evidence": ["beauty", "focus", "render_pass"],
  "required": true,
  "authority": "qualified_qualitative_required",
  "qualification": {
    "suite": "claim_type_version",
    "judge_model": "model_version",
    "prompt": "prompt_hash"
  },
  "repair_owner": "layer.work_unit"
}
```

The planner defines the claims. The judge never expands its own scope or decides what else
should block the unit.

## Single-claim and multi-claim judging

Separate judgement is the default. Claims must be isolated when they differ in repair owner,
control family, moment, crop, render pass, dependency, risk, or reasoning method. Failed,
borderline, disputed, or repaired claims are always rejudged alone.

Compatible claims may share one model call only when they have:

- the same frozen candidate and evidence bundle;
- the same moment and evaluation method;
- the same ownership/repair boundary;
- no dependency on one another;
- a bounded visual-attention and context cost.

Batching is a transport optimization, never logical aggregation. The judge returns a separate
verdict and cited evidence for every claim. Required claims combine with logical `AND`; passing
claims cannot average away a failed one.

If a batched claim fails, is borderline, requests focus, contradicts executable evidence, or
disagrees across judges, the orchestrator splits that claim out and rejudges only it with the
narrowest sufficient evidence. Unrelated passing claims are not repeated.

After required atomic claims pass, an explicit **interaction claim** may assess whether the
individually correct parts work together. Interaction judgement can identify focal competition,
visual imbalance, or lost coherence, but cannot contradict deterministic facts.

An interaction claim declares a `coordination_owner`, participating units, and the exact controls
that may be balanced. The coordination owner is not a blanket cross-layer repair owner. It may
adjust only the authorized shared relationship while every participant's atomic claims remain
protected. If balancing requires reopening sealed work or expanding scope, it must enter the
transactional replanning path. This separates legitimate balancing of correct systems from
forbidden downstream compensation for a broken upstream fact.

## Evidence hierarchy

Decisions use evidence in this order:

1. deterministic scene/interface facts;
2. executable image contracts;
3. isolated render passes and focused optical evidence;
4. atomic qualitative judgement;
5. interaction judgement;
6. adjudication only for genuine uncertainty.

Generic evidence primitives include semantic scene facts, region relationships, value ordering,
adjacent-region separation, gradients, edge rolloff, geometry state, material assignment,
keyframe behaviour, and controlled render passes. Plans bind these primitives to shot-specific
roles, moments, regions, references, and targets.

A critic may describe a qualitative residual. It may not fail a measurable assertion contradicted
by passing authoritative evidence. It should report the visible defect and violated relationship,
not prescribe an unverified implementation such as a specific light coordinate or mesh operation.

## Judge orchestration

```text
frozen candidate
      -> deterministic evidence extraction
      -> compatible atomic claim batches in parallel
      -> split uncertain claims and rejudge them alone
      -> deterministic per-claim aggregation
      -> explicit interaction claims, if planned
      -> conflict/borderline adjudication
      -> one ordered authoritative verdict
```

The final report is a claim-by-moment matrix, not one broad mean. Every row records pass, fail,
unjudgeable, or evidence conflict; cited evidence; repair owner; judge/prompt version; and the
candidate/settings hashes.

## Validating the judges

Judges require their own qualification suite. Each general claim type needs:

- a known-good candidate;
- an identical-image control;
- a targeted defective candidate;
- a near-threshold candidate;
- an irrelevant alteration that must not affect the claim;
- a mismatched or unjudgeable reference case.

Measure false-pass rate, false-failure rate, repeatability, correct evidence citation, scope
leakage, sensitivity to irrelevant changes, and whether failures help the builder converge.
Calibration is specific to the judge model and prompt version. Repeated calls to the same model
are not assumed independent; adjudication should use genuinely independent evidence or judge
configuration where possible.

### Qualification-gated claim authority

Claim authority is earned, not inferred from the planner marking a field `required`. Supported
authority levels are:

- `executable_required` — a qualified deterministic contract may block or pass autonomously;
- `qualified_qualitative_required` — this claim type, judge model, prompt, and evidence shape
  passed a declared qualification budget and may block autonomously;
- `human_required` — the claim matters, but autonomous evidence/judgement is not qualified;
- `advisory` — useful polish or diagnosis that cannot block acceptance.

Qualification budgets state maximum false-pass, false-failure, repeatability, scope-leakage, and
irrelevant-change sensitivity rates. The run records the exact qualification artifact and hashes.
Changing the judge model, prompt, evidence layout, or claim semantics invalidates that authority
until requalified.

A failed qualitative qualification does not automatically make an important requirement optional.
The required response is to create executable evidence, improve and requalify the judge, or route
the claim to audited human adjudication. Majority voting by repeated calls to one correlated model
is not a substitute for qualification.

The previously measured 17% verdict-flip rate is a warning, not a universal false-failure rate:
it was measured on a different judge model and does not justify multiplying independent-error
probabilities across claims. Every current judge configuration must be measured directly.

## Semantic mutation-scope enforcement

Blender mutations have derived side effects: dependency-graph evaluation, modifiers, material
slots, caches, animation evaluation, and generated geometry can change without representing an
unauthorized authoring decision. Therefore a raw datablock diff is diagnostic evidence, not the
sole mutation gate.

Enforcement has four layers:

1. **Preventive surface:** expose scoped semantic mutation operations and deny writes outside the
   work unit's declared roles, controls, and artifact spans where technically possible.
2. **Authoritative semantic diff:** compare declared role, control, interface, ownership, and
   contract facts before and after the mutation.
3. **Derived-state tolerance policy:** classify expected evaluated changes by mechanism and apply
   explicit numeric/hash tolerances rather than accepting arbitrary collateral edits.
4. **Raw scene audit:** retain broad datablock/file diffs for diagnosis and unknown-change alerts,
   but do not let incidental evaluated state create repair thrash by itself.

The checkpoint records the semantic before/after manifest, tolerance policy version, resolved
unknown changes, and any raw audit warnings. An unknown authored change blocks freeze until it is
classified, reverted, or added through a plan amendment.

## Frozen-candidate parallel evaluation

Mutation is sequential. Evaluation fans out only after the script, dependency manifest,
render settings, and candidate hash are frozen.

```text
single-writer mutation
        -> freeze candidate and canonical scene checkpoint
        -> isolated render workers by moment
        -> parallel checks, passes, crops, and claim judges
        -> ordered fan-in coordinator
        -> accept, reopen owning unit, or rollback
```

Workers never share a mutable Blender session. Every result proves it used the expected script,
dependency, scene, and render-settings hashes. Parallelism is resource-aware: a single GPU may
serialize renders while CPU checks and model calls proceed concurrently; multiple render devices
allow bounded render fan-out.

On a single-GPU machine this design primarily improves determinism, reproducibility, failure
isolation, and overlap between rendering, CPU checks, and judge calls. It must not promise linear
render-time speedup. Reports separate render queue time from parallel evidence time so the value
of fan-out is measured honestly.

Independent asset preparation and reference analysis may also run in parallel before integration.
Authoritative script/scene mutation, integration, accept, repair, and rollback remain single-writer.

## Checkpoints and durable memory

Every accepted unit records:

- cumulative script hash and accepted script snapshot;
- dependency/input manifest;
- comparison/render-settings hash;
- local and protected contract results;
- claim verdicts and relevant renders;
- rejected mechanisms and repair deltas;
- tool, time, cost, and context telemetry.

Selectors such as `all_active_upstream_interfaces` are planning conveniences only. At candidate
freeze they resolve to an explicit, sorted contract-ID set stored in the checkpoint. Evaluation,
repair, resume, and revalidation use that recorded closure, never a wildcard whose meaning may
change later.

These artifacts are durable memory across agents, restarts, context compaction, and fresh planning.
Conversation summaries may help reasoning but never become execution authority.

## Transactional repair

Repair begins from the last accepted checkpoint and receives a machine-authored manifest:

- failing claims and frames/moments;
- passing claims that must remain protected;
- verified evidence and focus regions;
- allowed semantic roles, controls, and script spans;
- locked candidate/render-settings hashes;
- rejected prior repair deltas and measured outcomes.

The repair transaction is:

1. Restore and snapshot the accepted artifact.
2. Apply one bounded semantic edit.
3. Freeze a new candidate.
4. Re-evaluate the failure and protected evidence.
5. Accept only monotonic progress without regression.
6. Otherwise restore the snapshot before another attempt.

Truncation, cancellation, SDK failure, worker failure, and unexpected exceptions are rollback
paths, not exceptional permission to retain an unvalidated edit. A failed unit routes to its
semantic repair owner; downstream compensation is forbidden.

If the repair manifest cannot express a necessary change inside its authorized scope, the repair
stops with a typed plan-defect result and requests transactional replanning. It must not broaden
its own permissions.

## State and reporting

A work unit follows an explicit state machine:

```text
pending -> planning -> building -> frozen -> evaluating -> passed
   |          |              ^                       |
   |          |              |---- repair/rollback <-|
   |          +-> plan defect -> replanning -> superseded
   +-> unsatisfied terminal dependency -> blocked

Any incomplete/error path -> restore accepted checkpoint -> failed or retryable
```

Parallel output is recorded as events but presented through one ordered aggregator:

```text
moment A: render -> contracts -> atomic claims -> FINAL PASS
moment B: render -> contracts -> isolated panel -> FINAL FAIL
```

Late model output cannot appear to reverse an already aggregated result. Reports show wall time,
compute time, model cost, render queue time, claim outcomes, repair count, and concurrency so an
optimization can be measured rather than assumed.

`blocked` means a dependency prevents legal execution; `superseded` means a versioned plan
amendment replaced the unit or its outcome. Neither is counted as a builder/scene failure. Resume
may proceed only after the dependency or amended plan supplies a valid next state.

## Human adjudication

Autonomous execution remains the default, but audited human adjudication is a valid terminal path
for `human_required` claims, reference contradictions, unresolved judge conflicts, or final
quality approval. A human decision records the evidence bundle, scope, decision, rationale,
identity, timestamp, and candidate/settings hashes. It may accept/reject the claim or authorize a
plan amendment; it may not silently mutate artifacts or erase prior evidence.

## Generalization and anti-overfitting

Core pipeline code must not contain:

- shot or subject names;
- object display-name contracts;
- fixed frames, layer counts, work-unit counts, or stage names;
- shot-specific coordinates or target values;
- department-specific assumptions presented as universal logic;
- keyword guessing for primary moments or evidence modes.

Shot knowledge belongs in briefs, references, plans, semantic roles, and executable contracts.
Core code supplies schemas, state transitions, generic evidence primitives, scheduling,
transactions, aggregation, and fail-closed validation.

No mechanism is called general because it worked on the development shot. It must be exercised
on heterogeneous held-out fixtures such as static architecture, products, characters, outdoor
environments, transparent/volumetric subjects, camera-driven scenes, and multi-beat animation.

Inject failures including missing roles, broken materials, flat form lighting, incorrect holds,
upstream regressions, interrupted edits, failed workers, contradictory judges, irrelevant image
changes, and out-of-order results.

## Architectural decisions and rationale

### Keep layers; add dynamic work units

Layers provide stable ownership and fault routing. Work units reduce context, mutation, evidence,
and rollback scope. Replacing layers entirely would lose the department boundary; keeping one
unbounded transaction per layer does not scale.

### Planner defines scope; judge does not

A judge choosing its own subjects or requirements makes the acceptance boundary nondeterministic.
The planner defines atomic propositions; the orchestrator may batch compatible calls, and the
judge evaluates only the supplied manifest.

### Claims aggregate logically, not by broad mean

An average lets several easy passes buy one critical failure. Required claims must each pass.
Interaction quality is evaluated separately after atomic correctness.

### Required authority is qualification-gated

Logical `AND` is safe only when each blocking input has earned authority. Otherwise adding claims
compounds judge noise and produces repair thrash. A qualitative requirement must be qualified for
the exact judge/prompt/evidence configuration, converted to executable evidence, or routed to
human adjudication before it can block autonomously.

### Replanning is an explicit transaction

Execution often reveals that the decomposition, dependency graph, ownership, or scope is wrong.
Versioned amendment plus deterministic invalidation preserves good work without forcing agents to
obey or silently rewrite a bad plan.

### Mutation scope is semantic, not a raw Blender diff

Blender's evaluated state changes transitively. Preventive semantic operations and authoritative
fact diffs enforce ownership; tolerance-classified derived state and raw audit telemetry preserve
visibility without turning harmless dependency-graph changes into false violations.

### Interaction uses coordination ownership

An interaction defect can exist between individually correct systems. A declared coordination
owner may balance only enumerated shared controls while all atomic claims remain protected.
Anything broader requires replanning, preventing “interaction” from becoming a loophole for
downstream compensation.

### Explicit policy replaces inference

Implicit primary-frame ordering and keyword-based motion detection already produced incorrect
work and unnecessary evidence. Primary moments, temporal evidence, ownership, and mutation scope
must be declared and validated by schema.

### Freeze before fan-out

Parallel evaluation is safe only when every worker reads the same immutable candidate. Parallel
mutation creates races, irreproducible verdicts, and unclear ownership.

### Evidence precedes critic prose

Builders optimize the feedback they receive. False passes stop early; false failures cause costly
thrashing. Moving measurable claims to executable evidence narrows critics to the visual decisions
they are actually suited to make.

### Every mutation boundary is transactional

An agent can edit successfully and fail before returning. Process success and file mutation are
not atomic, so the harness must supply the transaction and restore policy.

### Strict migration, no silent compatibility

Fallbacks allow stale plans, selectors, and schemas to regain authority. The new schema is a hard
cutover: old artifacts are explicitly migrated or rejected, never interpreted heuristically.

## What to do

- Decompose dynamically from dependencies, subjects, controls, evidence, and risk.
- Use one bounded goal and semantic mutation surface per work unit.
- Generate unit plans just in time from sealed outcomes.
- Save accepted checkpoints and manifests on disk.
- Validate locally, then widen validation at meaningful boundaries.
- Reuse frozen evidence and parallelize independent evaluation.
- Split uncertain batched claims and rejudge only the disputed proposition.
- Gate blocking qualitative claims on recorded judge qualification.
- Amend incorrect plans transactionally and invalidate only their dependency closure.
- Route repair to the semantic owner and protect every accepted dependency.
- Verify deterministic scripts by replaying from an empty scene.
- Demonstrate generalization with held-out fixtures and injected failures.

## What not to do

- Do not make a complex layer one giant plan or conversation.
- Do not hardcode the development shot into core behaviour.
- Do not let judges invent blocking scope or average failures away.
- Do not give an unqualified qualitative claim autonomous blocking authority.
- Do not ask a subjective judge to decide a stable measurable fact.
- Do not send motion strips or other evidence unless the claim requires them.
- Do not let concurrent workers mutate one scene or script.
- Do not retain unvalidated edits after any interrupted path.
- Do not silently rewrite a plan or broaden repair permissions when execution exposes a plan defect.
- Do not repair upstream faults with downstream compensation.
- Do not treat warm-scene success as proof of a deterministic artifact.
- Do not add compatibility fallbacks that weaken the strict contract.

## Implementation sequence

1. Introduce the smallest strict schema-4 slice: two work units, explicit primary/evidence policy,
   resolved protections, claim authority, replanning metadata, and the expanded state model.
2. Add schema validation, unit ledger, checkpoints, resume/rollback, and one schema-4 fixture;
   reject legacy staged inputs rather than adding a compatibility path.
3. Run that two-unit layer end to end with the existing serial renderer and existing judge. This
   early vertical slice must expose planning/state/integration failures before wider investment.
4. Add a layer charter, just-in-time unit planning, sealed outcomes, transactional DAG amendment,
   dependency invalidation, and rerun the vertical slice including a forced replan.
5. Enforce semantic mutation scopes, derived-state tolerance classes, explicit protection closure,
   and single-writer artifact ownership; rerun with injected unauthorized/derived changes.
6. Add generic evidence primitives, atomic claim manifests, qualification artifacts, authority
   gating, coordination ownership, and audited human-required outcomes.
7. Implement compatible claim batching, automatic isolation of uncertainty, interaction claims,
   deterministic aggregation, and judge qualification tests; rerun the same vertical slice.
8. Add repair manifests, process-failure recovery, protected regression, and typed escalation from
   impossible repair to transactional replanning.
9. Implement frozen-candidate render/evidence workers with resource-aware scheduling and hashes;
   verify serial single-GPU and bounded multi-worker paths separately.
10. Add ordered reporting and per-unit/per-claim queue, wall-time, cost, and tool telemetry.
11. Migrate the complete development shot and run it freshly from planning through acceptance.
12. Run heterogeneous held-out fixtures and injected failures before calling the architecture
   general.

## Verification strategy

The first end-to-end verification must be a **fresh run**, not a warm start from old outcomes:

1. Archive the previous run as a baseline.
2. Regenerate the global plan, schema, layer charters, and first unit plan from the source brief
   and references.
3. Ignore old outcomes, checkpoints, runtime evidence, and revalidation caches.
4. Build every layer and work unit from the beginning.
5. Exercise checkpoint, resume, repair, rollback, and parallel fan-in paths deliberately.
6. Replay the entire chain from an empty scene.
7. Run full-shot acceptance and final rendering.
8. Compare quality, false decisions, wall time, compute/model cost, repairs, and tool use with the
   archived baseline.

That run proves integration, not generalization. Generalization requires the heterogeneous and
injected-failure suite described above.

Before that expensive run, every implementation phase above must leave a working vertical slice.
The slice deliberately exercises at least one pass, local repair, rollback, resume, plan amendment,
blocked dependency, superseded outcome, unqualified qualitative claim, and ordered fan-in. There
is no long dark migration period in which the architecture cannot run end to end at small scale.

## Definition of done

The redesign is complete when:

- complexity scales by adding bounded planner-generated units rather than enlarging one session;
- every unit has explicit scope, evidence, checkpoint, repair owner, and stop condition;
- plan amendments are versioned transactions with explicit preservation and invalidation closure;
- `blocked` and `superseded` outcomes cannot be mistaken for scene failures;
- no primary moment or evidence mode is inferred implicitly;
- measurable claims are executable and qualitative claims are atomic and evidence-cited;
- autonomous blocking claims have current qualification or deterministic authority;
- required failures cannot be hidden by averaging;
- interaction repairs have bounded coordination ownership or trigger replanning;
- semantic mutation enforcement tolerates declared derived state without hiding unknown edits;
- frozen evaluations are reproducible and safely parallelized;
- every interrupted mutation restores the accepted checkpoint;
- fault routing prevents downstream compensation;
- audited human adjudication can terminate claims the autonomous judge is not qualified to own;
- empty-scene replay remains the source of truth;
- fresh full-chain verification passes; and
- held-out scenes and injected failures demonstrate that the design is general rather than
  overfit to one shot.
