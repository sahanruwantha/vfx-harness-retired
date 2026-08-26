# VFX Harness agent guide

VFX Harness is an agent-driven Blender production runtime. Treat the repository as a
control system: model judgment proposes work, while typed contracts, deterministic evidence,
checkpoints, and replay decide whether work is accepted.

Making the agents smarter is a first-class goal alongside that control system. Smarter means
the model can do better work inside the control system: better instruments, better roles and
tool policy, better bounded context for the active unit, and better earned judgment. It does
not mean replacing evidence with model confidence, enlarging prompts or sessions as the way
complexity scales, or patching mechanical defects with prompt wording.

This file is the complete binding rule set, loaded by default for every agent (`CLAUDE.md` is a
symlink to it). Documents under `docs/` hold reasoning, history, and validation — read them for
why, never as the only copy of a rule. Record ids in parentheses point to the reasoning.

## Start here

- Product and commands: `README.md`
- Current architecture: `docs/architecture/`
- Durable decisions: `docs/decisions/`
- Failure-to-mechanism history: `docs/improvements/`
- Operational procedures: `docs/operations/`
- Provisional experiments: `docs/research/`

Read the relevant ADRs, HIRs, and tests before changing core harness behavior. Do not infer
current authority from old probes, generated shot output, or transcript history.

## Operating a shot

Exact commands live in `docs/operations/running-vfx-harness.md`. Use the public `.venv/bin/vfx`
CLI from the repository root; do not improvise internal module commands when the CLI owns the
operation.

- Run `vfx preflight --strict` before spending model budget. Authentication, Blender, or
  configuration failures can resemble an empty successful agent session; never diagnose them as
  a VFX-quality problem.
- The normal operation is `vfx run`. It stops on the first unaccepted boundary; do not force
  downstream work past it. `--force` is a bounded debugging experiment, never a deliverable.
- Reading order after any invocation: `runs/latest.json`, then the selected run's
  `manifest.json`, `status.json`, `reports/summary.json`, `artifacts.json`. Fail closed on an
  unsupported manifest schema. Open detail (`reports/layers/`, `plan_gate.json`, `evidence/`,
  transcripts, checkpoints) only when the summary names a reason. Never diagnose by recursively
  listing the shot or grepping every transcript, and never parse meaning from filenames.
- Shot-root legacy directories (`logs/`, `renders/`, `.artifacts/`, `.snapshots/`, `.versions/`)
  are unsupported: no evidence authority, no write destination (ADR-0002).
- Diagnosis routes by cause: preflight/config failure → fix the environment, not VFX logic;
  plan-gate failure → repair plan/contracts and rerun the gate; builder evidence failure → the
  owning layer report and its cited evidence; `hypothesis_falsified` → publish amended authority
  and consume the typed finding with `vfx units replan`; canonical replay failure → the
  deterministic script/checkpoint mechanism; acceptance failure → the declared fault-owning
  layer; interruption → last checkpoint, journal, and final transcript events.
- Resume a truncated builder session only when its ledger resume record names an existing
  checkpoint and journal; otherwise start a new run from the fault-owning layer. Never copy an
  old render, snapshot, or script into a run and call it a resume.
- Authority editing: `brief.md` and `refs/` change authored intent only; plans and contracts
  change only through planning, amendment, or an explicit reviewed repair; `build/` and
  `shot.json` are the accepted deterministic chain and ledger; `state/` is durable cross-run
  state; `runs/` is generated audit evidence — never hand-edit a run to make it pass.
- Run output never becomes authority by proximity. Promotion from evidence into a contract,
  plan, HIR, or ADR is an explicit decision (ADR-0002).
- Generated writes go through `observability/run_artifacts.py`: JSON published atomically,
  JSONL only for event streams, resumable accepted state in `checkpoints/`, disposables in
  `scratch/`, cross-run state under `state/` — never under a prior run.

## Pipeline north star

