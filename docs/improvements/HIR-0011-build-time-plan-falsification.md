---
id: HIR-0011
title: Move world-model falsification from planning into bounded build units
status: proposed
introduced_in: unreleased
date: 2026-08-22
failure_class: planning_simulates_scene_truth_before_any_build_checkpoint
mechanism: structural_plan_gate_plus_runtime_hypothesis_falsification
adr: ADR-0004
---

# Move world-model falsification from planning into bounded build units

## Outcome

Planning will establish enough authority to build safely, not attempt to prove the future scene
correct before Blender has built it.

The global plan remains a versioned DAG of intents, owners, mutation scopes, dependencies,
requirements, and executable contracts. The deterministic plan gate proves that this authority is
complete and internally coherent. The earliest producing work unit then evaluates scene-dependent
hypotheses against the real scene, real camera, real dependency outcomes, and exact contract
frames. A failed hypothesis becomes typed replanning evidence rather than a reason to add another
simulation layer to planning.

The operating rule is:

> Plan enough to mutate safely; build the smallest useful falsifier; revise authority from
> executable reality.

This is not permission to improvise around the DAG. Builders remain bounded by declared semantic
scope, and all authority changes still use transactional replanning.

## Observed failure

The user-supplied cost audit reports **$219.71 across 19 costed planning/gate runs** for the current
shot with **zero accepted build units**. Before accepting this HIR, that total must be reproduced
from canonical run cost records and recorded here with its exact run set; the architectural finding
does not depend on the last cent being exact.

The planning loop repeatedly tried to eliminate scene risk before execution. Each added planning
mechanism found another representational gap: warning tiers, typed evidence domains, executable
contracts, plan spikes, and finally contract-bound spike proposals. HIR-0010 records the individual
authority and lifecycle defects uncovered along that path and the mechanisms that correctly fixed
them.

The final fresh plan still produced a false-clean world claim. Its f36 iris evidence was generated
with the camera left at the f1 position. The approved camera schedule places the camera at a
different transform on f36, so the spike did not measure the state its prose claimed. Geometric
analysis of the published values alone shows the entire iris plane behind the approved f36 camera,
so the authoritative bbox evaluator must reject that scene on first contact. No Blender
reproduction of that state has been persisted; the producing unit's first honest f36 evaluation is
that reproduction. Planning had nevertheless spent another full model pass claiming reachability.

This failure is evidence of two distinct facts:

1. Spike citations need integrity when they are used. A spike must not claim a frame, selector,
   decision state, or contract it did not execute.
2. Even honest spikes are not a substitute for the producing work unit. Reconstructing enough of
   the future scene to prove every world-model claim is an alternate build pipeline with additional
   opportunities for drift.

The current worktree contains a partial, unverified contract-bound spike implementation started in
response to the false f36 evidence. This HIR narrows its purpose: it may protect the honesty of a
`spiked` citation, but it must not become a mandatory plan-time proof of scene reachability.

## Root cause

The architecture already states that execution may prove a plan wrong and that replanning is a
first-class transaction. Operationally, however, the plan gate accumulated two different kinds of
responsibility:

- **authority closure**, which planning can decide deterministically; and
- **world-model truth**, which depends on scene state that does not exist until execution.

The second category cannot be made authoritative by adding more planner prose or more elaborate
proxies. A plan spike is still a separately reconstructed scene. It can prove that a Blender
mechanism exists, but not that the eventual cumulative scene satisfies the same composition,
lighting, interaction, or motion relationship unless it reproduces the build—in which case it has
become a duplicate build.

The cost model then inverted. Paid planning rounds cost more than the narrow build units they were
intended to protect. Meanwhile the already-implemented checkpoint, retry, invalidate, and replan
transactions were not allowed to perform their intended role: absorbing executable feedback while
preserving accepted work.

## Decision criteria

The mechanism must:

