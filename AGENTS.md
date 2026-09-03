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
  a VFX-quality problem. Blender is selected only by a `--version` probe run inside the
  mandatory worker confinement; a launcher that works on the host but not in the sandbox
  (a snap shim that needs snapd) is rejected at resolution and strict preflight with the
  confinement's diagnostic, never discovered at worker boot. Point `BLENDER_BIN` at the real
  binary when the packaged launcher is unusable (HIR-0173). Strict preflight also proves the kernel-owned plan-consumer
  directory primitive (`plan_consumer_directory`), so an unsupported host fails before spend
  rather than at its first consumer view (HIR-0172).
- The normal operation is `vfx run`. It stops on the first unaccepted boundary; do not force
  downstream work past it. `--force` is a bounded debugging experiment, never a deliverable.
- Reading order after any invocation: `runs/latest.json`, then the selected run's
  `manifest.json`, `status.json`, `reports/summary.json`, `artifacts.json`. Fail closed on an
  unsupported manifest schema. Every run is the `vfx-harness.run/v2` generation owned by a claim
  and fence; `status.json` is `vfx-harness.run-status/v2` and holds only the selected record
  locators and digests, terminal diagnostics such as `terminal_cause` live in
  `reports/summary.json`, and prior-generation runs fail closed (HIR-0172). For an unaccepted terminal run, `status.json` selects
  `reports/stop-envelope.json` by exact digest; that closed envelope is machine dispatch
  authority. An interrupted v2 run selects `reports/interruption-receipt.json` and its
  satisfied evaluation by digest and authorizes no transaction. `status.json` `detail` and the exit-code digit are operator diagnostics only
  (HIR-0037, HIR-0164). Materialization writes
  `logs/transcripts/plan/materialize-layer-*.jsonl` (HIR-0038). Open detail
  (`reports/layers/`, `plan_gate.json`, `evidence/`,
  transcripts, checkpoints) only when the summary names a reason. Never diagnose by recursively
  listing the shot or grepping every transcript, and never parse meaning from filenames.
- Shot-root legacy directories (`logs/`, `renders/`, `.artifacts/`, `.snapshots/`, `.versions/`)
  are unsupported: no evidence authority, no write destination (ADR-0002).
- Diagnosis routes by cause: preflight/config failure → fix the environment, not VFX logic;
  plan-gate failure → repair plan/contracts and rerun the gate; builder evidence failure → the
  owning layer report and its cited evidence; `hypothesis_falsified` → stop, review the typed
  finding, and publish amended authority through its owning plan/materialization boundary;
  canonical replay failure → the deterministic script/checkpoint mechanism; acceptance failure
  → the declared fault-owning layer; interruption → last checkpoint, journal, and final
  transcript events. The authority publisher, not a follow-up state command, atomically derives
  and commits preservation/invalidation effects. `vfx units replan` is retired, and there is no
  public receipt-backed finding-consumption adapter.
  `EvidenceNotDue` is successful continuation to its DAG-compiled provider, never a stop or
  replan request. A stop envelope proposes exactly one typed transaction; it does not prove that
  transaction ran. `recover_environment` is the sole receipt-backed public adapter: after an
  operator repairs the environment, invoke it with the exact source run and idempotency key; its
  independent evaluator proves the commit. Identical typed stops from separate runs converge on
  semantic evidence identity, and an explicit retry reconciles exactly one already-written direct
  receipt orphan before probing again (HIR-0166). There is no automatic controller yet: ADR-0010
  (proposed) replaces this rule with `vfx run` dispatching only receipt-backed transactions,
  landing one transaction kind at a time after `room_1046_opening` closes. Until each kind lands,
  the other six transaction kinds remain non-dispatchable, and no current producer proves
  `local_implementation_miss`; never infer local retry authority from a generic builder failure
  or progress from a repeated finding (HIR-0164, HIR-0166).
- Do not resume a truncated builder merely because a ledger row names a checkpoint and journal.
  Safe resume requires a phase-specific immutable receipt binding the selected bundle/view,
  exact unit and plan digests, candidate, checkpoint, durable journal/WAL, model session, phase,
  and remaining budget. The current builder does not produce that complete receipt, so no
  automatic `resume_checkpointed_session` dispatch is authorized; start a new run from the
  fault-owning unit and current exact authority instead. Never copy an old render, snapshot, or
  script into a run and call it a resume (HIR-0164).
  A process death after a layer replay or critic call can leave an exact pre-terminal
  layer-finalization claim active. Restart must fail closed on that claim. Only after the
  builder fence proves no live owner may an operator invoke
  `vfx finalizations release <shot> --layer ... --claim-id ... --reason ... --evidence ...`.
  That reviewed transaction snapshots evidence bytes content-addressably, archives only the
  named unsealed claim, preserves every accepted unit receipt/checkpoint byte, records the
  complete existing ordered contiguous replay-receipt prefix, marks unsealed judgment output
  non-reusable, and permits a fresh higher finalization revision only while its v2 request,
  evidence, release receipt, and snapshots remain source-verifiable. A missing, duplicate,
  substituted, out-of-range, or count-inconsistent replay group refuses release. This is not unit
  invalidation, model-session resume, or automatic retry (HIR-0170).
  Every layer finalization publishes one `vfx-harness.layer-replay-receipt/v2` per actual
  evaluation group. Its typed observation binds the exact claim/replay prefix, group plan,
  required claims/evidence ids, reference bytes, actual deterministic evidence, and any
  group-specific `solid | eevee` render/capture plus hashed auxiliary captures. A typed replay
  execution failure carries only its closed stage/message/digest and cannot invent point,
  payment, raster, or critic evidence. `vfx-harness.layer-evaluation-receipt/v1` accepts only the
  ordered contiguous executed prefix from group zero, derives every group result, and can pass
  only after all planned groups execute. `vfx-harness.layer-finalization-receipt/v2` derives its
  status and projections from that evaluation. Terminal and layer-outcome readers reopen the
  external evaluation, every group receipt, and every replay-input/dependency,
  reference/render/auxiliary, script, predecessor, and sealed outcome source; embedded receipt
  content never self-certifies current publication (HIR-0170).
  A new ledger attempt starts from a bare in-progress row: `Ledger.begin` moves the previous
  attempt's receipt digest and script hashes into its history entry, so re-finalizing
  a passed layer after rematerialization cannot trip the claim scope check (HIR-0179).
  A failed-artifact warm start is likewise legal only when the ledger pins that artifact to the
  exact current `WorkUnit` digest; same-id superseded and legacy unpinned scripts replay from clean
  priors instead (HIR-0059).
- Durable builder worklists are scoped by layer id, unit id, and exact unit digest.
  Unresolved items survive retries of that generation only; layer-only legacy lists and
  sibling or superseded unit lists are inert and cannot block the active unit. Executable-only
  composed canonical fans in the exact digest-bound worklist of every constituent unit; the
  synthetic composition judge never invents its own worklist identity (HIR-0107, HIR-0109).
- Authority editing: `brief.md` and `refs/` change authored intent only; plans and contracts
  change only through planning, amendment, or an explicit reviewed repair; `build/` and
  `shot.json` are the accepted deterministic chain and ledger; `state/` is durable cross-run
  state; `runs/` is generated audit evidence — never hand-edit a run to make it pass.