Before changing core runtime behavior, read `docs/architecture/pipeline-end-goal.md` and
`docs/architecture/staged-pipeline.md`.

Complexity must scale by adding bounded, dependency-ordered work units, not by enlarging prompts
or agent sessions. Every unit must be checkpointed, locally validated, repairable, and proven
through cumulative empty-scene replay. Runtime context, cost, and uncertainty must scale with
the active work unit, not with the total scene, plan length, or accumulated run history.

## Repository boundaries

- `src/vfx_harness/domain/`: pure contracts and state; no Claude SDK, Blender, network, or filesystem adapters.
- `src/vfx_harness/application/`: user-facing use cases.
- `src/vfx_harness/orchestration/`: scheduling, repair, rollback, checkpoints, and revalidation.
- `src/vfx_harness/agents/`: model roles, prompts, context, and tool policy.
- `src/vfx_harness/blender/`: the only direct Blender process/API boundary.
- `src/vfx_harness/evidence/`: deterministic and qualitative decision evidence.
- `src/vfx_harness/observability/`: run IDs, logs, transcripts, cost, and provenance.
- `src/vfx_harness/infrastructure/`: configuration, sandbox, and external runtime adapters.
- `src/vfx_harness/knowledge/`: packaged VFX recipes and their verification.
- `evals/`: tracked suites and fixtures; `artifacts/`: generated evidence; `shots/`: local production work.

Dependencies point inward toward `domain`; runtime code must never import offline evaluation
fixtures or generated artifacts. Preserve user changes in a dirty worktree. Generated data
belongs under `shots/` or `artifacts/` and must not be imported as source.

## Non-negotiable invariants

- Nothing self-certifies. A model verdict cannot replace authoritative executable evidence.
- Parallelize evidence production; serialize authoritative scene mutation and integration.
- Empty-scene replay is the source of truth for a published build artifact.
- Builders and repairs may mutate only declared semantic roles, controls, and script spans.
- Fail closed on stale, ambiguous, incomplete, or schema-incompatible authority.
- Core code must not contain shot names, display-name selectors, fixed shot frames, fixed layer
  or unit counts, scene-specific coordinates, keyword inference for moments or evidence modes,
  or department assumptions presented as universal.
- A current shot is a fixture, not a template. Generalization requires heterogeneous held-out
  fixtures and injected failures; no mechanism is general because it worked on the development
  shot.
- Strict migration, no silent compatibility: obsolete schemas and artifacts are migrated or
  rejected, never interpreted heuristically. Compatibility windows declare a deterministic
  expiry and fail closed after it (ADR-0004).

## Decision quality: smarter agents through instruments, not guesses

Agent capability is a harness product. Improve it. An agent forced to guess, rediscover
authority, or act without seeing the effect of its last mutation is a harness defect, not a
model limitation to paper over with a longer prompt.

Wherever model judgment must decide, the harness supplies ground truth first: typed
introspection, deterministic measurement, enumerated options, and read-back of every mutation's
effect.

- Query, don't recall: agents act on authoritative state read through tools, never on memory of
  the scene, the plan, or a prior run.
- Measure, don't estimate: if a decision depends on a quantity, expose an instrument that
  measures it; a judgment call where a measurement is possible is a patch.
- Enumerate, don't imagine: where the option space is knowable — roles, controls, targets,
  frames — present validated choices instead of free-form generation.
- Close the loop: every mutating tool has a matching observation, so an agent sees what its
  action actually did before its next decision.
- Abstention is always legal: every decision point accepts "insufficient evidence", fails closed,
  and escalates. A forced pick among unsupported options is a harness defect.
- Rejections teach: tool failures and validation rejections name the violated contract, expected
  versus found, and the legal next actions. A selector miss reports both sides — what was
  requested and what actually exists (HIR-0018).
- A recurring guess is a missing tool. Build the instrument at the owning boundary and grant it
  to the roles that need it; do not tune prompts to guess better.