- retain singular, immutable, content-addressed plan authority;
- retain requirements-register completeness and evidence-suitability checks;
- retain declared ownership, dependency, mutation scope, due gates, and protected interfaces;
- prevent builders from broadening scope or silently rewriting plans;
- let scene-dependent values fail cheaply against the real cumulative scene;
- distinguish a falsified planning hypothesis from an implementation defect;
- preserve accepted checkpoints and invalidate only the complete affected dependency closure;
- keep model context and spend proportional to the next decision;
- keep optional spike citations reproducible without making spikes a second build pipeline;
- remain generic across static, camera-driven, animated, volumetric, and multi-layer shots.

## Authority boundary

### Plan-time structural authority

These remain blocking before any build mutation:

- one selected immutable bundle and verified pointer identity;
- authored-input and decision-input provenance;
- schema validity and cross-record references;
- complete substantive brief-clause registration;
- every normative requirement resolved to a contract, obligation, or explicit decision;
- an acyclic work-unit DAG with resolvable dependencies;
- exactly one owner and repair owner for each required claim;
- declared semantic roles, controls, script spans, and control-to-role closure;
- declared evidence kind, judge moments, lifecycle, and due boundary;
- no circular entry obligation that depends on evidence produced by the gated unit;
- no missing or stale unit-plan authority for the next executable unit;
- no path traversal, mixed-run authority, disappearing adversary, or mutable bundle member;
- no human-locked decision silently changed by a planner or builder.

These checks prevent silent corruption. They are deterministic, cheap, and remain fail-closed.

### Build-time world-model authority

These are validated only after their producer has created the relevant state in Blender:

- projected bbox, screen position, visibility, occlusion, and camera clearance;
- whether a provisional camera path intersects visible geometry;
- geometry scale and placement relative to the real cumulative scene;
- animation timing, motion paths, return transforms, and terminal holds;
- material response, exposure, bloom, atmosphere, and lighting hierarchy;
- interaction between separately correct systems;
- qualitative resemblance and residual visual quality.

The plan must still declare these contracts and their owners. The plan gate validates their shape,
binding, lifecycle, and due unit, but does not claim their target values are reachable.

### Qualitative authority

Qualitative residuals remain bounded claims evaluated after deterministic evidence. A critic cannot
override executable facts, expand its own scope, or convert a failed upstream interface into a
downstream repair instruction.

## Decision strength

Plan decisions need an explicit strength instead of treating every recorded value as equally
immutable:

- **hard constraint** — explicit user or brief law. Execution may falsify feasibility, but only a
  human or a new higher-authority brief revision may change the value.
- **approved start** — an approved initial value with a named falsification contract and owner. A
  failed contract may propose a transactional amendment without pretending the original value was
  proven.
- **planner start** — a marked starting value chosen to make progress. Its owner may tune it within
  declared controls and bounds during normal unit convergence.
- **confirmed outcome** — a value sealed by accepted executable evidence and checkpoint identity.
  Downstream consumers treat it as protected authority until explicit invalidation/replanning.

Existing `values.contract` records remain the machine-readable value channel. The migration adds
decision strength and falsification ownership; it does not return to prose parsing.

## Target execution flow

```text
brief + references + durable decisions
        -> global plan: DAG, owners, scopes, contracts, due boundaries
        -> deterministic structural gate
        -> JIT plan for the earliest ready unit
        -> bounded build mutation in the real cumulative scene
        -> executable unit evidence
             -> pass: seal checkpoint and confirm produced outcomes
             -> local miss within declared controls: bounded repair and re-evaluate
             -> hypothesis falsified: emit typed plan finding
                  -> transactional replan
                  -> preserve unaffected checkpoints
                  -> invalidate affected dependency closure
                  -> resume at earliest legally reopened unit
        -> cumulative layer replay
        -> cross-layer replay
        -> full empty-scene acceptance
```

The first useful unit of a layer is the earliest falsifier for that layer's unconfirmed assumptions.
This does not require a synthetic `unit_0` in every layer. If the natural first producer can cheaply
create the necessary proxy and measure it, that unit owns the falsification. Split a separate proxy
unit only when doing so creates a meaningful checkpoint, repair boundary, or cost reduction.