- The live canonical `<shot>/shot.json` may be written only through the typed shot-ledger
  publication transport. Generic prepared/durable writers reserve every `shot.json` and
  `shot.json.lock` target, and scratch/view projection adapters must prove their destination is
  physically distinct from the live shot root. A writer fence or caller binding string proves
  physical serialization only; it is not strict `shot-ledger/v2`, accepted-build closure, or
  interruption authority. Plan-consumer scratch roots and descendants are created only through the
  kernel-proven owned-directory primitive (fanotify target-FID plus `openat2`); an unsupported
  kernel or filesystem fails closed before any consumer write, and a descriptor whose identity
  cannot be read is retained and poisons the process rather than being assumed closed. The
  `accepted_build` member of `shot.json` is the strict `vfx-harness.shot-ledger/v2` index derived
  only by the shot-ledger derivation writer from selected authority, passed terminal receipts,
  sealed outcomes, composed script bytes, and the coordinator head. Its chain rows bind every
  durable terminal receipt whatever its status, the accepted prefix ends in front of the first
  non-passed receipt, and a selected authority without an evaluated coordinator head cannot
  publish the member. Plan and JIT republication republish it inside the authority-state
  transaction once the successor head is current and the WAL is removed, and
  `vfx recover-authority-state` republishes a member left stale by a death in that window.
  Callers never supply accepted rows, the transport refuses any other change to that member, and
  readers re-derive it rather than trust the stored value (HIR-0172).
  An interruption receipt binds a run-owned content-addressed archive of the exact authority
  sources and transcripts captured under the shared shot-authority fence, and the independent
  evaluator derives `satisfied | failed` only by reopening that archive through the one domain
  source classification, never the live shot tree. That archive binds authored inputs too: the
  exact `brief.md`, every admissible `refs/` still, and the registration plus crop of every
  `refobs-*` witness a selected unit names; a symlinked or non-regular reference entry and a
  missing witness pair refuse capture. A run without a readable receipt has no
  evaluation. The direct-command boundary and the whole-shot driver own every public run: they
  acquire the run-owner fence before publishing `running`, record the first SIGINT/SIGTERM as
  a typed `RecordedSignalIntent` that only the signal handler mints (the terminalizer accepts no
  kind string) and cancel by raising, and select exactly one terminal status; a stage inherited inside
  a driver publishes only its typed stop envelope, an exception no stage classified is
  terminalized `failed` by the root owner rather than left `running`, and a cancellation with no
  recorded intent is a failure, never an interruption. A run left `running` by a dead owner is reconciled only through
  `vfx reconcile <shot> --run-id <run>`, which proves loss by acquiring the exact recorded fence,
  never from PID absence, status age, or transcript silence; a held fence changes nothing. Only the terminalizer, holding the live root-owner fence, selects `interrupted`:
  it captures, publishes the receipt, lets the evaluator reopen the archive, then publishes the
  evaluation, summary, inventory, terminal status, and latest projection exactly once, replacing
  the exact `running` bytes it observed; an unsatisfied evaluation leaves the run running with
  interruption authority unavailable. The authoritative interrupted reader re-evaluates the
  archive and derives zero legal transactions and no retry, resume, or dispatch authority
  (HIR-0172).
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
  `scratch/`, cross-run state under shot-root `state/` — never under a prior run. Read-only
  cross-run state access, including `layer_state`, resolves through `shot_state_dir` and must
  not call a run-producing helper or change `runs/latest.json` (HIR-0155).
- A normal full render is a deliverable only when `shot.json` contains a complete passing
  `vfx-harness.acceptance-outcome/v1` for the exact current selected bundle, materialized view,
  accepted script chain, complete moment set, and unchanged acceptance evidence bytes. Missing,
  failed, partial, or stale acceptance refuses publication. `--force` and `--upto` are previews;
  their default destination is the current run's `scratch/previews/`, and an explicit output path
  does not promote them to acceptance authority (HIR-0165).

## Pipeline north star

Before changing core runtime behavior, read `docs/architecture/pipeline-end-goal.md` and
`docs/architecture/staged-pipeline.md`.

Complexity must scale by adding bounded, dependency-ordered work units, not by enlarging prompts
or agent sessions. Every unit must be checkpointed, locally validated, repairable, and proven
through cumulative empty-scene replay. Runtime context, cost, and uncertainty must scale with
the active work unit, not with the total scene, plan length, or accumulated run history.
The authored `stages[]` array is not execution order: replay accepted priors and compose layer
artifacts in the stable topological order derived from exact `depends_on` edges, with authored
position only as the tie-break among independent ready units (HIR-0119).

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

## Code hygiene

Leave code cleaner at the point of change; do not create cleanup debt for a later pass.

- Imports belong at module scope. Never import inside a loop. A function-local import is legal
  only for an unavailable embedded-runtime dependency (`bpy`, `bmesh`, `mathutils`), optional
  version compatibility, a deliberate package-facade lookup used by tests, or a proven circular
  dependency that cannot be removed in the same change. Mark every production exception with
  `# noqa: PLC0415` and a comment naming the reason. Do not suppress `PLC0415` for an entire
  ordinary module.
- Remove cycles structurally: extract shared contracts and parsers into dependency-free leaf
  modules. Do not hide a cycle with repeated local imports, `importlib`, duplicated helpers, or
  package `__init__` side effects. Import concrete leaf modules instead of large public facades
  inside core domain code.
- Use module-qualified access for collaborators that tests or runtime configuration replace
  (`module.function(...)`, not a copied `from module import function` binding). Direct symbol
  imports are appropriate only for stable values and non-replaceable pure helpers.
- Keep one responsibility per module. New production modules stay below 900 lines; split by
  cohesive behavior before crossing that limit. A package split preserves its public API through
  explicit re-exports and must not turn `__init__.py` into a new dependency hub.
- Delete unused imports, dead branches, commented-out code, obsolete compatibility paths, and
  redundant helpers in the same change. Do not leave placeholder files or empty directories to
  imply support that does not exist.
- Catch only exceptions the boundary can handle. Never use a broad exception to turn invalid
  authority, programming errors, or partial writes into apparent success. Error messages name
  the violated contract, the observed value, and the legal next action.
- Keep mutation and I/O at owning boundaries. Domain modules remain deterministic and free of
  filesystem, network, Blender, SDK, global-state, and generated-artifact dependencies.
- Tests live under `src/tests/` and mirror the production concern (`unit`, `contract`,
  `architecture`, `integration`). Every structural rule that can regress gets an architecture
  test or lint rule; prose alone is not enforcement.
- Before handoff, run Ruff on the complete `src` tree, run the relevant tests, then run the full
  suite for cross-package or import-order changes. Never report a failing or undiscovered suite
  as clean, and never weaken a check merely to land the change.
- Git commits use the repository or user's configured author identity only. Never add
  `Co-authored-by` trailers or other authorship attribution for Cursor, Codex, Claude, an AI
  assistant, or an agent, and never modify Git identity configuration to manufacture attribution.

## Non-negotiable invariants

- Nothing self-certifies. A model verdict cannot replace authoritative executable evidence.
- Parallelize evidence production; serialize authoritative scene mutation and integration.
- Empty-scene replay is the source of truth for a published build artifact.
- Every replayed artifact publishes freshly evaluated current-frame Blender state
  before a successor script, interface check, or evidence reader runs. Candidate,
  revalidation, warm-start, ablation, canonical, and composed constituent replay share
  that barrier; composition is not raw script concatenation (HIR-0117).
- Builders and repairs may mutate only declared semantic roles, controls, and script spans.
  Each unit owns exactly one identity-derived replay file
  (`build/units/<layer>/<unit-id>.py`); composed layer paths and `#fragment` notation
  are not script authority (HIR-0126).