Wanted capability work: new instruments, compiled unit context, tighter tool policy, closed-loop
mutation, and earned qualitative judgment where executable evidence cannot decide. Not wanted:
larger prompts or longer sessions as the scaling strategy, prompt-only patches for mechanical
defects, a confident model verdict replacing executable evidence, or extra mutation authority so
a builder can "figure it out".

The same standard binds coding agents on this repository: resolve unknowns by reading authority,
running code, or adding a probe — never by assumption.

## Fix policy: permanent mechanisms only

Temporary fixes are forbidden. A change is a fix only when it removes the cause; anything that
suppresses, defers, or narrows the symptom is a patch and must not land, even "for now".

- Prefer making the failure unrepresentable: types, contracts, ownership, a single source of
  truth. Where prevention is impossible, fail closed at the boundary; never detect-and-continue.
- Workarounds, stopgap guards, special-case branches, silent retries, broadened tolerances, and
  prompt wording are not fixes for mechanical defects.
- If the root cause is an architectural bottleneck, change the architecture through an ADR and
  migrate. Recurrence always costs more than the structural fix; the short path is the expensive
  path.
- A fix shaped around the current shot, fixture, or data layout is a patch. Prove generality on
  heterogeneous fixtures before acceptance.
- Diagnostic scaffolding may be temporary; shipped behavior may not. Remove scaffolding in the
  same change that lands the mechanism.
- If the permanent fix exceeds the task's scope or authority, stop and escalate with evidence.
  An open, recorded defect is acceptable; a landed stopgap is not.

## Planning and authority

- Plan authority is a singular immutable bundle selected by one atomic pointer
  (`plans/current.json`). Readers resolve everything through the pointer, verify member hashes,
  and fail closed on malformed, incomplete, or stale selected authority — there is no fallback
  read. Never copy bundle contents onto shot-root files by hand (ADR-0004).
- `clean` means structurally executable — requirements, DAG, owners, scopes, contracts, decision
  strengths, and due boundaries close. It never claims that future scene-dependent targets
  already pass; the earliest producing unit evaluates them in the real cumulative scene
  (ADR-0004).
- Global publication contains only: the dependency-ordered layer DAG, an ownership-only
  requirements register, durable shot-wide constraints, reserved interfaces, and genuine
  blockers. Every layer materializes just in time; a dependency-root layer may materialize
  immediately after publication. Concrete evidence design — contract kinds, moments, thresholds,
  calibration, research, reference fingerprints — belongs to the owning layer's materialization,
  which fails closed until every owned requirement resolves (ADR-0005, ADR-0006). Materialization
  validation reports every collectable finding in one write, each addressed by an RFC 6901 JSON
  pointer; field repair is `patch_materialization` on the candidate file. Unreadable JSON, wrong
  schema, wrong bundle hash, and a non-object layer remain fatal. Historical plan bundles are
  not a repair instrument (HIR-0023).
- A decision is made globally only if it is needed before the first unit, alters the DAG, is
  irreversible, or is expensive to be wrong about later; otherwise defer it to the owning layer
  (ADR-0005).
- Global planning reads only `brief.md` and `refs/` inside an isolated run workspace. Prior
  plans, contracts, builds, and runs are not implicit planning input; planner writes outside the
  workspace are denied.
- Scene-dependent values declare a strength — `hard_constraint`, `approved_start`,
  `planner_start`, `confirmed_outcome`. Only accepted executable evidence pinned to a checkpoint
  creates a confirmed outcome; records without a strength read as hard constraints (ADR-0004).
- Unit plans publish only through the gate-attested two-phase transaction (HIR-0016). Do not
  hand-author placeholder units, edit `state/jit-layers/current.json`, or reinitialize,
  hand-edit, or delete durable work-unit state to make a new DAG fit.