For camera-driven layers, the earliest composition-producing unit must evaluate every composition
moment needed to seal the camera decision. A unit that checks only f1 cannot claim to have falsified
an f36 camera/geometry relationship merely because it authored the f36 keyframe.

## Failure routing

A failed runtime contract is classified before repair:

### Local implementation miss

The intended outcome remains expressible inside the unit's declared roles, controls, dependencies,
and decision strengths. The unit may repair within its existing bounded budget, then re-run its
failed and protected evidence.

Examples: a mesh count is off by one; a material role was not assigned; a planner-start radius
needs bounded tuning; an animation curve uses the wrong interpolation.

### Falsified planning hypothesis

Passing requires changing a hard/approved decision, adding or removing a dependency, changing
ownership, expanding semantic mutation scope, changing a required contract, or reopening a sealed
outcome. The builder stops and emits a typed plan finding containing:

- failed contract IDs and readings;
- exact candidate, checkpoint, bundle, and unit-plan hashes;
- relevant scene/render evidence;
- decision IDs and strengths involved;
- current roles/controls and the missing authority;
- affected downstream closure calculated by the orchestrator;
- no proposed silent mutation outside scope.

The public replan transaction consumes this record. The builder does not edit the plan.

### Infrastructure or adapter failure

Blender launch, filesystem authority, provider authentication, cancellation, worker failure, or
schema incompatibility routes to recovery rather than creative replanning. The last accepted
checkpoint remains authoritative.

## Planning policy and cost boundary

Default global planning becomes one bounded authoring transaction plus deterministic structural
validation. A second paid planning pass is justified only by machine-listed structural blockers or
an explicit adversarial register audit; it is not justified by unproven scene reachability.

Before scheduling another paid plan/repair round, the orchestrator compares it with the cheapest
ready unit capable of falsifying the disputed hypothesis. The comparison is policy/configuration,
not a shot-specific dollar constant in core code. When the executable falsifier is cheaper and safe
to run, execution wins.

The verifier's charter narrows to:

- missing or misclassified requirements;
- disputed authority and decision strength;
- ownership, dependency, scope, and due-gate defects;
- qualitative risks clearly labeled as unconfirmed;
- no aggregate claim that future scene contracts are reachable.

`--until-clean` may iterate deterministic structural corrections. It must not create an unbounded
model loop trying to make world-model claims true on paper.

## Spike policy

Spikes are optional and have two legitimate uses:

1. prove that an unfamiliar Blender/API mechanism works in the supported runtime; or
2. cheaply compare alternative mechanisms before selecting a ticket approach.

A ticket labeled `spiked` must cite a harness-authored immutable record containing the exact script,
output, Blender identity, render frame when applicable, and any exact scene contracts the ticket
claims the spike executed. Stale, missing, or failing contract bindings invalidate the `spiked`
label.

However:

- a composition-owning ticket is not required to recreate the future scene in a spike;
- an unspiked world-model value may proceed as an approved/planner start with a due runtime contract;
- passing spike evidence never upgrades a future cumulative-scene claim to a confirmed outcome;
- spike validation protects citation honesty, not plan-time truth.

The partial contract-bound spike changes currently in the worktree must be reviewed against this
boundary. Any rule that makes contract-bound composition spikes mandatory for publication should be
removed or redesigned before merge.

## Proposed implementation slices

### Slice 1 — Record the authority correction

- Amend ADR-0004 to distinguish structural commit validation from runtime contract truth.
- Preserve its bundle, pointer, requirements, lifecycle, and capability decisions unchanged.
- State explicitly that `clean` means structurally executable authority, not a promise that future
  scene contracts will pass.
- Add outcome terminology to the public plan/run summaries.

Target outcome: the repository has one unambiguous definition of what plan cleanliness certifies.

### Slice 2 — Classify contracts and decisions