- Fail closed on stale, ambiguous, incomplete, or schema-incompatible authority.
- Core code must not contain shot names, display-name selectors, fixed shot frames, fixed layer
  or unit counts, scene-specific coordinates, keyword inference for moments or evidence modes,
  or department assumptions presented as universal.
- A current shot is a fixture, not a template. Generalization requires heterogeneous held-out
  fixtures and injected failures; no mechanism is general because it worked on the development
  shot.
- Strict migration, no silent compatibility: obsolete schemas and artifacts are migrated or
  rejected, never interpreted heuristically. Compatibility windows declare a deterministic
  expiry and fail closed after it (ADR-0004). A change to what a unit or layer capsule
  contains is a digest generation change: bump `DIGEST_SCHEMA` with the golden digest tests,
  and migrate prior-generation durable state only through `vfx migrate-digest-schema <shot>`,
  which republishes the selected view and supersedes those units and terminal receipts with
  a typed reason (HIR-0182).

## Decision quality: smarter agents through instruments, not guesses

Agent capability is a harness product. Improve it. An agent forced to guess, rediscover
authority, or act without seeing the effect of its last mutation is a harness defect, not a
model limitation to paper over with a longer prompt.

Wherever model judgment must decide, the harness supplies ground truth first: typed
introspection, deterministic measurement, enumerated options, and read-back of every mutation's
effect.

- Query, don't recall: agents act on authoritative state read through tools, never on memory of
  the scene, the plan, or a prior run.
- Semantic roles use one matcher everywhere. A literal dotted selector names its exact tag
  and every dotted descendant (`building` includes `building.mass.tower`); wildcard selectors
  retain fnmatch behavior, and prefixes without a dot do not match. Aggregate rendered-subject
  evidence composes every matching surface. A single-host tool that reaches several descendants
  fails closed and enumerates legal `object=` choices (HIR-0147).
- Camera availability comes only from typed `provides: ["camera"]` authority; role names,
  including `camera.target`, never imply a capability (HIR-0098).
- `inspect_scene(render/lights)` exposes the world/compositor identity, EEVEE volumetric and
  view-layer pass state, and light shape/distance settings; do not smuggle those reads through an
  idempotent `run_bpy` assignment (HIR-0055). Every `inspect_scene` call re-evaluates
  the selected or current frame and reads evaluated object, camera, and light hosts;
  omitting `frame=` never means accepting a stale depsgraph (HIR-0116).
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
- Camera-relative placement of proposed coordinates uses the read-only
  `check_scene(kind='projection', frame=…, points=[[x,y,z], …])` instrument. It evaluates
  the active camera and frame and preserves off-frame coordinates; never create temporary
  marker geometry, broaden roles, or reproduce projection math in `run_bpy` to answer a
  coordinate query (HIR-0136).
- Measure, don't estimate: if a decision depends on a quantity, expose an instrument that
  measures it; a judgment call where a measurement is possible is a patch.
- Coupled multi-frame `bbox_*` bands over a sealed camera are decided by
  `check_scene(kind='bbox_feasibility', roles=…)`, which searches every axis-aligned proxy box
  under the real camera and returns the satisfying box or the binding rows. After six
  consecutive mutations that leave the same bbox row failing, `run_bpy` is refused until it
  runs, and an infeasible verdict under harness-derived bounds fails closed into
  `cannot_express_in_scope` naming the camera provider; an infeasible verdict under
  builder-narrowed bounds proves nothing and never downgrades an earlier feasible one.
  Framing and bbox checks on a shared role return the union the contract measures
  (HIR-0183).
- Form builders inspect off-axis geometry with the typed `inspect_view` instrument, never by
  moving the shot camera. It resolves a semantic role namespace, offers bounded orbit/elevation/
  through-camera Workbench views and optional transactional soloing, then restores camera,
  frame, visibility, and temporary datablocks in `finally`. These images are diagnostic-only,
  mint no payment handle, and cannot satisfy a contract; geometry units discover the tool in
  compiled unit context rather than a larger global prompt (HIR-0148).
- Look-less form/layout reference comparison defaults to a live Workbench-solid plate when
  `compare_frame` omits `mode`; an explicit mode is honored. That diagnostic can guide geometry
  through the sealed camera but never pays EEVEE beauty debt. An `image-signal-bootstrap`
  rejection names the two legal paths: bind a typed optical-signal provider for genuine beauty,
  or declare `look_capabilities: []` and bind executable scene/projected-composition claims;
  otherwise escalate missing authority (HIR-0131).
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
  requested and what actually exists (HIR-0018). An artifact execution-policy rejection names
  the line, the escaping expression, its capability chain, and the legal replay forms
  (HIR-0174).
- A JSON-pointer list miss reports the live length, valid indices, stable row ids when
  present, `-` as the final-token append action, and that a list token may be
  `id=<row id>` (HIR-0177). Negative indices never mutate from
  the end; candidate repair does not make the materializer guess list occupancy (HIR-0105).
- The staging schema offers only claim authorities an authored claim can carry
  (`executable_required`, `advisory`); `qualified_qualitative_required` is minted by the
  harness for judgment debt, never staged. A staged unit whose mutated role has no
  required claim covering it is refused at the stage call by the same predicate claim
  closure applies, naming the roles and the unit's judged subject roles, the cross-row
  contract contradictions run over the candidate at every stage and patch call, and a
  composition-owning layer's camera unit cannot stage while a judge frame lacks a `bbox_*`
  row of a rendered subject — the plan gate's `composition-coverage` predicate is the same
  function (HIR-0177). More generally, every stage call runs the terminal collectable
  validator on the proposed candidate inside the write transaction and refuses any finding it
  introduces that is addressed under the staged unit, a supplied row, or a supplied id;
  layer-level findings are listed with the STAGED result and settle at finalize (HIR-0180).
- A plan-workspace path miss names the requested path and the staged relative files in
  that cwd, and forbids prefixing another filesystem root. Draft, verify, and repair
  kickoffs compile the same relative-read card (HIR-0156).
- An unresolved write-family rejection names the unresolved mutated roles, the family
  already derived for siblings, its registry-backed write-kind witnesses, and the legal
  mutation selector fields. Comparison selectors such as `compare_roles` are read-only
  and never force the materializer to discover that rule by staging retries (HIR-0103).
- An unpublished staged unit that becomes invalid after cross-unit validation is retired
  only through the typed, revision-checked materialization transaction. Retirement names
  one unit, refuses surviving dependants or consumers, and prunes only authority no
  surviving unit binds; it never grants whole-array patch authority (HIR-0104).
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
on the staging tool whose script span is the identity-derived unit file, projection consumption that matches the exporting interface, locked
revision-checked materialization candidate writes, deferred-owner domain AND-coverage
against the owner layer's already-validated evidence_domains, subject composition as
bbox of a rendered subject due when geometry exists, vacuous origin/bbox bands,
earlier-layer camera in cannot_express options, a plan-workspace path miss that
enumerates staged relative reads so a verifier cannot invent another filesystem
prefix (HIR-0156), camera-host optics plus motion that stay one camera write-cluster
(HIR-0157), deferred bbox `activates_at` compiled from the selected DAG so a camera
layer does not ask_supervisor for layer occupancy (HIR-0158), materialization
validation that refuses required contract role selectors the binding unit does
not mutate (HIR-0159), global publication that refuses form `reserved_roles` on a
camera-providing layer so materialization cannot be asked for geometry it cannot
stage (HIR-0128), image-contract debt that cannot publish before a mesh,
volume, or compositor carrier is in the replay prefix (HIR-0160), same-layer
mutation roles that cannot be dressed so a sibling look unit does not retry
`layer_updates.dressable` until max-turns (HIR-0161), a construction route whose
generate/retrieve legality is derived from a mesh write family and `refobs-*`
witnesses so a shading or instancing unit cannot claim image-to-3D (HIR-0162), a
multi-image Meshy adapter that cannot drop extra views onto a single `image_url`
(HIR-0162), generate-construction plates that call Higgsfield with a parent crop
and fail an all-white identity card before Meshy (HIR-0162), minted `refobs-*`
crops that register before generate staging, harness-owned plate→Meshy→promote
under `build/construction/<sha256>.glb` with `bvfx_import_construction()`, and
retired `vfx asset` / generate-unit `import_asset` (HIR-0162), and
earned qualitative judgment where executable evidence cannot decide. Not wanted: larger prompts or longer sessions as the scaling strategy,
prompt-only patches for mechanical defects, a confident model verdict replacing executable
evidence, or extra mutation authority so a builder can "figure it out".