- When passing requires a decision, dependency, ownership, scope, contract, or sealed-outcome
  change outside the active unit, record `hypothesis_falsified` and stop. Replanning is a
  versioned transaction: freeze accepted state, validate the amendment, compute the complete
  invalidation closure, preserve unaffected checkpoints, mark replaced outcomes `superseded` and
  terminally unsatisfied dependants `blocked`, publish atomically, resume at the earliest legal
  unit. Builders and repairs never rewrite plans or broaden their own scope.
- Reopen a fixed or interrupted unit only through the audited `vfx units retry` transition, with
  reason and evidence. A reopened unit whose executable rows already pass may mutate until the
  first in-session verdict — the convergence guard cannot treat a failed qualitative claim as
  sealed work (HIR-0021). Promote a retained clean candidate through `vfx plan --promote-run`,
  never by editing an immutable bundle or re-paying to author the same plan.
- `blocked` and `superseded` are lifecycle states, not scene or builder failures, and never
  permission to compensate later.
- When the evidence vocabulary cannot express a requirement, record a typed vocabulary-gap
  escalation and close the requirement with an explicit decision referencing it. Padding —
  vacuous bounds, self-certifying properties, invented evidence — is forbidden and rejected at
  validation (HIR-0017).

## Evidence, claims, and judgment

- Decide with the evidence hierarchy in order: deterministic scene/interface facts; executable
  image contracts; isolated render passes and focused optical evidence; atomic qualitative
  judgment; interaction judgment; human adjudication only for genuine uncertainty.
- Metric identity is explicit: one canonical registry (`vfx-harness.look-vector/v1`); producers
  and consumers call the same implementation; unknown metric ids are rejected. Never maintain
  parallel implementations of one metric (ADR-0003, HIR-0006).
- Every metric kind declares the evidence domain it can certify (scene, temporal,
  projected_composition, image); a claim binds only evidence that can certify its domain. Counts
  prove existence — never timing, ordering, or appearance (HIR-0014).
- No implicit frames or moments: frame-sensitive contracts declare `frame`; plans declare the
  primary moment explicitly; motion claims bind exact temporal contract ids; evidence modes are
  declared, never inferred (HIR-0015, ADR-0003).
- Projection-only rows prove screen placement, not visibility. Judge frames require
  `visible_fraction` coverage; nothing-on-screen reads 0.0 and is a failing measurement, not an
  instrument error (HIR-0019).
- Absence fails closed: a required contract never evaluated blocks sealing; unknown keys are
  rejected naming the accepted set; silence is never consent (HIR-0014). An empty obstacle
  selection is not path clearance — the 1e9 sentinel never PASSes (HIR-0024).
- Claims are atomic propositions defined by the planner; the judge never expands its own scope.
  Required claims combine with logical AND — passing claims cannot average away a failure.
  Batching is transport, never aggregation; failed, borderline, disputed, or repaired claims are
  rejudged alone with the narrowest sufficient evidence.
- Blocking authority is earned. Executable contracts and qualified qualitative claims may block
  autonomously; an unqualified qualitative claim becomes executable evidence, is requalified, or
  routes to audited human adjudication — never silently optional. Changing the judge model,
  prompt, evidence layout, or claim semantics invalidates qualification.
- Critics describe qualitative residuals and cite evidence. They never override a passing
  authoritative measurement of the same fact and never prescribe unverified implementations. No
  repair is justified by unsupported measurement prose.
- Executable-only units call no visual critic; evidence is filtered to the active unit's exact
  bindings; sibling and future contracts cannot judge a unit or authorize a repair.
- Interaction claims declare a coordination owner and the exact shared controls it may balance;
  atomic claims stay protected, and anything broader enters transactional replanning.
- Warm-scene success proves nothing durable. The deterministic script and its empty-scene replay
  are the artifacts of record.

## Mutation, repair, and recovery

- One authoritative scene writer. Freeze the script, settings, dependencies, and candidate hash
  before any parallel evaluation; every worker proves it used the expected hashes.