- Add or derive an explicit validation phase/due boundary for plan-static, unit-runtime,
  layer-runtime, and final-acceptance evidence.
- Add decision strength (`hard_constraint`, `approved_start`, `planner_start`,
  `confirmed_outcome`) and falsification owner/path to typed decision records.
- Reject any world-model contract treated as confirmed without an accepted producing checkpoint.
- Keep all existing requirement-to-contract closure checks.

Target outcome: plans carry executable hypotheses without misrepresenting them as proven facts.

### Slice 3 — Narrow the plan gate

- Inventory every current plan-gate finding and classify it as structural, runtime, qualitative, or
  compatibility migration.
- Keep structural findings blocking.
- Route runtime reachability findings into the producing unit's due manifest instead of plan
  publication blockers.
- Remove prompt/gate language that asks a verifier to certify future scene truth.
- Keep current bbox/schema/binding checks; stop requiring a plan-time proxy to prove bbox
  reachability.

Target outcome: a structurally complete plan can start its cheapest safe falsifier without another
paid plan round.

### Slice 4 — Add typed hypothesis-falsified outcomes

- Add a model-free result type separate from generic unit failure.
- Produce it when resolution requires authority outside the unit's current plan.
- Include contract readings, decision strengths, scope/dependency conflict, evidence paths, and
  immutable identities.
- Prevent the run summary from reporting such a unit or run as passed.

Target outcome: contact with reality becomes durable replanning input rather than transcript prose.

### Slice 5 — Route through existing transactions

- Teach `vfx units replan` (or a narrowly named companion command) to consume a typed falsification
  record.
- Compute and preview the dependency invalidation closure before publication.
- Preserve accepted unaffected checkpoints and mark replaced outcomes `superseded`.
- Require human approval only when a hard constraint changes or configured policy demands it;
  approved/planner-start amendments follow their declared authority.

Target outcome: a wrong plan is cheap, recoverable, and auditable.

### Slice 6 — Reduce spike enforcement to citation integrity

- Complete typed spike records and stale-contract detection.
- Do not require a spike merely because a unit owns projected composition.
- Count a ticket as `spiked` only from typed evidence, not prose/regex alone.
- Add an exploratory/unbound spike mode that cannot be cited as authoritative proof.

Target outcome: optional probes are honest without becoming a duplicate pipeline.

### Slice 7 — Prove the workflow on real and heterogeneous fixtures

- Migrate the current camera/iris plan so f1 and f36 are evaluated by the earliest real composition
  producer.
- Treat the camera spine and iris placement according to their declared decision strengths.
- Build and accept the first empty-scene Layer 1 checkpoint before running another whole-shot plan
  refinement loop.
- Add at least one non-camera heterogeneous fixture whose first unit falsifies a different class of
  world hypothesis, such as material response or animation timing.
- Compare plan cost/latency, build-to-first-evidence cost/latency, replan count, and accepted units
  with the recorded baseline.

Target outcome: the mechanism is demonstrated beyond the development shot and improves the
economics it claims to improve.

## Current-shot migration

The selected plan bundle remains immutable evidence; do not edit it in place.

1. Preserve the false f36 spike as failure evidence; the producing unit's first honest f36
   evaluation supplies the executable reproduction.
2. Finish or revert the partial spike changes so only citation integrity remains.
3. Publish the ADR/HIR contract changes before changing plan-gate semantics.
4. Produce a new structurally valid plan generation or model-free promotion under the new gate.
5. Transactionally replan Layer 1 from its current durable base.
6. Make the earliest composition-producing unit evaluate the real f1 and f36 camera/iris state.
7. Start Layer 1; let executable evidence confirm or falsify the approved starting values.
8. On falsification, amend authority through the public replan path rather than another whole-shot
   planning loop.
9. Accept Layer 1 only after cumulative empty-scene replay passes its required claims.

No current bundle is downgraded or mutated to fit this workflow. The runtime and authority model
change first; the shot then migrates through the same public transaction future shots will use.