The same standard binds coding agents on this repository: resolve unknowns by reading authority,
running code, or adding a probe — never by assumption.

## Claude Agent SDK leverage

Treat the Claude Agent SDK as a production runtime, not merely a prompt transport. Before
building custom orchestration, inspect the installed SDK and use the strongest applicable native
mechanism when it improves control, observability, or agent capability.

- Prefer SDK-native typed messages, tools, hooks, permission and tool policy, MCP integration,
  session lifecycle, cancellation, resume, model configuration, and usage reporting over custom
  glue that recreates the same contract.
- Preserve structured SDK events, tool errors, termination reasons, usage, and results until the
  owning boundary records them; do not flatten authoritative state into prose prematurely.
- Give each role bounded tools and compiled active-unit context. SDK capability never grants an
  agent repository-wide context, unrestricted mutation, or broader authority by default.
- Use hooks and tool policy for deterministic enforcement. Prompt text is not a substitute for a
  mechanical boundary the SDK can enforce.
- Resume only when checkpoint, journal, authority generation, and active-unit identity still
  match. A resumable SDK session does not by itself prove a valid harness resume.
- Choose model, effort, turn, token, and cost budgets for the role and measured uncertainty; do
  not maximize every setting indiscriminately. A materialization session's turn budget is the
  larger of the requested cap and 24 plus two turns per owned requirement, capped at 96; an
  exhausted session still publishes nothing (HIR-0177). Parallelize independent evidence production while
  authoritative scene mutation and publication remain serialized.
- Before adding an SDK workaround, verify the installed SDK does not already provide the needed
  primitive. Pin and test every SDK behavior the harness depends on, and fail closed when an
  upgrade changes message schemas, hooks, tools, permissions, or session semantics.
- Adopt an SDK feature only when it removes a measured bottleneck, closes a control gap, or
  improves evidence-backed agent performance. It never replaces domain contracts, replay,
  deterministic validation, or acceptance evidence.

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

## Root-cause resolution

Every bug, bottleneck, quality gap, recurring pain point, or unexpected outcome is evidence of a
system mechanism that permitted it. Trace and remove that mechanism at its owning boundary; never
patch only the visible symptom or specialize the fix to the scene that exposed it.

- Reproduce the failure and trace the complete causal chain: what happened, why it was allowed,
  how the agent or runtime arrived there, what evidence or context was absent, and which boundary
  should have prevented acceptance.
- Classify the cause explicitly as an implementation defect, architecture bottleneck, reasoning
  failure, prompt/context failure, plan or contract defect, missing instrument, tool-policy gap,
  validation gap, or authority/provenance failure. Mixed causes name every contributing boundary.
- Fix the earliest owning cause. Do not use downstream guards, silent retries, broader tolerances,
  extra mutation authority, prompt reminders, or special cases to conceal an upstream failure.
- The root cause is the earliest decision or reasoning step that made the failure reachable,
  not the code where it surfaced. That step may be a design choice in an ADR, an assumption in
  an HIR, a plan or contract decision, an ownership boundary, or an agent reasoning step the
  harness allowed. Trace the chain back to that step and fix it there. When the originating
  step is a recorded decision, the fix supersedes that decision and migrates; adding code
  around its consequences is a patch. Fixing only the downstream code leaves the same decision
  free to produce the next variant of the failure elsewhere.
- Treat a reasoning failure first as a possible instrumentation problem: missing measurement,
  unavailable legal choices, incomplete bounded context, or absent mutation read-back. Ask the
  model to reason differently only when the remaining defect is genuinely judgmental.
- Prompt changes are valid for judgment or communication failures. Mechanical defects require
  types, contracts, tools, deterministic checks, state machines, or architectural changes.
- Record how the system reached the failure, including the assumptions and prior design decisions
  that made it representable. The explanation must identify a mechanism, not blame an agent.
- Generalize across scenes. Never encode the exposing shot's name, objects, coordinates, frames,
  layer count, vocabulary, or data layout. The original scene proves reproduction only.
- Prove the mechanism on heterogeneous fixtures and at least one injected failure. A regression
  test asserts the general invariant and fails without the fix; unrelated suites must still pass.
