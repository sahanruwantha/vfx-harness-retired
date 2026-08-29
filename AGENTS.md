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
  unsupported manifest schema. `status.json` `detail` is the stop meaning, never the exit-code
  digit (HIR-0037). Materialization writes `logs/transcripts/plan/materialize-layer-*.jsonl`
  (HIR-0038). Open detail (`reports/layers/`, `plan_gate.json`, `evidence/`,
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
  A failed-artifact warm start is likewise legal only when the ledger pins that artifact to the
  exact current `WorkUnit` digest; same-id superseded and legacy unpinned scripts replay from clean
  priors instead (HIR-0059).
- Authority editing: `brief.md` and `refs/` change authored intent only; plans and contracts
  change only through planning, amendment, or an explicit reviewed repair; `build/` and
  `shot.json` are the accepted deterministic chain and ledger; `state/` is durable cross-run
  state; `runs/` is generated audit evidence — never hand-edit a run to make it pass.
- Run output never becomes authority by proximity. Promotion from evidence into a contract,
  plan, HIR, or ADR is an explicit decision (ADR-0002).
- Builder image payments use `vfx-harness.image-payment/v2`: the harness captures the
  pre-unit adversary from the exact prior-script chain, render tools issue immutable
  current-run handles, and `propose_checks` accepts a handle rather than a path. Candidate
  and adversary live under one structured run's `evidence/renders/` with matching
  frame/settings and verified SHA-256, unit digest, and parent-chain digest. Legacy rows
  and shot-root renders pay no debt (ADR-0008, HIR-0053).
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
- Camera availability comes only from typed `provides: ["camera"]` authority; role names,
  including `camera.target`, never imply a capability (HIR-0098).
- `inspect_scene(render/lights)` exposes the world/compositor identity, EEVEE volumetric and
  view-layer pass state, and light shape/distance settings; do not smuggle those reads through an
  idempotent `run_bpy` assignment (HIR-0055).
- Node-graph introspection enumerates both input values and output socket names; an unlinked
  producer must not force a read-only mutation probe or Blender-version guess (HIR-0061).
- Framing, bbox, and visibility checks measure rendered subjects, not Light/Camera/Empty-style
  control hosts; use per-light render isolation for illumination contribution (HIR-0055).
- Camera alignment to an Empty/control host uses `projected_origin_x/y`; its repair owner
  provides camera. The control producer proves fixed world state with scene evidence, and
  the camera successor depends on it and owns projection. Bbox and `visible_fraction` are
  surface evidence and a unit mutating their roles must provide geometry or dress an existing
  rendered surface. Executable-only camera/control units with no rendered subject owe no
  invented visibility proxy (HIR-0090, HIR-0094).
- Measure, don't estimate: if a decision depends on a quantity, expose an instrument that
  measures it; a judgment call where a measurement is possible is a patch.
- Enumerate, don't imagine: where the option space is knowable — roles, controls, targets,
  frames — present validated choices instead of free-form generation.
- Close the loop: every mutating tool has a matching observation, so an agent sees what its
  action actually did before its next decision.
- Animation read and mutation instruments traverse the same host closure: object, object
  data-block, or every object resolved by an exact semantic role. A curve reported as
  `data.P` by `list_keyframes` must be reachable by `bvfx_interp` without Python-host
  rediscovery (HIR-0074).
- Abstention is always legal: every decision point accepts "insufficient evidence", fails closed,
  and escalates. A forced pick among unsupported options is a harness defect.
- Rejections teach: tool failures and validation rejections name the violated contract, expected
  versus found, and the legal next actions. A selector miss reports both sides — what was
  requested and what actually exists (HIR-0018).
- A recurring guess is a missing tool. Build the instrument at the owning boundary and grant it
  to the roles that need it; do not tune prompts to guess better.

Wanted capability work: new instruments, compiled unit context, tighter tool policy, closed-loop
mutation, in-scope recipe retrieval that can abstain, repair abstention that stops the budget,
named argmax on scalar bound evidence, look-less live Workbench preview, look-owning repair
probe that includes draft beauty, shared-role tools that name object= or enumerate hosts,
bound materialization transcripts, teaching status details, look-less composed canonical that
fans in unit executable claims, repair `cannot_express_in_scope` bound on the candidate
session, look-without-image-contracts that does not lock `run_bpy` on a 0/0 handoff,
unit judge frames covered by required claims rather than a critic fall-through,
look-owning units that cannot seal on scene counts, claim-closure that counts
look image-contract ids as build-time debts, a compiled image-debt card that
freeze refuses while unpaid without typed abstention, a keyframe_schedule path
miss that names object and data-block fcurve paths instead of INAPPLICABLE, a
required `visible_fraction` claim repaired only by a camera or role-mutator,
per-role vis AND so a union cannot hide a subject, geometry units that
freeze-protect active-layer vis, a plan gate that rejects geometry units whose protected
same-layer vis producer is outside their dependency closure, a derived write-cluster
gate that names mixed mutation families, typed digest-bound successor publish
interfaces, camera-dependent evidence that cannot publish before a camera provider,
role-bound global camera capability whose dependency closure reaches every judged layer,
typed camera availability that role names cannot impersonate, a closed unit-ticket schema
on the staging tool, projection consumption that matches the exporting interface, locked
revision-checked materialization candidate writes, and
earned qualitative judgment where executable evidence cannot decide. Not wanted: larger prompts or longer sessions as the scaling strategy,
prompt-only patches for mechanical defects, a confident model verdict replacing executable
evidence, or extra mutation authority so a builder can "figure it out".

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
  not a repair instrument (HIR-0023). Rematerialization writes a reverted overlay as the design
  base and selects only when the replacement publishes; crash, truncation, or a broken pipe
  leaves the previously selected view (HIR-0026). After global republication, a live JIT view
  pinned to the prior bundle is superseded state: rematerialization derives its unpublished
  design base from the currently selected sparse bundle and never carries prior-generation
  rows forward by proximity (HIR-0101). Rematerialization of a layer that already
  has accepted units is `apply_replan`: matching digests stay, including `passed`;
  changed or downstream-invalidated units are superseded even if they had passed.
  `--discard-accepted` retires accepted orphans and wipes state only when the replan
  base is unusable — it is not the door on remat (HIR-0052). Task and Agent are not remat repair
  instruments. An exhausted materialization session does not publish: max-turns is a failed
  transaction, not a select (HIR-0027). Structured decision adoption is last-write-wins for
  the selected bundle only: a `values.contract` row keyed to another generation is inert, a
  later `superseded` or `falsified` row for the same id on this bundle retires it, and
  materialization copies the compiled binding set rather than every ledger line (HIR-0028).
  Unit judge frames, claim moments, and `composition_context.frames` are a subset of the
  layer judge list compiled into the materialization kickoff. Every unit judge frame
  must appear in a required claim's `moments`; an uncovered frame is a contract_gap,
  not a critic look vote (HIR-0045).   A look-owning unit must cover every judge
  frame with a required image-domain claim; scene counts cannot seal appearance
  (HIR-0046). Claim-closure counts those bound `image_contract` ids as
  producers even while `checks.json` is empty; missing image rows are
  build-time debts, not `does not exist` (HIR-0047). Those ids compile to a
  payment card (id, frame, property, axis); `propose_checks` must match all
  four fields and binds each multi-frame batch row to its own immutable candidate
  handle (the batch handle is only a same-frame shorthand); candidate freeze refuses while any remain unpaid without a
  typed `unpaid_image_debt` abstention; ids are bare, never `check:`;
  a `frame_delta` image debt is paid by a `frame_*` scalar that passes only on
  the candidate and fails the harness-captured pre-unit adversary; rejection
  enumerates the registered metrics that can certify the owed property rather
  than making the builder guess;
  readers of falsification `contract_ids` strip that prefix
  (HIR-0048). Scene contracts may measure other frames; bind
  those ids through `composition_context.contract_ids` without adding the extra
  frames to the judge lists. Claim-closure counts those ids as bound producers.
  A dependency root has no sealed outcomes to directory-Read (HIR-0029).
  A `keyframe_schedule` whose consecutive samples already exceed a same-role
  `curve_derivative_max.hi` is refused at materialization, authoring, and the
  plan gate — interpolation cannot invent a third option (HIR-0030).
  A required claim that binds `visible_fraction` is repaired by a unit that
  `provides: ["camera"]` or mutates/dresses every `roles` selector on that row;
  a volume-only unit cannot bind mesh vis as required repair. Multi-role vis is
  logical AND across named roles. A unit that `provides: ["geometry"]`
  freeze-protects lifecycle-active vis on this layer, including sibling-owned
  rows (HIR-0051). If a protected vis role is produced by another same-layer unit,
  that producer must be in the geometry unit's transitive dependency closure; a future
  producer is an unsealable DAG and publication fails closed (HIR-0057).
  Every scene-contract kind that projects or renders through the active camera must bind
  on a unit whose dependency closure (or an earlier materialized layer) provides a camera;
  otherwise publication fails `composition-bootstrap` (HIR-0085).
  Camera availability is global DAG authority: each sparse layer declares `jit.provides`
  as capability → reserved-role selectors, every judged layer's transitive closure must
  contain camera, and a materialized camera unit must mutate one of that layer's exact
  reserved camera-interface roles (HIR-0086, ADR-0005).
  A work unit publishes one derived write-cluster (role-namespace × host class ×
  instrument family); authored family strings are padding and do not satisfy the
  gate. Instrument family comes from typed mutation targets and write-kind evidence;
  object-level transforms are control state, and a zero-animation bound is observation,
  not keyframe authority (HIR-0095). Unresolved families fail closed. Dressing, vis
  observation/protection, and bounded
  coordination are typed exceptions. Consumed interfaces are read-only inputs — they
  do not grant mutation of producer export roles and cannot hide a mixed cluster.
  Required claims share one repair_owner (HIR-0083). Each unit publishes typed
  successor interfaces whose export values are roles, controls, or sealed contract
  ids. Authored `publishes` and `consumes` participate in `unit_digest`; a successor
  that declares `consumes` is ready only when that exact producer interface id/kind is
  digest-matched. A dependency with no `consumes` is a legal status-only edge and grants
  no interface. The live builder card carries only the exact consumed interfaces from
  direct predecessors, with producer digests (HIR-0084). A camera-owned
  `projected_origin_x/y` row may observe a same-layer target only through an exact
  consumed producer interface that itself exports the measured selector; another
  interface from the same producer is not authority. The selector is read-only and
  never enters camera mutation authority (HIR-0096, HIR-0099).
  The incremental unit staging tool exposes the closed WorkUnit authoring schema:
  temporal enums, optional composition-context union, and exact typed publish/consume
  fields are enumerated before generation rather than learned through parser retries
  (HIR-0097). Every staging or patch write uses one locked, revision-checked candidate
  transaction. Only `stage_materialization_unit` may add, replace, or reorder stage rows;
  field patches affecting units or their contracts run the same local staging gates before
  bytes change. Concurrent calls cannot overwrite a previously staged prefix (HIR-0100).
  Materialization binds `transcript` and `costlog` (`materialize-layer-{id}`);
  `log_message` journals only when bound (HIR-0038).
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
  hand-edit, or delete durable work-unit state to make a new DAG fit. A selected
  `layers.json` hash change that leaves this layer's unit IDs and digests unchanged
  is a preserve-all plan-identity adoption, not a DAG replan and not permission to
  empty-base-replan that layer (HIR-0040). Rematerialization of a layer that already has
  accepted units is the same `apply_replan`: matching digests stay; `--discard-accepted`
  is not the door on remat (HIR-0052). `vfx units replan --falsification`
  on a JIT layer compares `plan_hash` and unit digest to durable state and the
  selected view, not sha256 of the sparse bundle `layers.json`; consuming the
  finding reopens that unit and its affected closure even when the published
  DAG bytes are unchanged (HIR-0049).
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
  instrument error (HIR-0019). Multi-role `visible_fraction` is AND across named roles;
  a union scalar cannot hide a subject below `lo`. A required vis claim is repaired
  by a camera unit or the mutator of those roles, not a volume-only unit (HIR-0051).
- Absence fails closed: a required contract never evaluated blocks sealing; unknown keys are
  rejected naming the accepted set; silence is never consent (HIR-0014). An empty obstacle
  selection is not path clearance — the 1e9 sentinel never PASSes (HIR-0024).
  A `curve_derivative_max` miss names the argmax adjacent-frame pair and compact
  over-`hi` segments; a scalar without its argmax is an estimate (HIR-0035).
  A `keyframe_schedule` path miss names the requested aliases (`P`, `data.P`)
  and the fcurve data_paths on the object and its data-block; empty keys are a
  failing measurement, not INAPPLICABLE (HIR-0050).
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
  A declaring unit's `look_capabilities: []` is that executable-only authority — not a cue
  to scan axis identifiers for look groups. A candidate plate with no optical signal is
  not a look score: fail closed without calling the critic (HIR-0032). Composed
  canonical of a layer whose units all declare empty look capabilities fans in those
  units' executable claims; omitting `active_unit` is not permission to reopen a
  critic look vote on `layer.owns` (HIR-0039). `vfx build` exits 9 when units passed
  but the composed ledger verdict did not. A unit judge frame with no required claim
  is a contract_gap, not a critic look vote (HIR-0045). A look-owning unit
  cannot seal 5.0 on scene counts (HIR-0046). Claim-closure counts look
  `image_contract` ids as bound producers while `checks.json` is still empty
  (HIR-0047). A matching `runtime_checks.json` row is consumed as payment;
  unpaid debts are not scene-selector misses or critic handoff; readers of
  falsification `contract_ids` strip a `check:` prefix (HIR-0048). A runtime row counts
  only with a valid v2 payment envelope; the harness, never the model, selects its
  pre-unit adversary. Generated thresholds clear the measured replay margin, and
  candidate probing evaluates the same image rows before publication (HIR-0053). Live
  comparison gates close over only the bound ids due at that frame and report valid builder
  payments as bound checks, never as autonomous acceptance authority (HIR-0060).
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
  hosts (HIR-0022). `bvfx_control` adds an independent control tag and never overwrites an
  existing semantic role; an untagged node may receive role=control for node-role lookup
  compatibility (HIR-0063). Builder scene tools (`inspect_scene`, `check_scene`, `list_keyframes`)
  address `role`; a miss names the requested selector and the names and roles that exist
  (HIR-0018). A shared role on several hosts is not an inexact selector: `check_scene`
  names `object=` as the next action, and `list_keyframes` lists every host
  including data-block curves (`data.energy` on a Light) (HIR-0041, HIR-0050).
  Kickoff, `CLAUDE.md`, and the `unit_scope` tool share one compiled card for the
  active unit: mutation roles/controls/dresses/spans, bound contracts, claims, judge frames,
  the `run_bpy` helper inventory, including parameter and return contracts compiled from
  the worker source, authored publish interfaces, the producer digest, declared consumes,
  and exact consumed, digest-matched direct-predecessor publish interfaces. The kickoff keeps every evaluator field on the exact
  active-unit contract closure; unit boundedness never means hiding graph/socket/path selectors.
  Query that card; do not `inspect.getsource`, guess a helper result shape, or guess a
  sibling unit (HIR-0025, HIR-0079). A declared live unit may not reload the full brief or raw plan
  catalogs; relevant authority is the compiled card and embedded unit plan. JIT unit planning
  likewise has no raw `Read` surface: it receives the exact active-unit card plus compact passed
  direct-predecessor interfaces (exported roles, controls, capabilities, sealed ids, and
  only the typed digest-bound publish interfaces named by `consumes`) and bounded
  outcome/amendment/gap feedback, never dependency evaluator internals, selected catalogs, or scripts.
  Its plan publishes through a harness-bound content sink with no path argument; generic
  `Write` is not part of the JIT unit-planner surface (HIR-0091).
  Finalizer journals start after reset/dependency replay and end
  at the selected checkpoint. Layer materialization has no raw `Read` surface: kickoff compiles
  only the exact global layer row, owned requirements, active structured decisions, compact
  required upstream outcomes, and semantic/dressable dependency interfaces; full registers and
  outcome reports stay outside model context. Context scales with the active delta (HIR-0054).
  Its unpublished candidate is seeded deterministically and staged one bounded unit at a
  time; generic `Write` is denied, and only a fully validated candidate may satisfy the
  session postcondition (HIR-0092).
  Derived write-cluster atomicity is checked before each unit enters staged scratch; a
  mixed unit never becomes an accepted prefix that later repair must split (HIR-0093).
  Materialization kickoff compiles this layer's judge frames
  and extra-frame id-binding; two-sided `path_clearance_min` `roles` bind on the unit
  that mutates them (HIR-0029). Every unit judge frame must appear in a required
  claim's moments; an uncovered frame is a contract_gap, not a critic look vote
  (HIR-0045). A required claim that binds `visible_fraction` is repaired by a
  unit that `provides: ["camera"]` or mutates/dresses those roles; a volume-only
  unit cannot bind mesh vis as required repair (HIR-0051). A look-owning unit must bind image-domain evidence at every
  judge frame; scene counts cannot seal appearance (HIR-0046). Claim-closure
  counts those `image_contract` ids as debts, not missing bindings
  (HIR-0047). Freeze refuses while an owed look id has neither a coherent
  `propose_checks` row nor `unpaid_image_debt` abstention; canonical repair
  cannot author evaluation contracts (HIR-0048, HIR-0081). `find_recipe` ranks against the active unit's mutation
  roles and abstains naming the query and the roles present; fuzzy discovery returns compact
  ranked summaries, an exact recipe name returns a compact prose/code section index,
  `<name>#<section>` loads one fragment, and only explicit `<name>#full` loads the whole
  body. One session may load at most three distinct recipe bodies within a cumulative
  character budget; rereads and incremental full-recipe reconstruction fail closed so
  cookbook context cannot scale with turn count (HIR-0062, HIR-0067, HIR-0078). A lighting hit is not
  permission on a camera unit (HIR-0033). `run_bpy` errors that reinvent
  `path_clearance_min` or `BVHTree.FromMesh` name the bound instrument (HIR-0034).
  Live `render_frame` / `verify_change` default to Workbench `solid` when the
  unit has no look capabilities; canonical EEVEE remains the sealed artifact
  (HIR-0036). Repair `probe_candidate` on a look-owning unit also returns draft
  EEVEE `look_render`; solid is geometry, not the critic plate (HIR-0042). Live
  `run_bpy` stays open when a look-owning unit binds no image contract; 0/0
  image rows are not critic handoff (HIR-0044). An
  authored `run_bpy`/import call is a scene transaction: snapshot immediately before
  mutation, journal only on success, and restore before surfacing any exception. A failed
  call may retain no partial edit in the live scene (HIR-0068). Recurring Blender-5
  light conversion and Vector Blur construction use the typed `bvfx_light` and
  `bvfx_vector_blur` helpers; coupled API ordering is not free-form model work. Material
  inspection names object-slot consumers directly (HIR-0070). Volume helpers atomically
  tag their object/material/node/control hosts and return after dimensional readback is
  current; `size` is the requested full domain size. World-density mutation guards require
  graph provenance and must not classify a material-volume Density socket as World state
  merely because the socket label matches (HIR-0072). An
  unknown authored change blocks candidate freeze until it is classified, reverted,
  or added through a plan amendment.
- Read-only questions use read-only instruments: `contract_result` for an exact active
  bound row, `inspect_scene` for render/color/compositor/light state, `inspect_nodes` for
  sockets, and `render_pass(light=...)` for isolated contribution. Do not recreate those
  probes or write render/filesystem artifacts in `run_bpy`. `verify_change` requires a
  successful intervening mutation; a rejected edit is not a visible no-op (HIR-0055).
  Node inspection preserves meaningful small nonzero socket values in compact
  significant-digit form; it must never render a calibrated `1e-05` as `0.0` and
  induce a destructive rediscovery loop (HIR-0065).
  Render tools are read-only scene transactions: solid/wire/draft/EEVEE and diagnostic
  passes restore engine, frame, resolution, output path/format, Workbench shading, and
  sample count before the next authored call. A diagnostic render may not silently set
  the execution engine for a later mutation (HIR-0071).
  Local-light coverage under a World volume is measured with the transactional
  `render_pass(pass='light_coverage', light=...)` preset: it suppresses World surface and
  volume, overrides surfaces with clay, isolates the named light, transactionally normalizes
  and curve-mutes diagnostic energy, and restores all state. Coverage is geometry, not the
  unit's contract-scale wattage. Do not diagnose placement by mutating light visibility or
  unlinking atmosphere (HIR-0073).
  A nearly black EEVEE plate automatically carries a typed cause card for active
  World-volume density, background strength, camera span, canonical EEVEE volumetric
  start/end, semantic-subject distances, and light distance. Extinction uses the effective
  sampled volume span, not camera `clip_end`; subjects beyond `volumetric_end` are named as
  renderer depth coverage, never recast as a density target. When
  unbounded optical scale is high it names a logarithmic `probe_control` density
  sweep and blocks free-form mutation until that measurement runs; after a sweep,
  direct World-density commits are limited to measured values. A density ceiling is
  not a calibrated target. Completed role+frame measurements retire stale probe guards;
  density equality accounts for Blender float32 socket read-back rather than treating
  representation noise as a new unmeasured value;
  a sweep whose entire range remains black closes that causal branch and routes to a
  different measured variable instead of repeating. `probe_control` refuses a closed
  role+frame density sweep when every requested value is already in the measurement
  registry; replaying an experiment is not new evidence. After that closure, three distinct
  near-black local-light placements spanning at least a 4x camera-distance range close the
  placement branch; further render, probe, and mutation calls fail closed into typed
  `cannot_express_in_scope`, rather than spending the unit budget on more coordinates
  (HIR-0066, HIR-0069, HIR-0082).
  A live `cannot_express_in_scope` sets the remaining visual critique/revision budget to zero
  after the current response, fences every pixel-producing tool, then finalizes the accepted
  journal solely to publish the typed finding. It is not permission to launch another critic
  or repair session (HIR-0077).
  Model work is validated at every phase boundary, not only kickoff. A nominal SDK success
  with zero new tool calls and zero incremental cost is a transport/auth/spend failure and
  truncates before critique, finalization, replay, or repair; cumulative cost from an earlier
  response is not evidence that the current phase did work. Provider `is_error` and HTTP
  error status outrank a contradictory success subtype even after productive work; preserve
  those fields through collectors and never feed that terminal response into judgment
  (HIR-0080). Structured termination preserves the cause: provider/zero-work failures are
  `model_session_failure`, turn exhaustion is `max_turns_exhausted`, and the configured
  model-dollar ceiling is `model_budget_exhausted`; exit 3 is not sufficient diagnosis.
  Optional context-usage telemetry is bypassed after authoritative provider error facts;
  observability may not delay terminal propagation (HIR-0080).
  A scoped unit may not write free-form renderer policy through `run_bpy` (`eevee`, `cycles`,
  `render`, color/display settings). Use transactional render diagnostics; renderer-policy
  authority requires a typed plan control, and infeasibility under canonical settings routes
  to `cannot_express_in_scope` (HIR-0076).
  Candidate `probe_candidate` applies the same scoped new-object role validation as
  canonical verification before returning evidence; an undeclared helper/role is a probe
  failure, never a green repair read-back (HIR-0058).
- Protection wildcards resolve to an explicit sorted contract-id closure at freeze; evaluation,
  repair, resume, and revalidation use that recorded closure, never a re-evaluated wildcard.
  A unit that `provides: ["geometry"]` also freeze-protects lifecycle-active
  `visible_fraction` rows on this layer, including sibling-owned vis (HIR-0051).
  Publication rejects a geometry unit when one of those roles is produced only by a
  same-layer unit outside its dependency closure (HIR-0057).
  Publication also refuses a unit whose derived write-clusters are heterogeneous
  without a typed exception; the finding names the clusters. A successor consumes
  digest-matched publish interfaces from the compiled card, not producer scripts.
  Consumed exports are read-only; `depends_on` without `consumes` is a status-only edge,
  not interface compatibility, and grants no producer interface. Authored interface identity participates in the producer digest
  (HIR-0083, HIR-0084).
- Appearance on another layer's geometry is owner-granted authority: the owner declares
  `dressable` selectors, the dresser declares `dresses`, validation closes over both, and
  dressing is material assignment only — moving, deleting, or remeshing a dressed object breaks
  the owner's sealed contracts (ADR-0007).
- Repair starts from the last accepted checkpoint with a machine-authored manifest: one bounded
  semantic edit per attempt, then re-evaluate failing AND protected evidence; accept only
  monotonic progress without regression, otherwise restore the snapshot. Truncation,
  cancellation, SDK failure, or worker failure is a rollback path, never permission to retain an
  unvalidated edit. `cannot_express_in_scope` stops remaining repair attempts and records
  `hypothesis_falsified`; an unsatisfiable published schedule/smoothness pair does not
  consume the next attempt (HIR-0031). Canonical repair binds that tool on the candidate
  server — it is not a blender MCP ToolSearch (HIR-0043). A `keyframe_schedule` path
  miss names requested aliases vs present fcurve paths and stays in repair; it is
  not INAPPLICABLE and not `cannot_express_in_scope` by default (HIR-0050). Once active
  exact schedule rows pass, diagnostics may not mute their curves or override their paths
  without legal same-transaction rekeying; direct `keyframe_point.co` edits are the same
  protected mutation, including curves returned by `bvfx_fcurves`. Use a transactional observation; if normalized
  evidence proves the hard schedule cannot produce the required image signal, record
  `cannot_express_in_scope` instead. Numeric control sweeps always include the restored
  live value (HIR-0075).
- Faults route to their semantic owner. A downstream layer never compensates for a broken
  upstream interface, geometry, material, animation, or other sealed responsibility. A repair
  that cannot express the fix inside its authorized scope stops with a typed plan defect; it
  does not broaden its permissions. `cannot_express_in_scope` names any proven upstream owner only
  through the compiled `fault_owner_options`; the finding's invalidation closure starts from the
  active unit and those validated owner units. Accepted checkpoints remain authoritative until
  `vfx units replan --falsification` consumes that finding atomically. A unit that consumes an
  earlier unit's scene or pixels declares the producer in `depends_on`; serialization order is not
  dependency authority (HIR-0056).
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