## Rejected alternatives

### Prove every world claim before building

Rejected because it creates a second approximate build pipeline. Its cost has exceeded the units it
protects, and its proxies can diverge from the cumulative scene.

### Rough prose plan and unrestricted builder discretion

Rejected because amnesiac, metered builders need durable ownership, scope, requirements, contracts,
checkpoints, and replay. Removing those protections would reintroduce silent corruption.

### Mandatory contract-bound spikes for all composition owners

Rejected because an exact-enough composition spike is already a duplicate build. Typed spike
records protect optional citation integrity; they do not become entry gates.

### Let a builder silently tune any failed decision

Rejected because hard constraints and accepted upstream outcomes would become meaningless. Tuning
is legal only for declared planner starts or through a versioned authority amendment.

### Treat every contract miss as a plan defect

Rejected because many misses are ordinary implementation defects repairable within declared scope.
Classification depends on whether passing requires new authority, not merely on failure.

### Patch the current shot plan or hand-edit its bundle

Rejected because it would bypass content-addressed publication and fail to fix the workflow for the
next shot.

## Validation plan

### Focused contract tests

1. A structurally incomplete plan still blocks before build.
2. A structurally complete plan with an unconfirmed bbox target publishes as executable authority
   without claiming the bbox has passed.
3. The producing unit evaluates that bbox in the real declared camera/frame state.
4. A miss within planner-start controls routes to bounded repair.
5. A miss requiring a hard/approved decision change emits `hypothesis_falsified` and cannot mutate
   outside scope.
6. A hard constraint cannot be changed by automatic replanning.
7. An approved-start amendment records old/new values and evidence.
8. Replanning preserves unaffected accepted checkpoints and invalidates the complete affected
   closure.
9. A `spiked` ticket with a missing, stale, narrower, or failing typed record blocks its evidence
   claim, while an otherwise valid unspiked ticket may build.
10. A run with no accepted requested unit cannot report `passed`.

### Integration fixtures

- Camera-driven fixture: the planned f36 camera places proxy geometry behind the camera. Planning
  passes structural closure; the first composition producer returns typed falsification; replan
  changes the approved start; replay passes f1 and f36.
- Non-camera fixture: a planned material or animation start is unreachable. The owning unit—not a
  plan spike—detects it, repairs or replans according to decision strength, and preserves unrelated
  checkpoints.
- Authority fixture: missing requirement, undeclared role, stale bundle, circular due gate, or
  changed hard decision still fails before mutation.

### Economic acceptance

Record for the baseline and migrated workflow:

- planning model cost and wall time before first build mutation;
- cost and wall time to first authoritative scene evidence;
- number of model planning/repair sessions;
- number of accepted units;
- number and classification of replans;
- replay/regression cost;
- repeated failure classes.

The change is accepted only if it preserves structural-authority failures while reducing cost or
latency to first authoritative scene evidence on both fixtures. A cheaper workflow that weakens
authority is not a pass.

### Repository verification

Run from the repository root:

```bash
.venv/bin/ruff check src tests
.venv/bin/python -m pytest -q
.venv/bin/vfx --help
git diff --check
```

Record exact results, fixture artifacts, run IDs, hashes, cost, and latency in this HIR. Do not mark
it accepted from unit tests alone; the current shot must reach its first accepted Layer 1
empty-scene checkpoint under the revised workflow.

## Release and rollback

Implement behind an explicit plan/build policy version until the heterogeneous fixtures and current
Layer 1 checkpoint pass. Existing immutable bundles retain their recorded policy and remain
readable. New publication records which policy classified plan cleanliness and decision strength.

Rollback restores the previous policy for new runs without mutating existing bundles or accepted
checkpoints. Typed falsification records remain valid evidence even if the automatic routing is
disabled.

## Implementation progress — 2026-08-22

The first generic runtime slice is implemented, but this HIR remains `proposed` until the current
shot and heterogeneous executable fixtures satisfy the economic acceptance criteria.