- If the permanent scene-independent fix exceeds current scope, stop and escalate with evidence.
  An explicit unresolved defect is preferable to a committed workaround.

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
  blockers. A still-open `deferred_owner` row declares `evidence_domains` from the same
  closed vocabulary as layer `evidence_domains` and `claim.asserts` (ADR-0003). Coverage
  is AND: the owner layer must already declare every domain on the row. A rejection names
  the requirement, its declared domains, the owner's domains, and every layer whose
  domains could cover the row. Contract and obligation rows bind concrete ids and do not
  carry this field (HIR-0124). Every layer materializes just in time; a dependency-root layer may materialize
  immediately after publication. Concrete evidence design — contract kinds, moments, thresholds,
  calibration, research, reference fingerprints — belongs to the owning layer's materialization,
  which fails closed until every owned requirement resolves (ADR-0005, ADR-0006). Materialization
  validation reports every collectable finding in one write, each addressed by an RFC 6901 JSON
  pointer; field repair is `patch_materialization` on the candidate file. Required scene-contract
  role selectors must close against the binding unit's `mutates.roles`/`dresses` in that write —
  a mutation-empty observer cannot look locally clean and then die on terminal-gate
  `role-selector-closure` (HIR-0159). A work unit may declare `construction` with a
  closed route (`procedural` default, `generate`, `retrieve`, `simplify`). `omit` and
  `abstain` are not unit routes. `generate`/`retrieve` require a mesh write family;
  `generate` requires non-empty `refobs-*` witnesses and cannot bind a required
  `object_count` whose minimum exceeds 1. Route legality is derived; `reason` is
  audit only. Materialization mints witnesses with `mint_refobs` on a `refs/` crop
  (not a whole frame) and refuses unregistered generate witnesses. `vfx asset` is
  retired (ADR-0009, HIR-0162). Unreadable JSON, wrong
  schema, wrong bundle hash, and a non-object layer remain fatal. Historical plan bundles are
  not a repair instrument (HIR-0023). Rematerialization writes a reverted overlay as the design
  base and selects only when the replacement publishes; crash, truncation, or a broken pipe
  leaves the previously selected view (HIR-0026). After global republication, a live JIT view
  pinned to the prior bundle is superseded state: rematerialization derives its unpublished
  design base from the currently selected sparse bundle and never carries prior-generation
  rows forward by proximity (HIR-0101). First materialization and rematerialization stage an
  exact semantic-capsule/state effect for every affected layer; the gate verifies it, and the
  authority-state publisher atomically selects the plan/JIT head and installs those state bytes.
  There is no post-publication `apply_replan`, direct `unit_state.initialize` adoption, or public
  `vfx units replan` state-movement path (HIR-0171).
  A selected `state/authority-state/pending.json` is the sole roll-forward authority and makes
  every ordinary reader/publisher fail closed. Recover it only through
  `vfx recover-authority-state <shot>`; that command verifies the exact staged before/after
  identities, performs no planning/Blender/render/critic/model work, and returns a typed result.
  Repeating recovery after the committed head is current is an exact state no-op apart from
  republishing a stale derived `accepted_build` projection, which its typed result reports as
  `accepted_build_projection` (HIR-0172).
  An immutable completed-unit receipt may cross a changed layer transition only when every
  contiguous immediate-predecessor edge preserves its exact unit binding and complete source
  closure. Changed or downstream-invalidated units are superseded even if they had passed. A
  terminal layer receipt survives only when the complete layer capsule, constituent receipts,
  predecessor terminal bindings, and layer source closure are unchanged; unit preservation never
  implies layer-finalization preservation. Missing or cross-schema identity fails closed, and an
  A -> B -> A sequence cannot revive a receipt omitted by B (HIR-0102, HIR-0171).
  Plain first-time `vfx plan --layer` uses the same gate-attested authority-state transaction over
  prior-generation durable units; a post-intent crash rolls forward only from the staged immutable
  bytes and never waits for a later plan call to repair selected state. Comparable current-schema
  state never requires reinitialization, deletion, or `--discard-accepted` (HIR-0133, HIR-0171).
  `--discard-accepted` permits a reviewed rematerialization to retire accepted orphans; it never
  authorizes an out-of-band state wipe and is not the door on remat (HIR-0052). Task and Agent are
  not remat repair instruments. An exhausted materialization session does not publish: max-turns is a failed
  transaction, not a select (HIR-0027). The sole exception is an exact current revision
  that a successful `finalize_materialization` call has bound to the selected bundle in a
  typed terminal attestation; candidate existence or patch validation alone never
  qualifies (HIR-0108). Structured decision adoption is last-write-wins for
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
  build-time debts, not `does not exist` (HIR-0047). JIT owned-requirement
  closure resolves contract ids through those same required-claim debt cards;
  `image_contracts` stays empty, and an optional image reference cannot close
  a required requirement (HIR-0122). An image claim's `property`
  must be payable by the canonical image-metric registry: authoring enumerates the
  closed vocabulary, staging and materialization refuse free-form properties before
  bytes publish, and the plan gate rejects incompatible selected views. Appearance
  prose belongs in `proposition`, not in an invented evidence property (HIR-0111).
  Those ids compile to a payment card (id, frame, property, axis); `propose_checks`
  must match all
  four fields and binds each multi-frame batch row to its own immutable candidate
  handle (the batch handle is only a same-frame shorthand); candidate freeze refuses while any remain unpaid without a
  typed `unpaid_image_debt` abstention; ids are bare, never `check:`;
  a `frame_delta` image debt is paid by a `frame_*` scalar that passes only on
  the candidate and fails the harness-captured pre-unit adversary; rejection
  enumerates the registered metrics that can certify the owed property rather
  than making the builder guess;
  readers of falsification `contract_ids` strip that prefix
  (HIR-0048). A unit that owes required image-contract debt publishes only when
  its own derived write cluster, a same-layer dependency closure, or an earlier
  materialized layer contains a pixel-affecting `light`, `shading`, `volume`, or
  `compositor` family. Look labels, role names, object counts, geometry, camera,
  controls, and keyframes cannot self-certify optical signal; the materialization
  and plan gates fail `image-signal-bootstrap` and enumerate registry-derived
  write-kind witnesses (HIR-0110). That optical-signal grant is not a rendered
  carrier: image-contract debt also requires a `mesh`, `volume`, or `compositor`
  family in the same replay prefix. A shading-only root on a camera-only scene
  cannot pay beauty; the materialization and plan gates fail
  `image-subject-bootstrap` and name same-layer carriers outside the dependency
  closure (HIR-0160). Absolute pixel statistics (`render_region_stat`, `control_render_response`) bound by a
  required image claim are the same debts: a
  camera-only prefix cannot carry them, and a camera unit that creates a World to tint a
  plate is mixed volume work refused before mutation (HIR-0176). Scene contracts may measure other frames; bind
  those ids through `composition_context.contract_ids` without adding the extra
  frames to the judge lists. Claim-closure counts those ids as bound producers.
  A dependency root has no sealed outcomes to directory-Read (HIR-0029).
  A `keyframe_schedule` whose consecutive samples already exceed a same-role
  `curve_derivative_max.hi` is refused at materialization, authoring, and the
  plan gate — interpolation cannot invent a third option (HIR-0030). Two `curve_derivative_max`
  rows on the same roles and property whose floor window lies inside a lower cap window are
  refused at the same boundaries — no curve satisfies both (HIR-0175). An `object_count` upper
  bound over a literal namespace whose dotted descendants sibling rows require (same layer, or
  every layer for a `persistent` row) is refused there too, naming the matcher rule, the requiring
  rows, and the leaf-role or raised-bound fix; the object_count read-back names every matched
  descendant and carries the selector beside the matched roles (HIR-0178). A falsification stop
  compares the finding's selected layer capsule digest, never a whole-file hash (HIR-0175).
  A required claim that binds `visible_fraction` is repaired by a unit that
  `provides: ["camera"]` or mutates/dresses every `roles` selector on that row;
  a volume-only unit cannot bind mesh vis as required repair. Multi-role vis is
  logical AND across named roles. A required vis row becomes due at its typed
  `repair_owner` unit. That owner pays the row; every downstream geometry unit whose
  dependency closure contains the owner freeze-protects it. Geometry before the owner
  does not pretend the future surface exists. Unordered geometry is rejected with the
  one missing acyclic dependency; ambiguous/unbound vis remains conservatively
  layer-active and mutual ambiguity is reported as one cycle (HIR-0051, HIR-0057,
  HIR-0106, HIR-0132). A unit-local runtime view narrows judges and mutation through
  `active_unit` but retains the parent layer's complete typed unit DAG; pruning sibling
  stages would manufacture ambiguous ownership and falsely bill future visibility to
  the active unit (HIR-0135).
  Every scene-contract kind that projects or renders through the active camera must bind
  on a unit whose dependency closure (or an earlier materialized layer) provides a camera;
  otherwise publication fails `composition-bootstrap` (HIR-0085).
  A projected_composition owner covers each judge frame with `bbox_*` of a rendered
  subject, not `projected_origin` of a camera-only host. A normalized `band` wider
  than half the frame is vacuous. When the subject does not exist yet, the camera
  layer authors those bbox rows with `activates_at` equal to the compiled
  `earliest_geometry_layer` from the selected DAG (HIR-0158), `lifecycle: persistent`,
  and `fault_owner` on the camera owner; the camera unit
  binds the ids through `composition_context` and does not seal them. Geometry units
  that mutate the measured roles freeze-protect the active deferred rows. At the exact
  activation layer, layer-start prior-interface replay excludes the not-yet-instantiated
  subject. Parent selectors may span several truthful geometry write clusters; the first
  unit whose dependency closure contains every overlapping producer pays all owner-frame
  rows. Publication rejects a DAG with no such payer. Later layers protect the persistent
  bbox on every overlapping geometry mutation. Before that activation-layer payer, each
  overlapping geometry producer receives the exact deferred rows as diagnostic-only
  forecasts evaluated at their declared frames. Forecasts teach mutable partial producers
  but never enter required evidence, checkpoint protection, sealing, canonical payment,
  or revalidation authority; the dependency-complete producer alone pays the final union
  (HIR-0134, HIR-0151). One directional exception fails closed before producer freeze:
  projected union width/height/bottom cannot decrease as successor geometry arrives, and
  union top cannot increase. A partial producer already beyond a bound on that
  irreversible side must repair or call `cannot_express_in_scope`; repairable-side misses
  remain diagnostic and bbox centres are never inferred monotonic. Live judgment,
  empty-scene canonical replay, and deterministic revalidation all consume those blocker
  ids as required evidence; producing a blocker row without adding it to the verdict's
  required set, or swallowing blocker compilation errors, is forbidden (HIR-0152).
  Post-mutation readback and exact-id `contract_result` classify those same rows with
  the same irreversible-union function used by verdicts. A blocker is labelled
  `REQUIRED BEFORE FREEZE` immediately and names repair or
  `cannot_express_in_scope`; only repairable-side misses may be described as
  diagnostic-only (HIR-0153).
  More generally, any scene contract whose `activates_at` differs from its
  `owner_layer` is inactive at the authoring unit: it may bind through
  `composition_context.contract_ids`, never through that unit's claim evidence
  (HIR-0129).
  A future-active contract keeps its owner layer's judge-frame authority; the
  activation layer pays that moment as extra-frame evidence and must not add it to
  its own judge list. Runtime schedules every bound static row at its declared
  `frame` or `frames` (falling back to the active judge only when unframed), and
  lifecycle-tests it there. Live read-back, empty-scene canonical replay, and
  deterministic revalidation all require geometry-protected extra-frame readings;
  filtering them through the activation layer's judge list is forbidden
  (HIR-0130, HIR-0143).
  `cannot_express_in_scope` may name an earlier-layer camera provider; the finding
  records those ids as `fault_owner_units` without treating them as same-layer
  affected seeds (HIR-0127).
  Camera availability is global DAG authority: each sparse layer declares `jit.provides`
  as capability → reserved-role selectors, every judged layer's transitive closure must
  contain camera, and a materialized camera unit must mutate one of that layer's exact
  reserved camera-interface roles (HIR-0086, ADR-0005).
  That sparse camera grant compiles the materialization's unit capability vocabulary:
  a camera-providing layer may stage camera/control units but cannot add
  `provides: ["geometry"]` to manufacture subject framing. Global publication refuses
  `reserved_roles` selectors that do not match the camera grant; those selectors belong
  on a later layer that does not provide camera. It authors persistent
  downstream rendered-subject `bbox_*` rows and binds them through
  `composition_context`; non-camera form layers retain local geometry authority
  (HIR-0128).
  A work unit publishes one derived write-cluster (role-namespace × host class ×
  instrument family); authored family strings are padding and do not satisfy the
  gate. A typed producer capability is additive mutation authority, not a fallback
  label: geometry plus keyframe evidence is mesh plus keyframe work. Camera-host
  optics, placement, and motion remain one camera family even when a camera-property
  row is bound with temporal evidence on that host (HIR-0095, HIR-0112, HIR-0157).
  Instrument family comes from typed mutation targets and write-kind evidence;
  object-level transforms are control state, and a zero-animation bound is observation,
  not keyframe authority (HIR-0095, HIR-0112). Unresolved families fail closed. Dressing, vis
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
  temporal enums, the active layer's exact owned claim-axis ids, the identity-derived
  `build/units/<layer>/<unit-id>.py` script span (HIR-0126), optional
  composition-context union, and exact typed publish/consume fields are enumerated
  before generation rather than learned through parser retries
  (HIR-0097). Materialization mutation roles use a cluster-shaped authoring type: one
  two-token `role_namespace` plus relative `role_members`, compiled to durable full
  selectors before staging. Absolute `mutates.roles` is not accepted on that tool, so
  one request cannot express two write namespaces; `$self` names the namespace tag
  itself (HIR-0150). An `interaction` claim requires the complete conditional coordination
  shape — `coordination_owner`, at least two `participants`, and non-empty `controls` —
  while an `atomic` claim forbids all three. Owners and participants are exact
  same-layer work-unit ids, never semantic roles or controls; a miss enumerates the
  valid unit ids (HIR-0123). Every staging or patch write uses one locked, revision-checked candidate
  transaction. Only `stage_materialization_unit` may add, replace, or reorder stage rows;
  field patches affecting units or their contracts run the same local staging gates before
  bytes change. Concurrent calls cannot overwrite a previously staged prefix (HIR-0100).
  `unstage_materialization_unit` is the only retirement surface for unpublished scratch:
  it removes one named unit under the same transaction, refuses surviving dependency or
  consume edges, and prunes only rows no surviving unit binds (HIR-0104).
  Terminal preview of a replacement stages the candidate documents, the exact all-layer
  successor state images from the prepared authority-state transition, and one canonical
  content-addressed preview reference. The independent gate reopens that reference through the
  live predecessor head, transition intent, capsule/effect digests, before/after state hashes,
  JIT manifest targets, immutable staged members, and source-closed preserved receipts. It
  recompiles the effects and exact successor states/bindings from the live predecessor and
  candidate capsules; producer-authored effects never self-authorize. Preview receipt authority
  is a distinct typed value and
  cannot authorize live planning or build claims. The preview must not compare the
  post-publication candidate DAG to pre-publication state, skip hierarchy validation,
  reinitialize state, or mutate the selected predecessor before publication. An unprojectable
  digest schema or transition closure fails closed (HIR-0140, HIR-0171).
  Materialization exposes one terminal operation: `finalize_materialization` performs
  collectable validation, stages that coherent preview, runs the exact deterministic gate,
  and attests only its clean candidate revision. A materialization session does not expose
  a separate terminal `gate_preview`; every subsequent patch invalidates attestation and
  requires finalization again (HIR-0141).
  Materialization binds `transcript` and `costlog` (`materialize-layer-{id}`);
  `log_message` journals only when bound (HIR-0038). `publish_unit_plan` stamps the
  bundle-pinned integrity sidecar with the bytes it publishes so the session's own
  `gate_preview` evaluates the draft; gate attestation remains the terminal gate's alone, and
  a stale `planning` claim with no live session is released only through `vfx units retry`
  before `vfx build --layer` regenerates the plan (HIR-0174).