- Mutate only declared roles, controls, and script spans. Semantic tagging uses the universal
  `bvfx_role`/`bvfx_control` properties on any host type; never invent type-specific variants.
  A role is one dotted token (`[A-Za-z0-9._-]+`); commas are not membership — tag once or split
  hosts (HIR-0022). Builder scene tools (`inspect_scene`, `check_scene`, `list_keyframes`)
  address `role`; a miss names the requested selector and the names and roles that exist
  (HIR-0018). An unknown authored change blocks candidate freeze until it is classified,
  reverted, or added through a plan amendment.
- Protection wildcards resolve to an explicit sorted contract-id closure at freeze; evaluation,
  repair, resume, and revalidation use that recorded closure, never a re-evaluated wildcard.
- Appearance on another layer's geometry is owner-granted authority: the owner declares
  `dressable` selectors, the dresser declares `dresses`, validation closes over both, and
  dressing is material assignment only — moving, deleting, or remeshing a dressed object breaks
  the owner's sealed contracts (ADR-0007).
- Repair starts from the last accepted checkpoint with a machine-authored manifest: one bounded
  semantic edit per attempt, then re-evaluate failing AND protected evidence; accept only
  monotonic progress without regression, otherwise restore the snapshot. Truncation,
  cancellation, SDK failure, or worker failure is a rollback path, never permission to retain an
  unvalidated edit.
- Faults route to their semantic owner. A downstream layer never compensates for a broken
  upstream interface, geometry, material, animation, or other sealed responsibility. A repair
  that cannot express the fix inside its authorized scope stops with a typed plan defect; it
  does not broaden its permissions.
- Conversation summaries and transcripts are never execution authority. Durable memory is
  checkpoints, manifests, and sealed outcomes on disk.

## Generated imagery

- Generate freely where many answers satisfy the requirement and the output is cheaply
  measurable. Where exactly one answer is acceptable, generate only if the difference is
  measurable — and gate on that measurement. Where it is not measurable, do not generate.
- Every generated artifact needs a named check and a named owner of its failure case; an ungated
  generated artifact is confident fiction.
- Gate strictness scales with blast radius: errors that propagate through stacked layers
  (camera, root-layer framing) get the strictest gates exactly where generation is most
  tempting.
- Verify the quantity the layer owns: derive targets from the subject region and from the same
  reference the layer is judged against — a frame-band statistic is invalid for a subject that
  does not fill the band.

## Change protocol

Follow `docs/operations/improvement-lifecycle.md` when a pain point, failure, or improvement is
found. In short: capture evidence, reproduce, classify the cause, choose the owning record,
implement the smallest general mechanism, prove the failure now passes without regressions, and
record the outcome. Do not fix a harness defect with prompt wording alone when it can be enforced.

- Use an HIR for a failure-to-mechanism improvement and its validation.
- Use an ADR when the choice changes durable architecture, authority, contracts, or boundaries.
- Use a research note while the cause or mechanism remains uncertain.
- Use the changelog only for accepted, user-visible outcomes; link the HIR/ADR for reasoning.
- If evidence is missing, authority conflicts, or scope must widen, stop and escalate rather than
  silently guessing.

Rules live in this file. A change that creates, amends, or retires a durable rule updates this
file in the same change; ADRs and HIRs carry reasoning and validation, never the only copy of a
rule. If this file and a record disagree, stop and reconcile — do not silently pick one.

## Verification

Run from the repository root:

```bash
.venv/bin/ruff check src tests
.venv/bin/python -m pytest -q
.venv/bin/vfx --help
```

Report which checks ran and any checks that could not run.

A defect fix is complete only when the failure is pinned as a tracked test or eval fixture that
fails without the mechanism and passes with it. Suites are a ratchet: fixtures and assertions are
removed or loosened only by a recorded decision, never to make a run pass. Report results
verbatim; partial success is partial, not done.

Judge a fix only on a path that provably executed it. A resumed run that can reuse products
sealed before the fix is evidence of nothing: re-run the producing step — or the pipeline from
the start — before reading any outcome as a verdict on the fix, and say which one you did.