Implemented:

- ADR-0004 now defines plan cleanliness as structural authority rather than future scene truth.
- Assumptions and append-only decision records understand `hard_constraint`, `approved_start`,
  `planner_start`, and `confirmed_outcome`. Legacy strength-less records deterministically read as
  hard constraints; a confirmed outcome requires an accepted checkpoint hash.
- Approved/planner starts require a producing owner and runtime contract path, and the plan gate
  checks that both exist in the DAG/claim graph.
- Projected-composition ownership no longer requires a plan spike. An unspiked structurally valid
  ticket can reach its producing unit.
- A ticket that claims `spiked` must cite typed evidence whose exact script, complete output,
  Blender executable/version, current contract rows, and results are immutable and hash-checked.
- Work-unit state has a distinct `hypothesis_falsified` outcome. The record pins bundle, plan,
  unit, unit-plan, candidate, and comparison-setting identities; records observations, decisions,
  authority conflict, evidence, and affected closure; and blocks dependants in the same state
  transaction.
- The deterministic `contract_gap` path emits this outcome. A terminal unit failure whose failing
  bound contracts sit on a decision's declared falsification path emits it too: classification is
  by declared authority, never by diagnosing why the contract missed. Ordinary executable failures
  remain repair/failure outcomes; uncertainty does not gain plan-defect authority; and a recording
  error degrades to a visible `failed_unrecorded_plan_finding` instead of a silent ordinary
  failure.
- `vfx units replan --falsification ...` verifies the record against the explicitly named base
  bundle and DAG. Hard constraints require `--hard-constraint-approval`. `--preview` validates and
  prints the invalidation/preservation closure without publishing state.
- Plan-gate and run summaries expose structural validation policy and falsification outcomes.

Not yet implemented in this slice: the end-to-end camera-driven and heterogeneous integration
fixtures (a real producing unit measuring the scene and returning the typed record through a full
build), the current-shot migration, and the economic acceptance comparison. The bound-contract
routing itself is unit-tested at the builder seam with the current shot's f36 housing contract as
the fixture case.

Verification after this slice:

```text
.venv/bin/ruff check src tests
All checks passed!

.venv/bin/python -m pytest -q
135 passed in 34.61s

.venv/bin/python -m tests.integration.test_harness
ALL PASS (0 failed)

.venv/bin/vfx --help
passed; public commands include units

.venv/bin/vfx units replan --help
passed; exposes --falsification, --hard-constraint-approval, and --preview

git diff --check
passed
```

No paid plan or Blender build was run for this code-only verification. Therefore these results do
not establish the cost/latency improvement or accept HIR-0011; the next evidence boundary is the
current shot's first real Layer 1 producing unit and checkpoint.

## Remaining limitations

- The user-supplied $219.71/19-run baseline still needs canonical reproduction and an immutable
  cost-report path.
- Legacy satisfied resolutions conservatively default to hard constraints. A future migration may
  make explicit strength mandatory after every retained decision has been reviewed; until then,
  automation cannot weaken them.
- The boundary between a local implementation miss and a falsified plan can require deterministic
  escalation rules plus bounded model judgment; the initial implementation must fail closed when
  uncertain.
- Some first-contact units will still be model-expensive. The mechanism reduces duplicate planning;
  it does not make complex construction free.
- Qualitative failures may require several build/repair cycles. Cost budgets and checkpoint reuse
  remain necessary.
- Contract-bound spike integrity is useful but does not establish equivalence with a future
  cumulative scene.
- Plan-time coherence arithmetic over declared typed values — for example, a declared camera
  sample that places a declared subject plane behind the camera at a declared composition moment —
  is deferred, not rejected. It stays a candidate structural check only while it remains
  closed-form over typed records and constructs no proxy geometry; growing past that boundary
  would recreate the simulation pipeline this HIR removes.
- This HIR changes durable authority semantics. The required ADR-0004 amendment is drafted in the
  same changeset and is accepted or rejected together with this HIR.