- A decision is made globally only if it is needed before the first unit, alters the DAG, is
  irreversible, or is expensive to be wrong about later; otherwise defer it to the owning layer
  (ADR-0005).
- Global planning reads only `brief.md` and `refs/` inside an isolated run workspace. Prior
  plans, contracts, builds, and runs are not implicit planning input; planner writes outside the
  workspace are denied.
- Scene-dependent values declare a strength — `hard_constraint`, `approved_start`,
  `planner_start`, `confirmed_outcome`. Only accepted executable evidence pinned to a checkpoint
  creates a confirmed outcome; records without a strength read as hard constraints (ADR-0004).
  A requirement originally deferred to a layer but selected as `approved_start` or
  `planner_start` remains build-time debt: cumulative composed replay receives an
  independent reference judgment on the exact decision statement. Look-less composition
  uses Workbench solid through the sealed camera; failure records a contract gap for
  bounded replanning and never grants cross-unit repair authority (HIR-0137). A qualification
  binding id cited by that critic is typed judgment authority, not a missing scene check. The
  exact concrete observation is persisted as the composed contract gap, mapped to producer
  roles, and published as a replan-consumable finding while every already-passed checkpoint
  remains frozen. The finding, not a score-only failure or operator choice, reopens the
  affected producer/downstream closure (HIR-0139). Rebinding that requirement to executable
  producer contracts during rematerialization does not confirm its qualitative proposition:
  an exact current-bundle typed falsification keeps the approved/planner-start composed
  reference debt alive. Findings from another bundle are inert, and only a selected
  `confirmed_outcome` retires the debt (HIR-0142).
  Under requirements v2, every provisional image binding names one immutable typed
  judgment-debt definition: exact requirement statement, semantic owner, fault-owning unit,
  subject selectors, owner moments, axis, carrier families, observation medium, lifecycle,
  and digests. The harness derives `activates_at` from the selected DAG and the canonical
  semantic-role matcher; the planner never authors it, an unrelated carrier cannot trigger it,
  and a DAG with no reachable matching provider fails before builder or critic spend. The
  executable artifact and debt are independent state machines: an owner artifact may pass
  while debt is `pending_not_due`. Selected payer rows alone do not make it `due`: the
  empty-scene verifier invokes activation only after the actual cumulative replay succeeds,
  with a receipt proving every exact payer unit is currently passed and its unit artifact still
  matches its accepted checkpoint. Qualified canonical evidence then makes the debt
  `satisfied` or `falsified`; final acceptance refuses every unresolved current debt. A
  no-signal plate never invokes a critic or becomes qualitative falsification, and activation
  cannot conceal a fault owner whose mutation/dressing authority does not cover a non-camera
  appearance subject. Every due observation compiles an exact pre-render request from current
  authority, ordered checkpoint-concordant replay, declared point/reference, carrier-aware
  evaluated Blender state, promoted assets, medium/mode/scale, comparison, and judge config;
  the raster publishes its actual settings and PNG digest separately. A no-signal result is
  append-only attempt state and leaves debt `due`. Direct restart must re-prove replay, then
  suppress raster and critic for the unchanged request; only a changed relevant digest permits
  one new attempt (HIR-0163). A judgment payment binds the exact stable global topological
  replay prefix through its payer, including independent earlier layers; caller-supplied subsets,
  authored array order, and directory order are never prefix authority. Every strict predecessor
  row binds its current terminal finalization receipt, while the payer crosses observation through
  its exact active finalization claim and durable payment additionally requires the resulting
  terminal receipt. Every row must equal the payer group's v2 replay receipt's complete script,
  dependency, and dependency-ordered unit-completion inputs; the observation request additionally
  binds that group receipt, reference digest, render mode/scale, activation, definition, and
  payment generation. Omission, reordering, substitution, or an earlier independent-prefix
  change invalidates the payment generation rather than reviving a semantic A -> B -> A match
  (HIR-0170, HIR-0171).
  More generally, every deferred requirement's `evidence_domains` is logical AND through
  materialization: structural domains require same-domain registry contracts, while an unpaid
  `image` or `human` domain requires explicit `approved_start`/`planner_start` judgment debt.
  Mixed requirements bind contracts and that decision in one row. Selected concrete authority
  retains `evidence_domains` plus exact typed `domain_bindings`; the terminal gate re-derives
  every contract domain and rejects relabelling, structural decisions, missing domains, and
  padding decisions. Approved/planner starts schedule judgment; they are not confirmed outcomes
  (HIR-0124, HIR-0145). Every required structural claim binding must itself match the
  claim's canonical metric domain; one valid metric cannot hide incompatible count or
  projection padding. Likewise every requirement contract id must belong to one of its
  declared domains and every concrete resolution id must survive in exactly one typed
  domain-binding row. Cross-domain observations belong in `composition_context` or a
  separately typed claim (HIR-0146).
  A provisional domain binding never authors a second proposition: its statement must equal
  the exact cited requirement statement, and materialization chooses only approved/planner
  strength. The staging schema enumerates those authored statements, selected parsing rejects
  any rewrite, and composed judgment reads the immutable sparse-bundle proposition rather than
  binding meta-text. Vocabulary-gap or downstream-deferral prose is audit context, never a
  replacement for what the current layer must be judged against (HIR-0149).
- Unit plans publish only through the gate-attested two-phase transaction (HIR-0016). Do not
  hand-author placeholder units, edit `state/jit-layers/current.json`, or reinitialize,
  hand-edit, or delete durable work-unit state to make a new DAG fit. The complete selected
  `layers.json` hash is not a unit acceptance identity. Schema-closed unit and layer capsules plus
  the immediately preceding coordinator binding determine the exact preserve/change/remove
  effect, and plan/JIT publication commits that effect with pointer selection (HIR-0040,
  HIR-0171). A typed falsification cannot reopen unchanged authority: `vfx units replan` is
  retired, and no public finding-consumption adapter exists. When a finding names out-of-layer
  `fault_owner_units`, the reviewed replacement authority must actually change the exact owning
  capsules before publication may invalidate their closure; an unchanged owner cannot authorize
  repeated work it has no scope to repair (HIR-0049, HIR-0154, HIR-0171).
- When passing requires a decision, dependency, ownership, scope, contract, or sealed-outcome
  change outside the active unit, record `hypothesis_falsified` and stop. Replanning is a
  versioned transaction: freeze accepted state, validate the amendment, compute the complete
  invalidation closure, preserve unaffected checkpoints, mark replaced outcomes `superseded` and
  terminally unsatisfied dependants `blocked`, publish atomically, resume at the earliest legal
  unit. Builders and repairs never rewrite plans or broaden their own scope. A decision a
  layer's materialization makes on a requirement the bundle deferred to it belongs to that
  layer's capsule alone (only a decision on a never-deferred requirement is shot-wide), and
  `vfx run` re-derives the receipt-backed prefix after every just-in-time publication and
  builds a reopened lower layer before the newly materialized one (HIR-0181).
- Reopen a fixed or interrupted unit only through the audited `vfx units retry` transition, with
  reason and evidence. A reopened unit whose executable rows already pass may mutate until the
  first in-session verdict — the convergence guard cannot treat a failed qualitative claim as
  sealed work (HIR-0021). Automatic scene-contract read-back is likewise not candidate freeze:
  an executable-only unit remains mutable until terminal handoff so an intermediate structural
  floor cannot strand unfinished authored tickets; image-bound work still pauses mutation for
  immutable comparison once its scene interfaces pass (HIR-0118). Promote a retained clean candidate through `vfx plan --promote-run`,
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
- `keyframe_schedule` and `object_property` rows on `data.*` paths judge every selected host
  that owns a data-block: a host with none (a rig's Empty pivot) is typed out and named in the
  note, a data-block that lacks the attribute is a failing measurement, and a selection with no
  carrier fails closed. `bvfx_camera_rig` tags the pivot `<role>` and the camera
  `<role>.camera`; the rig is not demolished to pass a lens contract (HIR-0174).
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
  Threshold operators have one enumerated field shape: `eq` uses numeric `value`
  and optional numeric `tol` (there is no `eq` field), `min` uses `lo`, `max`
  uses `hi`, and `band` uses both `lo` and `hi`; validation names the exact field
  on the first rejection (HIR-0125).
  A projected `band` whose width is greater than half the normalized frame is
  vacuous. `projected_origin` of a camera-only host is alignment, not subject
  composition coverage; coverage is `bbox_*` of a rendered subject, deferred to
  the compiled `earliest_geometry_layer` when that subject does not exist yet
  (HIR-0127, HIR-0158).
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
  prompt, evidence layout, or claim semantics invalidates qualification. Work-unit claims carry
  `executable_required`, `qualified_qualitative_required`, or `advisory` authority;
  `human_required` / `human_decision` are retired because no runtime producer pays them — the
  human domain is judgment debt on the owning requirement (HIR-0174).
- A critic panel estimates score noise; bare pass votes do not refute a qualified actionable
  observation because passing scorecards carry no blocking proposition. A nominal passing
  majority with uncontradicted actionable dissent remains `REVISE` and preserves that exact
  row. Executable contradiction or typed conflict/gap/protocol reconciliation may retire the
  dissent; voting alone may not (HIR-0144).
- Critics describe qualitative residuals and cite evidence. They never override a passing
  authoritative measurement of the same fact and never prescribe unverified implementations. No
  repair is justified by unsupported measurement prose.
- Executable-only units call no visual critic and owe no raster before their typed
  scene/interface verdict. Live evaluation, canonical empty-scene replay, composed
  fan-in, and revalidation must reach executable evidence without rendering; a
  registry-declared functional image metric still owes raster (HIR-0114). Evidence is
  filtered to the active unit's exact bindings; sibling and future contracts cannot
  judge a unit or authorize a repair. Finalizer and repair `probe_candidate` read-back
  uses the same unit-and-frame evidence boundary, including only typed geometry
  visibility protection exceptions; it never exposes sibling rows as repair authority
  (HIR-0115).
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
  atomic claims stay protected, and anything broader requires a validated authority replacement.
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
  active unit: mutation roles/controls/dresses/spans, bound contracts, the camera-owned
  deferred subject rows the unit pays or protects as required evidence (HIR-0174), claims, judge frames,
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
  at the selected checkpoint. The Blender session mints the one checkpoint-owned journal
  destination (`checkpoints/journals/`) and `restore` re-stages a parent-published checkpoint
  into the confined worker's scratch with verified bytes, because the worker cannot see
  `checkpoints/`; a refused or failed journal capture fails the unit
  finalize closed, never degrades the finalizer to memory re-derivation, and every finalize or
  repair script session journals its kickoff and continuation prompts in the build transcript
  (HIR-0174). Layer materialization has no raw `Read` surface: kickoff compiles
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
  (HIR-0036). The first `compare_frame` scale is the measurement floor derived from the
  shot's frame height, never a fixed fraction that assumes one resolution (HIR-0174). Repair `probe_candidate` on a look-owning unit also returns draft
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
  Every model response stream also has a positive configurable event-idle deadline. If no
  SDK event arrives before it, the phase fails closed as `model_session_idle_timeout`,
  journals the deadline, message count, and last event type, and publishes no candidate.
  Turn and spend caps do not bound a stream that never emits a terminal result; an operator
  interrupt is not the normal timeout mechanism (HIR-0138).
  Optional context-usage telemetry is bypassed after authoritative provider error facts;
  observability may not delay terminal propagation (HIR-0080).
  A scoped unit may not write free-form renderer policy through `run_bpy` (`eevee`, `cycles`,
  `render`, color/display settings). Use transactional render diagnostics; renderer-policy
  authority requires a typed plan control, and infeasibility under canonical settings routes
  to `cannot_express_in_scope` (HIR-0076).
  Before execution, every `run_bpy` payload is classified from high-confidence typed helper
  and Blender API calls and checked against the one write-cluster in the compiled unit card.
  Mixed payloads fail before scene mutation or journaling; `bvfx_role`/`bvfx_control` tagging
  cannot launder mesh, shading, light, camera, volume, compositor, or keyframe work into scope.
  Explicit `dresses` permits shading and camera-host keyframes remain camera work (HIR-0112, HIR-0157).
  Candidate `probe_candidate` applies the same scoped new-object role validation as
  canonical verification before returning evidence; an undeclared helper/role is a probe
  failure, never a green repair read-back (HIR-0058).
- Protection wildcards resolve to an explicit sorted contract-id closure at freeze; evaluation,
  repair, resume, and revalidation use that recorded closure, never a re-evaluated wildcard.
  A required `visible_fraction` row activates at its typed repair-owner unit; that owner
  and dependency-ordered downstream geometry freeze-protect it. Publication rejects an
  unordered later geometry unit rather than making earlier geometry owe a future subject;
  rows without typed ownership retain conservative layer-wide protection (HIR-0051,
  HIR-0057, HIR-0132).
  Publication also refuses a unit whose derived write-clusters are heterogeneous
  without a typed exception; the finding names the clusters. A successor consumes
  digest-matched publish interfaces from the compiled card, not producer scripts.
  Consumed exports are read-only; `depends_on` without `consumes` is a status-only edge,
  not interface compatibility, and grants no producer interface. Authored interface identity participates in the producer digest
  (HIR-0083, HIR-0084).
- Appearance on another layer's geometry is owner-granted authority: the owner declares
  `dressable` selectors, the dresser declares `dresses`, validation closes over both, and
  dressing is material assignment only — moving, deleting, or remeshing a dressed object breaks
  the owner's sealed contracts (ADR-0007). A selector mutated by any unit on this layer
  cannot be dressed here; this layer's `dressable` grants later layers only (HIR-0161).
  Image-to-3D is a `generate` construction route on a mesh-family source unit, not a
  builder-time `import_asset` choice. The adapter posts every supplied view (1–4) as
  `image_urls` to `/multi-image-to-3d` and never keeps `images[0]`; source-unit
  generation sets `should_texture: false`. Generate-construction plates (`isolate_relight`,
  `orbit_view`, `isolate_cutout`) run through the Higgsfield CLI with a parent crop
  handle; Cursor MCP is not the `vfx run` image adapter. Text-only generate is not a
  Meshy input. Before the builder session the harness crops minted witnesses, identity-gates
  plates, posts surviving views to Meshy, and promotes hash-verified bytes to
  `build/construction/<sha256>.glb`. Generate units call `bvfx_import_construction()`;
  `vfx asset` and generate-unit `import_asset` fail closed (ADR-0009, HIR-0162).
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
  active unit and those validated owner units. A finding records the proposed closure but does not
  mutate authority or state. Accepted checkpoints remain authoritative until a reviewed replacement
  publishes through the atomic authority-state transaction; no public finding consumer or
  controller may reopen them in place. A unit that consumes an earlier unit's scene or pixels
  declares the producer in `depends_on`; serialization order is not dependency authority
  (HIR-0056, HIR-0171).
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
- Image-to-3D candidates are not acceptance: Meshy thumbnails, `preview.png`, and isolate-regen
  plates do not seal a unit. Generation is a mesh construction route behind witnesses,
  qualification, and empty-scene replay of a promoted artifact under
  `build/construction/<sha256>.glb` (ADR-0009, HIR-0162).
  Generate-construction image-edits use Higgsfield with a parent crop; a white empty plate
  fails the identity gate before Meshy. `vfx asset` is retired.

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

Rules live authoritatively in this file. `.cursor/rules/` contains concise delivery mirrors for
Cursor and changes in the same commit whenever its mirrored rule changes. A change that creates,
amends, or retires a durable rule updates this file in the same change; ADRs and HIRs carry
reasoning and validation, never the only copy of a rule. If this file, a Cursor mirror, and a
record disagree, stop and reconcile — do not silently pick one.

## Verification

Run from the repository root:

```bash
.venv/bin/ruff check src
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
