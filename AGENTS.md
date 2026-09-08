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
  rather than at its first consumer view (HIR-0172). The worker confinement dev-binds every GPU
  device node the host exposes (`/dev/dri`, `/dev/nvidia*`), the worker's ping reports the GPU
  platform Blender initialized, and strict preflight fails closed when the host has a GPU but
  the confined worker reports software OpenGL: a sandbox that hides the GPU renders every
  plate on llvmpipe, twenty times slower, and looks like a slow agent (HIR-0194).
- The normal operation is `vfx run`. A shot with no selected plan authority first drafts,
  verifies, gates, and repairs its global plan as a child stage of the same run; an existing
  bundle is never redrafted by the driver (HIR-0186). It stops on the first unaccepted boundary
  it cannot dispatch; do not force downstream work past it. `--force` is a bounded debugging
  experiment, never a deliverable.
- Reading order after any invocation: `runs/latest.json`, then the selected run's
  `manifest.json`, `status.json`, `reports/summary.json`, `artifacts.json`; and when the stop
  is a `harness_defect` naming an unclassified boundary, `reports/unclassified-boundary-audit.json`
  before anything else, because that file holds the real exception type and message and the
  envelope only summarises it. Omitting it from this list is why drivers diagnosed these from
  console tracebacks while the faithful record sat unread one file away -- 46 such boundaries
  accumulated across three shots, and the drivers who investigated them did not know the file
  existed. Naming the audit in the stop's prose is not enough on its own, since identical
  envelopes train a reader to skip that prose (HIR-0226). Fail closed on an
  unsupported manifest schema. Every run is the `vfx-harness.run/v2` generation owned by a claim
  and fence; `status.json` is `vfx-harness.run-status/v2` and holds only the selected record
  locators and digests, terminal diagnostics such as `terminal_cause` live in
  `reports/summary.json`, and prior-generation runs fail closed (HIR-0172). For an unaccepted terminal run, `status.json` selects
  `reports/stop-envelope.json` by exact digest; that closed envelope is machine dispatch
  authority. An interrupted v2 run selects `reports/interruption-receipt.json` and its
  satisfied evaluation by digest and authorizes no transaction. `status.json` `detail` and the exit-code digit are operator diagnostics only
  (HIR-0037, HIR-0164). Materialization writes
  `reports/materialization-session-*.json`, selecting the request/result/usage journal
  under `checkpoints/flynn/` (HIR-0038, ADR-0012). Open detail
  (`reports/layers/`, `plan_gate.json`, `evidence/`,
  transcripts, checkpoints) only when the summary names a reason. Never diagnose by recursively
  listing the shot or grepping every transcript, and never parse meaning from filenames.
- Shot-root legacy directories (`logs/`, `renders/`, `.artifacts/`, `.snapshots/`, `.versions/`)
  are unsupported: no evidence authority, no write destination (ADR-0002).
- Diagnosis routes by cause: preflight/config failure → fix the environment, not VFX logic;
  plan-gate failure → repair plan/contracts and rerun the gate; a per-layer gate rejection is
  typed transaction authority, not a boundary defect (HIR-0187); builder evidence failure → the
  owning layer report and its cited evidence; `hypothesis_falsified` → stop, review the typed
  finding; the controller publishes the amendment itself when the finding's fault owners
  are sealed in one earlier layer of the run's range (the stop names that owner's view and
  cites every open finding naming it), and a multi-owner, out-of-range, or hard-constraint
  finding waits for reviewed authority through its owning plan/materialization boundary;
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
  receipt orphan before probing again (HIR-0166). `vfx run` is a controller that dispatches only
  receipt-backed transactions (ADR-0010): at a builder boundary it reads the child's typed stop
  and, for a `publish_validated_amendment` on a layer view, runs the same rematerialization stage
  an operator would, proves the commit from selected authority through an immutable
  `vfx-harness.rematerialization-commit/v1`, an independent evaluation, and a per-dispatch ledger
  row under the run (`reports/controller-dispatch-NN.json`), then re-derives the receipt-backed
  prefix and continues. A builder finding whose `fault_owner_units` are sealed in one earlier
  layer compiles its amendment on that owner's layer view (never the stopped layer, which
  cannot change them), the controller dispatches it with every open finding naming that owner
  as `--evidence`, the rematerialization kickoff renders those findings, and preservation or
  supersession of the stopped layer's units is decided by the authority-state transaction, not
  by asking the stopped layer (HIR-0191). It refuses, and the envelope stays terminal, when the
  fault owners span layers or are not the target's units, when the owner lies outside the run's
  layer range (named for the operator to include), when the finding changes a hard constraint,
  when the cause fingerprint was already dispatched in this shot, when a dispatch, per-layer, or
  USD cap is spent, or when the transaction kind has no adapter; `--single-pass` restores the
  single pass for debugging (HIR-0186). Before it builds a
  layer the driver gates the selected authority in process and scopes that verdict by
  ownership: a finding another layer owns belongs to that layer's transaction and does not
  block this one, every blocker the gated layer owns becomes one `publish_validated_amendment`
  on that layer view which the controller dispatches, and any plan-wide blocker resolves to the
  global scope the controller refuses as a reviewed operator transaction. A gate finding about
  one layer carries that layer in its typed `layer` field, never only in its message: ownership
  written in prose is ownership `clean_for` cannot read, which silently promotes one layer's
  finding to a plan-wide block owned by nobody. Construct those findings with
  `Finding.in_layer` so the rendered text and the typed owner come from one value; an
  architecture test enforces it (HIR-0187). The other five
  transaction kinds remain non-dispatchable, global amendments stay reviewed operator
  transactions, and no current producer proves `local_implementation_miss`; never infer local
  retry authority from a generic builder failure or progress from a repeated finding (HIR-0164,
  HIR-0166).
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
  A judgment debt owes an observation only at the judge points it owns: the group plan
  carries those points from the same value the payment compiler uses, the receipt demands
  an observation there and refuses one anywhere else, and a debt owning no judge point is
  refused. Demanding one at every point from the layer-level `debt_id` killed a legal
  group whose debt was due at one frame of three (HIR-0206).
  A replay claim requirement carries each bound row's declared frames, so an evidence id is
  due at the frame its contract declares and only falls back to every frame the claim judges
  when the row is unframed. A claim's judge list is not its bindings' schedule: taking the
  union demands each frame-pinned row at frames where it cannot exist, and the layer stalls
  on `missing` evidence with nothing failed (HIR-0204).
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
- Native Flynn failure types supply terminal diagnostics, never recovery authority.
  An inherited stage propagates its exact published stop on the original exception;
  the root selects that stop without reclassifying it under a different command.
  Preserve the exception and its usage. Cooperative cancellation without recorded
  signal intent remains failed, and no native exception grants retry or resume
  permission (ADR-0012).
- Global draft, verify and repair execute through Flynn in the live root-owner process.
  An inherited run directory or on-disk claim is not an ownership capability; native global
  dispatch requires the current process-bound lease for that exact run. Draft phase snapshots
  stay outside the gate workspace. Declared client blockers in the compiler-preserved
  `plans/ownership_mapping.json` are plan-wide gate blockers, never informational prose
  that a clean gate may ignore (ADR-0012, HIR-0252).
- Production unit routing derives from typed construction and evidence requirements.
  Procedural or generated units requiring only executable evidence use Flynn with the exact active
  building claim, live shot fence and explicit step, output-token and wall-time caps.
  Native failures propagate; never retry through the other engine. Qualitative and simplify
  construction routes retain their explicit existing engine until migrated. The layer
  controller and VFX receipt readers retain checkpoint/completion authority (ADR-0012).
- Generated construction preparation is a harness-selected scripted Flynn operation,
  never a model choice. Reserve capacity for the complete remaining path before staging.
  Preserve exact pointer, GLB and witness bindings through native dispatch and publication.
  Construction pointers derive from the same canonical unit path as scripts; scratch
  replay selects construction by its canonical locator, never a guessed alternate directory
  or a pointer beside the temporary source (HIR-0254).
- Native image-payment tools invoke the shared VFX image-check operation, register their
  returned dispatch guard and supply the exact candidate check. Keep the building claim,
  live fence, run/unit/parent-chain image identities and artifact bytes current through
  guarded publication. Payment observations never authorize acceptance; raster routing
  stays gated until capture, payment and canonical replay are integrated (ADR-0012).
- Native unit image capture uses declared judge frames, a frozen scene setup and the
  exact ordered prior sources/dependencies. Recheck receipt-backed priors through report
  publication; reconstruct the adversary without the candidate and cold-replay the exact
  candidate separately. Capture derives the unit's canonical solid or EEVEE medium;
  candidate and adversary use scale 0.5 and matching resolution. A rewritten
  candidate must be recaptured before payment; its previous handles are retired. No
  partial capture registers a handle, and capture reports grant no acceptance (ADR-0012).
- Native builder feedback preserves the original observation digest, structured data,
  complete refusal text and image-label order. Transport image bytes through Flynn's
  image inputs, outside the bounded text context; replace images with each selected
  observation instead of accumulating them. A tool refusal is not a satisfied execution
  assessment, and neither assessment authorizes VFX acceptance (ADR-0012).
- Native builder recipe retrieval shares the planning lookup and bounded read/fragment
  policy. Filter against the active unit's roles, admit only whole fragments fitting
  required context, and reserve write, capture/payment, probe, freeze and replay capacity
  before granting a read. Rereads consume budget; each result replaces prior feedback.
  SQLite records recipe use and content identity without a process-global usage list.
  Verification improves a relevant recipe's rank; it never creates a query match
  (ADR-0012, HIR-0255).
- The production dispatcher selects the native Flynn executor for procedural or generated solid or EEVEE
  image contracts with required executable claims covering every judge frame. Capture
  and payment invalidate the prior probe; freeze and canonical dispatch reopen payment
  identity and captured bytes. Reserve recapture after candidate rewrites and retain
  budget for probing, freeze and independent replay. The ledger names the canonical
  render, never a diagnostic capture or a script masquerading as an image. Qualitative
  requirements remain a separate migration gate (ADR-0012).
- Native builder inspection reconstructs the selected prior chain from an empty scene,
  evaluates the active unit frame and exposes only owned and read-only predecessor
  objects. Prepare and recheck replay-source bindings through the inspection report;
  a missing, changed or failed prior yields no successful observation. The report
  retains the complete replay inputs outside bounded model context and grants no
  acceptance authority (ADR-0012).
- The native executable builder registers its exact claim/candidate dispatch guard on
  both model and scripted canonical runtimes. Recheck before inference and before tool
  reservations, retain the requested source digest after each completed write, and
  refuse pre-existing or externally changed candidate bytes. A failed or uncertain
  write never grants a new observation or permission to adopt replacement bytes.
  SQLite records execution; existing VFX replay and completion receipts retain acceptance
  authority (ADR-0012).
- Unit-plan generation uses a fresh Flynn SQLite session under the live builder
  execution fence and exact planning claim. Publication writes only the selected unit's
  plan and integrity stamp while holding that claim. The session previews the current
  consumer gate; the outer VFX transaction independently gates and stamps terminal
  approval or rolls back only its exact owned pair. It never adopts arbitrary bytes
  after inference, resumes a spent journal, or equates a preview with acceptance.
  Session reports select the journal under `checkpoints/flynn/`; context contains the
  required unit authority and only the latest observation and images (ADR-0012).
- Native materialization tools bind one run-owned scratch candidate, its finalization
  revision, exact selected authority, validation sources, authored inputs, and declared
  reference bytes. Register their dispatch guard and supply the live attempt check;
  a changed candidate is never silently adopted. Shared VFX operations own staging,
  patching, witness creation and gate attestation. Flynn journals their observations;
  successful text never substitutes for a current finalization record, and publication
  remains the separate VFX authority transaction (ADR-0012).
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
  expiry and fail closed after it (ADR-0004). Typed durable records are a second
  durability surface that `DIGEST_SCHEMA` does not cover: an additive field on one is
  required on write and optional on read, and absence is read as a derived historical
  default only where that default is the single reading consistent with the record having
  been written at all — otherwise the record is rejected and a migration is owed. Required
  keys stay strictly required and the refusal names them. Every widening is pinned by a
  test that parses the previous key set, because a widening round-trips itself perfectly
  and breaks only the generation before it (HIR-0207). Parsing is necessary and not
  sufficient: where a record digests its own serialization, it must round-trip
  byte-identically, so an additive field is written only when it differs from the default
  the reader derives for it — otherwise a sealed record still parses and stops verifying.
  Never claim sealed work is resumable without re-verifying a real sealed artifact's own
  stored digest under the new reader; parsing it is not that check (HIR-0208). A change to what a unit or layer capsule
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
- A compiled surface states only what it can know when it is compiled. Where a rule is
  conditional on a measurement the unit has not taken, the card carries the condition and
  the bound, never a verdict: asserting `diagnostic_only` at kickoff told a builder that
  rows the evaluator would fail it on were another unit's business, three lines below the
  sharing data that existed to warn about that exact bound (HIR-0212).
- Every evidence a layer binds must be visible in the medium that layer is judged in. A
  layer judged in Workbench solid with materials suppressed can bind silhouette, extent,
  occlusion and framing — not colour, pattern, or surface. A target can be correctly owned,
  correctly measured, and still unjudgeable there; checkable and judgeable are different
  properties and a contract must survive both.
- Semantic roles use one matcher everywhere. A literal dotted selector names its exact tag
  and every dotted descendant (`building` includes `building.mass.tower`); wildcard selectors
  retain fnmatch behavior, and prefixes without a dot do not match. Aggregate rendered-subject
  evidence composes every matching surface. A single-host tool that reaches several descendants
  fails closed and enumerates legal `object=` choices (HIR-0147).
- Camera availability comes only from typed `provides: ["camera"]` authority; role names,
  including `camera.target`, never imply a capability (HIR-0098).
  A capability is originated once per layer: exactly one declarer may reach no other
  declarer of it, and every other unit declaring the same capability must contain that
  originator in its dependency closure. Declaring `provides` does not create the host, and
  two unordered declarers leave replay order to authored position, which breaks the
  accepted chain at the first cold replay (HIR-0192).
  A published gate report's signature is computed by one shared function its validator
  also calls, with each finding excerpt stripped: a fixed-width slice of prose can end in
  whitespace, and a report that fails its own validator turns a dispatchable rejection into
  an engineering route (HIR-0193).
- `inspect_scene(render/lights)` exposes the world/compositor identity, EEVEE volumetric and
  view-layer pass state, and light shape/distance settings; do not smuggle those reads through an
  idempotent `run_bpy` assignment (HIR-0055). Every `inspect_scene` call re-evaluates
  the selected or current frame and reads evaluated object, camera, and light hosts;
  omitting `frame=` never means accepting a stale depsgraph (HIR-0116).
- Node-graph introspection enumerates both input values and output socket names; an unlinked
  producer must not force a read-only mutation probe or Blender-version guess (HIR-0061).
- Framing, bbox, and visibility checks measure rendered subjects, not Light/Camera/Empty-style
  control hosts; use per-light render isolation for illumination contribution (HIR-0055).
  Those metrics measure the rendered subject: an object hidden from render contributes no
  bbox and is never counted as seen, so a hidden subject reads `visible_fraction` 0.0 and a
  builder's hide/unhide ablation actually moves the number (HIR-0196).
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
  runs; the measurement clears the streak, and an infeasible verdict proves only that no
  single rigid box can — it names a multi-part subject measured with `contract_result` or
  `cannot_express_in_scope` as the legal paths and never forces the abstention. A feasible
  verdict is never downgraded by a later infeasible one under builder-narrowed bounds, and
  framing and bbox checks on a shared role return the union the contract measures (HIR-0183,
  HIR-0184).
- A camera-providing layer that owns projected composition authors, at every judge frame it
  shares with a later layer, a persistent `bbox_*` row over that layer's reserved namespace (target from that frame's still,
  bound through `composition_context`); the kickoff compiles those framing obligations from the
  sparse DAG, the materialization validator and plan gate refuse a missing one, and after every
  mutation the camera unit reads a proxy-feasibility verdict per later layer computed from its
  frustums; an infeasible read-back asks for a path change before sealing, because a later
  geometry layer cannot move the camera (HIR-0184).
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
  versus found, and the legal next actions. Where two modules each hold half the state, the one
  that resolved the value writes the sentence: a missing-credential rejection names the dotenv
  file this process actually resolved (or that none was, and `VFXH_ENV_FILE` is the way to point
  at one), because resolution follows the imported code, not the working directory (HIR-0198). A selector miss reports both sides — what was
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
  function (HIR-0177). Every unit-local pre-write gate runs on the proposed candidate and their
  findings are refused together, numbered, with the statement that nothing was staged and the
  candidate is unchanged: a gate that returned alone made a materializer meet five rules in
  five turns of a bounded budget, and made it probe with `unstage` to learn that a refused
  stage commits nothing (HIR-0201). More generally, every stage call runs the terminal collectable
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

## Flynn runtime cutover

The Flynn/SQLite rewrite follows ADR-0012: Flynn owns permitted execution, generic
records and budgets; VFX owns permissions, evidence, replay and production authority.
The target API may break with explicit schema refusal; no backwards compatibility is
required. Flynn is the sole target runtime: remove Claude models, the Claude Agent SDK,
its subprocess/session transport, and Claude-specific configuration from the completed
cutover. Do not add an engine selector, fallback, or Claude API compatibility wrapper.
Extend Flynn when a generic execution capability is missing; VFX-specific tools and
policy stay here. SDK development and installation use its `main` branch over SSH.
Prove each migrated boundary before retiring its current writer; an SDK commit or recovery never certifies a VFX unit or authorizes session resume.
Production unit routing and explicit Flynn execution share the same eligibility predicate:
procedural construction, executable required claims covering every judge point, no provisional
visual requirement, and EEVEE when raster is owed. This does not waive a composed layer's
separate look judgment. Native image feedback carries payment handles and pixel identities;
full ownership bindings remain in the guarded registry and source-verifiable report. Its unit
evaluation receipt names the canonical primary render used by the layer checkpoint, while
the replay closure separately binds the script; a script digest cannot stand in for that
render (HIR-0253).
Production critic inference runs through Flynn, including unit, composed-layer and acceptance
callers of the shared critic. Recheck the live execution guard (or root-owner lease when no
unit/layer guard applies) and exact selected authority throughout inference. Preserve native
usage, errors, cancellation and observation report identity; never retry through Claude or
retain an earlier verdict after failed motion/focus preparation or native inference.
Only claim ids verified by native measured admission may enter qualified reconciliation.
A visual pass requires every required qualitative claim and every requested axis in the selected
scope to have that authority. An unqualified opinion is diagnostic: mark it unresolved,
refuse acceptance, and provide no autonomous repair instructions from its visual issues.
Independent executable failures retain their own authority and diagnostics. Implicit layer
look and acceptance scope cannot become qualified by copying unit labels; explicit owning
qualification selection remains a migration gate (ADR-0012).
Native critic transport records a structured observation only. Its requested model,
prompt/context/schema digests and image identities are provenance, never qualification.
Critic image labels and attachment order derive from the shared bounded image manifest.
Never describe a motion strip by a fixed ordinal, silently truncate focus panels, or
drop a declared missing motion/prior image. Native image shape v2 binds these labels
to the exact image inputs and cannot inherit qualification for the previous layout.
For model calls, it checks the exact operation's durable provider, requested model and
provider-reported response model before submitting a verdict; missing or different identity
refuses without discarding spending. Scripted observations explicitly have no model identity.
Matching reported identity does not prove model weights or establish judge qualification.
Native qualification admission is opt-in through parsed claims derived by the owning
harness from current selected authority. Each claim must bind its exact passed, hash-pinned
qualification artifact and `native_invocation_sha256`. Check the exact prompt, scope/claim
semantics, response tool/schema, image roles/format/dimensions/mode/frame count/detail and
effective provider configuration before inference; compare the configuration fingerprint
recorded from the dispatched payload before consuming the verdict. Reopen qualification
sources and check claim immutability through return. Scripted/unreported configuration,
implicit layer-look claims and synthetic debt claims cannot invent qualification.
Scope facts and claim semantics belong in required bounded context, not an unbounded SDK
state payload. A qualification-verified observation grants no state commit, retry or layer
completion; the production wrapper must still derive complete owned qualification and
retain VFX reconciliation and receipt acceptance. Offline admission fixtures do not
qualify a live model (ADR-0012).
Native calibration uses explicit `calibration_claims`, mutually exclusive with admission.
It sends the same bounded claim semantics and invocation protocol without requiring an
existing qualification artifact. Require a configuration-reporting model and compare its
durable dispatched fingerprint; preserve failed attempts and spend. Trial reports never
mark qualification verified or grant acceptance. Passing fixture trials is not a measured
qualification suite, and implicit layer/debt placeholders remain inadmissible (ADR-0012).
Native calibration evaluation reopens hash-selected reports, read-only Flynn journal
snapshots and original images. Derive decisions from recorded tool results and check the
recorded request against its claimed invocation. Require all six control types, repeated
distinct trials and an explicit irrelevant-change baseline; never treat missing samples
as zero errors. Preserve metric numerators/denominators, score instability, citation errors
and unusable-reference errors. A measurement report does not publish qualification or
select layer/debt authority (ADR-0012).
Native qualification publication takes an exact hash-selected, closed calibration suite
and a current owning-run check. Re-evaluate the suite before writing its measured artifact;
never accept supplied metrics or silently adopt a changed source selection. Publication
creates a run-owned candidate credential and does not mutate selected plans or claims.
Native admission requires its exact claim id and `calibration_proof`, reopens the suite,
evaluation, journals and images, and derives the artifact again through consumption.
Old asserted-rate artifacts without measured proof refuse native admission. Authored label
review and layer/debt qualification selection remain owning harness responsibilities
(ADR-0012).
Composed judgment debt derives typed `Claim`/`EvidenceBinding` values with no qualification
credential. Those exact semantics may enter native calibration. Explicit measured binding
returns a new claim only after reopening proof and matching the complete calibrated
semantics and suite; it cannot change the proposition, fault owner, roles, controls or
judge points. A composed group's selected debt qualifications must cover exactly its
typed debt claims, with no duplicates or scope changes. Neither that structural selection
nor a typed unqualified claim grants native admission or layer acceptance (ADR-0012).
The executable Flynn builder grants initial scene inspection at most once and only before
writing a candidate. Candidate replay supplies subsequent scene evidence. VFX transports
explicit execution phase alongside bounded selected feedback; repeated unchanged initial
inspection must not consume the unit's action budget (HIR-0248). Flynn unit context
must compile the exact attempt's complete DAG with source-verified completion authorization
and durable state, projecting only declared consumed predecessor interfaces. An active-unit
card compiled without those inputs is not dependent-unit context (HIR-0249).
After a Flynn candidate write, permit probe, owed image capture/payment or abstention.
After that probe, permit revision, freeze, image evidence work or abstention; do not re-probe
unchanged bytes and evidence. A revision, capture or payment clears the observation and must
be probed again. Enforce these phases through grants. Reserve the full remaining
write/capture/payment/probe/freeze/canonical path before offering
an action, so revision cannot consume its own required probe allowance. Transport the
existing replay-failure stage/message to the model as selected feedback; an empty verdict
list must not hide a script error. Canonical acceptance authority is unchanged (HIR-0250).
Flynn candidate repair context includes the exact current scratch source and its SHA-256,
read from the guarded candidate path, alongside selected feedback. It is required material
under the existing context cap; overflow refuses before inference. No prior source versions
or producer scripts are accumulated into this context (HIR-0251).

Use native Flynn contracts for structured tools/results, grants, lifecycle events,
cancellation, inference, and durable budgets. Preserve structured failures and observations
until the owning boundary records them. A tool refusal, execution failure, model termination,
and VFX acceptance are separate outcomes. Never hide a missing SDK feature behind prompt text.
The harness selects bounded context and feedback; the SDK must not silently accumulate history.
VFX chooses model and budgets per role, owns file/Blender mutation policy, and derives domain
success from current receipts and replay. Session or journal recovery alone cannot authorize resume.
Preserve the existing role budget policy during migration: materialization uses the larger
of the requested cap and 24 plus two turns per owned requirement, capped at 96; global
verification uses the larger of its configured cap and 6 plus two turns per declared layer,
capped at 24. Draft limits do not clamp verification. Declare budgets once and report the
counter actually enforced, not an unrelated event count (HIR-0177, HIR-0198, HIR-0199,
HIR-0228). Any conversion from legacy turns to Flynn operations must name and test the
new counting contract. Parallelize independent evidence production; serialize authoritative
scene mutation and publication.
The remaining Claude implementation is migration work, not an endorsed alternate runtime.
Cutover verification must install VFX without the Claude package and exercise planning,
materialization, building, repair, critique, and acceptance through Flynn.

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
  A layer's materialization — its finalize tool and its terminal publication gate alike —
  decides on `plan_gate.scoped_to_layer(result, layer)`: its own findings plus every
  plan-wide one. A finding another layer owns blocks that layer's transaction, never a
  session with no scope to repair it (HIR-0189).
  Ownership must also stay reachable: a build receipt is not skip authority, so `vfx run`
  skips a passed layer only while that layer's own authority still clears the gate, and it
  refuses to start when a blocking finding is owned by a layer outside the run's range,
  naming the layer to include rather than building on authority the gate rejects
  (HIR-0190).
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
  not a critic look vote (HIR-0045). That rule is quantified per unit and the composed
  canonical is not: it judges the LAYER's list against the union of its units' claims,
  so every layer judge frame must also be covered by some unit's required claim. A frame
  outside every unit's judge list satisfies the unit rule vacuously and makes the layer
  unsatisfiable by construction. The layer judge list is structural and materialization
  cannot shrink it, so the fix is a required executable claim reaching those frames on a
  unit that judges them. The materialization validator, the plan gate, and the composed
  judge decide "is this layer judged mechanically" with one predicate, so the gate cannot
  demand executable coverage of a layer a critic will decide (HIR-0238).   A look-owning unit must cover every judge
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
  write-kind witnesses (HIR-0110). A `shading` cluster is a light MODIFIER and not a
  source: it decides how a surface responds to light, and whether it emits is invisible
  when the unit is staged, since `bvfx_emission` and its siblings resolve to `shading` and
  the script does not yet exist. Sources are `light`, `volume` and `compositor`; a unit
  that is itself the light declares `provides: ["illumination"]`, which a role name or look
  label never implies. A *non-emissive* shading-over-mesh prefix with no light and no world
  renders black, and its image debt is unpayable in both directions -- a darkness bound is
  trivially met by the black adversary and a brightness bound has nothing to illuminate.
  An emissive one renders perfectly well: hansa's accepted `hero_facade.py` mixes a
  `ShaderNodeEmission` at strength 3.5 and its `frame_detail` debt read 5.135, 5.266 and
  5.14 against `>= 2`. That is the reason for the declaration and not a counterexample to
  it -- the two prefixes are indistinguishable at staging time, so the gate must be told
  which it is rather than assume either (HIR-0234). That optical-signal grant is not a rendered
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
  A sibling `object_count` is not exempt from that scan and contributes the host count it
  demands, so `eq 1` over a namespace cannot survive beside `min 12` over its descendant
  (HIR-0195).
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
  subject. That exclusion is keyed to `activates_at` and covers every row carrying
  it, not only `bbox_*` kinds: a `path_clearance_min` row deferred to layer 2 was
  evaluated at layer 2's start, read `None` because its `compare_roles` subject did not
  exist yet, and deadlocked the shot — layer 1 could not satisfy it, which is why it was
  deferred, and layer 2 could not start to build what would. Active at a layer and
  testable at that layer's start are different properties. A `None` reading stays a
  failure; the row is simply not selected before its subject can exist (HIR-0236). Parent selectors may span several truthful geometry write clusters; the first
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
  diagnostic-only (HIR-0153). Every deferred-row forecast and payment names the geometry
  producers that share its union in dependency order and the ones still pending after the
  active unit, and every forecast read-back states the measured slack left on the row's
  irreversible side: a partial producer that consumes the band leaves the pending producers
  nothing, and the payer discovers that only at the last unit (HIR-0197).
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
  itself (HIR-0150). Every authoring field whose legal values are that unit's own roles
  speaks the same relative notation and is compiled from the same namespace value —
  `control_roles` included, since its values must be drawn from `role_members`. A
  `mutates.mode` refusal likewise names every offending field with its value and both legal
  modes: `mode 'none' cannot declare mutation targets` fired on a payload whose roles,
  controls and dresses were all empty, because `script_spans` was set — a script span is a
  mutation target since it is a file the unit writes — and the materializer, reading the
  refusal against its own request, retried the identical shape on the next unit
  (HIR-0233). One record
  carries one role notation. `dresses` is the deliberate exception and stays absolute
  because it names another layer's roles (ADR-0007). A control mapped on a unit with no
  `role_members` is refused naming the consequence — a control steering no mutated role
  derives no write family — and every refusal of a role value names the accepted set, not
  only the offending token: 13 refusals across seven materializations on three shots each
  reported what was wrong and never what would have been right, and the model's guess was
  the notation the field beside it had just taught. `patch_materialization` compiles a
  `mutates` value through that same function, so what a session may stage it may patch, and
  `MutationScope.parse` refuses `role_namespace`/`role_members` rather than ignoring them —
  a durable record holds no staging-only key. Dropping them silently is what manufactured
  the published unit that mutates nothing: the patch landed, `role_members` vanished, and a
  bare control derived no write family (HIR-0217). An `interaction` claim requires the complete conditional coordination
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
  Materialization records requests, calls, results, usage and termination in its fresh
  Flynn SQLite journal; its session report selects that journal and final candidate
  identity. Optional Claude transcript/cost callbacks are not its audit surface
  (HIR-0038, ADR-0012). `publish_unit_plan` stamps the
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
  `fault_owner_units`, the replacement authority must actually change the exact owning
  capsules before publication may invalidate their closure; an unchanged owner cannot authorize
  repeated work it has no scope to repair (HIR-0049, HIR-0154, HIR-0171). The typed stop
  therefore targets the owner's layer view, and the controller dispatches that rematerialization
  (HIR-0191).
- An amendment is bounded by the finding that drove it. Resolving a joint-unsatisfiability
  finding requires giving some named row a larger admissible set; it can never require a
  smaller one, so a named row whose band shrank was not asked for and is refused at
  materialization. A repair that resolved a real contradiction also raised an unrelated
  bound's floor from 0.15 to 0.28 past a reference measuring 0.216, at the shot's
  establishing frame -- only the other row was ever wrong, and nothing defended the row
  that was right because the finding had not named it as wrong, only as party to a
  contradiction. An amendment's search space is the finding's statement, and anything true
  but unstated is free to be spent. The rule needs no measurement: only the two bands and
  the finding's `contract_ids`. A finding's `layer` is where it was raised, not the
  authority it indicts, so scope by row identity rather than by that field. An
  operator-directed rematerialization carries no finding and is bounded by the operator
  (HIR-0232).
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
- A unit in `hypothesis_falsified` is not retryable: its only legal successor is
  `superseded`, published by a reviewed `vfx plan --layer <owner> --rematerialize`
  (adding `--discard-accepted` when that layer holds passed units). The driver reports
  the durable finding as a typed authority-defect stop naming that transaction before it
  attempts the claim; letting the claim refuse instead emitted a traceback from a boundary
  holding every field the operator needed (HIR-0214).
- A stop's identity and its authority are separate. An unclassified boundary authorizes
  nothing whatever its cause, but its cause fingerprint carries the closed terminal cause,
  the exception type, and whether an operator initiated it — a constant identity made
  every such stop in every shot share one finding id, and the controller refuses a
  fingerprint already dispatched in the shot (HIR-0214).
  Its operator prose is a third question: the envelope's `found` names the exception
  it swallowed, from the same bounded label the audit beside it records, because `detail`
  is composed from `found` at every consumer and is what `status.json` and
  `reports/summary.json` carry. Keeping free-form prose out of the identity digest is
  correct and does not extend to the sentence an operator reads — 46 unclassified
  boundaries across three shots held 32 distinct causes and produced four sentences,
  including a `BUILD TRUNCATED` message the harness had already authored. The label never
  enters `classification_digest`, and `next_action` still names one action (HIR-0226).
  A stop's class is not its terminal cause and never a legal value for it: `stop_class`
  says who owns the stop, `terminal_cause` says why the run ended, and the two closed
  vocabularies share no member. `TypedStop` requires a validated cause, the vocabulary
  carries a member for every stop the harness actually publishes, and one function derives
  a plan outcome's cause for both the exception and the boundary. Assigning one to the
  other put an illegal value in 51% of all run summaries -- including HIR-0138's
  `model_session_idle_timeout`, relabelled `harness_defect` -- because a `StopEnvelope`
  carries no cause and the set had no member for a typed stop, so three sites each reached
  for the adjacent field. Author a cause with `require_terminal_cause`, which fails closed;
  `closed_terminal_cause` is for normalising observed metadata off arbitrary historical
  records and degrades an unknown value silently by design (HIR-0227).
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
  escalation and close the requirement with an explicit `approved_start`/`planner_start`
  decision referencing it. A recorded gap is what makes that decision legal for any declared
  domain, structural included: it enumerates the kinds tried and why each cannot certify.
  A requirement carrying a gap may not then be closed by contract bindings, and a decision on
  a structural requirement without one is refused naming the escalation path (HIR-0202).
  Native layer planning grants gap publication only for its selected global owned
  requirements, with the exact authored statement and bounded explanations naming
  registered evidence kinds. Unit sessions receive no such grant. Publication rechecks
  the live attempt and selected authority, atomically compares the ledger predecessor,
  and reads back the stored record; a tool observation is not plan acceptance (ADR-0012).
  Native reference measurement is limited to the selected layer/unit judge references,
  six reads, verified stills of at most 8 MiB and 16 million pixels. Each successful read
  returns the image and canonical measurements from the same byte snapshot; rereads
  include the image because bounded context may have evicted an earlier observation.
  Native spikes construct under the artifact Python policy and evaluate in a fresh
  confined Blender process with automatic Python execution disabled. Model stdout is
  diagnostic only. Four attempts per session and two failures per semantic hypothesis
  bound exploration; row reordering does not reset that budget. Scratch spike reports
  are not published planning evidence or accepted scene state (ADR-0012).
  Native materialization sessions use fresh bounded Flynn journals, retain only selected
  current feedback, and recheck the candidate and VFX finalization before returning.
  Their completion prepares a candidate; only the existing VFX authority transaction
  publishes it. A spent or uncertain invocation is never reopened as a fresh budget.
  Production materialization and rematerialization retain a live shot-wide execution
  fence across planning and publication, including controller child stages. A run path
  or claim on disk never substitutes for that lease. Each attempt seeds a distinct
  candidate and journal; failure does not overwrite a preceding attempt's scratch.
  Recorded gaps are durable shot state read through the one shared
  `vocabulary_gaps_path(shot_folder)`; materialization validation takes `shot_folder` as a
  required argument distinct from the plan-bundle `global_root`. Reading them relative to the
  immutable bundle returned no gap in every shot and every run, so the branch that lets a gap
  close a structural requirement had never executed and the refusal prescribing escalation
  could never clear — a wrong directory and an empty file are the same value unless the
  boundary keeps them apart (HIR-0218). Padding —
  vacuous bounds, self-certifying properties, invented evidence — is forbidden and rejected at
  validation (HIR-0017).

## Evidence, claims, and judgment

- Decide with the evidence hierarchy in order: deterministic scene/interface facts; executable
  image contracts; isolated render passes and focused optical evidence; atomic qualitative
  judgment; interaction judgment; human adjudication only for genuine uncertainty.
- Metric identity is explicit: one canonical registry (`vfx-harness.look-vector/v1`); producers
  and consumers call the same implementation; unknown metric ids are rejected. Never maintain
  parallel implementations of one metric (ADR-0003, HIR-0006). The same holds for any
  quantity, not only metrics: where a second derivation of one value must exist, a test pins
  it against the first, because **an unpinned second derivation is itself the defect**. Three
  landed on one day — a role notation, a turn count, a layer order — and each had both
  derivations present in the codebase with only one matching the recorded rule. The layer
  order was the only one that surfaced *as itself* rather than as a downstream symptom, and
  only because `judgment_debt_replay_authority` writes the two orders next to each other and
  asserts they match. The role notation surfaced as a unit that could execute nothing; the
  turn count surfaced only because someone read the number (HIR-0217, HIR-0221).
- Every metric kind declares the evidence domain it can certify (scene, temporal,
  projected_composition, image); a claim binds only evidence that can certify its domain. Counts
  prove existence — never timing, ordering, or appearance (HIR-0014).
- A `keyframe_schedule` or `object_property` row on a registered `data.*` path (light:
  `data.energy`, `data.size`; camera: `data.lens`, `data.angle`, `data.clip_*`, `data.sensor_*`,
  `data.ortho_scale`) binds only on a unit whose dependency closure or an earlier materialized
  layer writes that carrier family; the materialization validator and plan gate refuse the row
  otherwise, naming the family and the producers outside the closure, and an unregistered
  `data.*` path is a finding (HIR-0185).
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
  A contract lifecycle is `layer`, `window`, or `persistent`; a `window` row carries the
  `valid_through` end its domain validates, and every row-key vocabulary derives the
  lifecycle key names from that domain rather than restating them — a vocabulary naming a
  key no validator reads makes a declared lifecycle unsatisfiable and loops the
  materializer between two refusals (HIR-0188).
  Threshold operators have one enumerated field shape: `eq` uses numeric `value`
  and optional numeric `tol` (there is no `eq` field), `min` uses `lo`, `max`
  uses `hi`, and `band` uses both `lo` and `hi`; validation names the exact field
  on the first rejection (HIR-0125). Each metric declares the interval it can
  physically produce in `KIND_VALUE_RANGE`, beside its domain and camera
  capability, and a threshold lying wholly outside that interval is refused at
  authoring naming the range: a magnitude cannot carry a negative band. Only a
  range that follows from the implementation is declared, and a metric with no
  entry gets no range check rather than a guessed default — `radial_distance_trend`
  is a slope and `onset_order` a difference of frame indices, and a non-negative
  default would refuse both. The check decides disjointness only; edge-touching
  bounds remain the separate vacuity rules, which answer first and keep their own
  wording. Range knowledge restated per kind in the consumer is how two wholly
  negative bands over a magnitude cleared every gate and cost a builder $3.50 to
  disprove (HIR-0219).
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
- Autonomy and satisfaction are two questions with two answers. A row that nothing bound
  needs autonomous authority to block, which a builder-authored image check never has —
  it would be marking its own homework (HIR-0060). A consumer closing over a unit's
  required claim bindings asks only whether that exact id was produced and passed; the
  binding, not the row, supplies the authority to require it. Every such consumer — the
  unit-outcome receipt, critic reconciliation, and the sealed revalidation record — calls
  the shared `domain/evidence_authority` predicates rather than reading the autonomy flag,
  because a required `image_contract` id can only be discharged by a builder payment and
  demanding autonomy of it makes the claim unpublishable in every shot. What satisfies a
  binding live must satisfy it durably (HIR-0205). That rule binds the whole
  record-of-record path — replay point observations, evaluation and finalization receipts,
  and the builder's own `evidence_failures` producers — in both directions: a failing bound
  row is a failure whether or not it may veto unbound, and filtering it out lets a failed
  image contract read as passed. Reading the raw flag stays legal only where the row is
  genuinely unbound, and an architecture test inventories every remaining raw read so the
  next one is deliberate (HIR-0210).
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
  falsification `contract_ids` strip a `check:` prefix (HIR-0048). A multi-frame debt has one
  runtime row per frame, so anything summarising those rows is keyed by CONTRACT ID
  and not by row: the layer-revalidation drop record carries `{id, reason}` with no
  frame, so per-row appends produced duplicate ids and the projection validator
  refused to mint a receipt for a layer whose every unit had sealed. Each reason
  names the frame it was read at (HIR-0237). A runtime row counts
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
  character budget; retained-conversation roles refuse rereads. Native Flynn planning
  may reread evicted material, but each retrieval consumes one of six reads and its full
  text consumes the shared 12,000-character budget, while the three-distinct-fragment
  limit remains. Oversized bodies are refused whole, never truncated. Incremental
  full-recipe reconstruction still fails closed, so cookbook context cannot scale with
  turn count (HIR-0062, HIR-0067, HIR-0078, ADR-0012). A lighting hit is not
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
  Every model response stream also has a positive configurable event-idle deadline,
  applied by the one shared stream helper every stream iterates rather than by any single
  consumer, and the live phase writes `runs/<id>/logs/phase-heartbeat.json` with that deadline
  and its last event so a reader computes the remaining budget instead of inferring liveness
  from console or transcript mtime (HIR-0200). If no
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
  not INAPPLICABLE and not `cannot_express_in_scope` by default (HIR-0050). `bvfx_interp` scopes to the curves a unit meant through `data_paths`/`exclude_paths`;
  the unscoped call still walks the whole host closure (HIR-0074), so on a host carrying both
  motion and optics schedules the scoped form is the one that expresses a motion-only edit,
  and the protection guard names it rather than advice from another domain (HIR-0203). Once active
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

A commit contains what you staged, not what you meant. `git add -A` in a worktree that has
been used before stages whatever abandoned work is sitting in it, under your message — and a
half-wired mechanism reads as deliberate to every later reader. One such sweep put a
`require_signal` demand into a commit about drop-record keys: the demand was live, the
witness that satisfies it was supplied by no caller, and every `eevee` judgment debt in
every shot would have failed to compile. Targeted tests passed, ruff passed, and the peers
were told it was ready; only the full suite found it, 1,424 tests in. Read `git status`
before staging and stage by path unless every listed path belongs to this change. Then
verify what you committed by reading `git show --stat`, not by remembering what you edited.

A verification step whose strength depends on repository state is not a verification step.
`git stash push -- <path>` reverts only *uncommitted* changes, so the moment the work is
committed its discriminating power drops to zero and it reports success identically either
way: every test "fails without the mechanism" by passing. Revert from the parent commit
(`git checkout <parent> -- <path>`), which discriminates whatever the tree holds. That form
overwrites the working tree and stages what it wrote, so the restore
(`git checkout HEAD -- <path>`) returns the parent's bytes and the work is gone — unless
that path had nothing uncommitted to begin with. So the precondition is per-path and
checkable, not a matter of sequencing your work: **`git status --porcelain <path>` must be
empty before you revert that path.** Someone who commits three files and leaves a fourth
dirty follows "commit first" and still loses the fourth. The two forms trade off exactly —
the safe one degrades to a no-op, the reliable one eats uncommitted work — which is why the
precondition is written as something to check rather than something to remember. And it does not remove
a file the parent never had: on a change that ADDS a module, `git checkout <parent> -- <dir>`
leaves the new file in place, the tests import it happily, and every one of them "fails
without the mechanism" by passing. Reverting an added path needs the directory removed first
(`rm -rf <dir> && git checkout <parent> -- <dir>`), which is safe only under the same
per-path precondition. The general
shape is the truncated-read problem one layer up — the check and the no-op are
indistinguishable from their output, so the output is a true answer to a different question.
Three forms of it have now bitten in one day: a stash that stopped discriminating once the
work was committed, a parent-revert that ate uncommitted work, and a parent-revert that
silently skipped an added file. Prefer the state-independent form of any check whose result
you intend to rely on, and confirm the revert actually changed the tree before believing the
run.

Where an artifact exists, reasoning about a description of it is not verification. Open the
render, the receipt, the sealed script, the payload. A description is uncomparative, so any
conclusion resting on a comparison it did not make is unsupported however accurate it is —
and accuracy is exactly what makes this hard. The misleading evidence is usually *true about
something adjacent*: a prefix does show a string is absent from the prefix, a clean seal is
evidence the builder finalizer works, a discriminator that returns ABSENT did discriminate
before the file it keyed on landed in both trees. None of that is noise; it is a true answer
to a different question, which is why opening the artifact beats resolving to be careful.
Four sessions produced five instances of this in one day — truncated results read as
inventories, grep hits read as payloads, one finalizer read as another, a wait loop matching
its own pgrep pattern timed as the suite, and a stale discriminator that would have passed
silently. State only what the artifact you opened evidences: a record that carries no layer
field does not establish a layer, whoever else already believes it.

A negative result is a statement about the scope you searched, and it has to carry that
scope in the same sentence or it will be read as universal by whoever gets it next —
including you. Four in one evening across three sessions, every one a true measurement of
something nobody asked about: a `grep -r` in a live shot folder reported as "appears nowhere
in that shot, in any generation", when the id was in the archived generation the live folder
excludes by construction; an `ls | grep -c` over `docs/` reported as "carries HIR-0241,
verified by import", when three files existing is not code being present; a grep against the
wrong module returning `False`, which meant *wrong file* and was nearly reported as
*absent*; and a record read from a pinned worktree, quoted faithfully as the state of the
world, which was true only of that pin. **A record is not scope-free.** The doc said the
mechanism was owed because the doc predated the mechanism.

A correction feels like verification and is not one. Confidence rises when you are
correcting someone, and the claim you replace theirs with gets less scrutiny than the one
you would have made unprompted — both of tonight's overshoots came from a corrector, not
from an author. A loose "truncates at 150" was sharpened into a false claim about which cut
fires first, on a line whose behaviour one execution settles; and a peer's correct flag that
a record did not match the current bytes was escalated to "the id is fabricated" off a
search that had excluded the archive by construction. In both, the original was imprecise
and the correction was wrong. Run the thing you are correcting someone about.

When a model's output looks like bad judgment, check what it was actually handed before
concluding anything about the judgment. Three instances in one evening were surface defects
diagnosed as reasoning defects, and the tell was identical each time — the output was
correct for the input received. A builder that kept re-proposing thresholds after fragile
rejections had never been shown the legal window: the payment surface cut the reason at 120
characters, before the window began (HIR-0244). A critic panel that faulted a build for
reading "flat grey" was accurately describing the Workbench-solid plate it was given, on a
layer whose contract was paid in EEVEE (HIR-0241). A materializer that appeared to ignore a
rule was reading a refusal that named what was wrong and never what would have been right
(HIR-0217). Each would have been settled by one command, and none of them was.

**Truncating for display manufactures a description, and a self-authored one is exactly as
untrustworthy as any other.** A `[:160]` slice of a DAG row, a `tail -40` of a suite, a
260-character prefix of a 5,572-character refusal, a `head` of a search — each produces
output that reads like data and is not. Re-derive from the artifact when you use it, never
from your own earlier rendering of it; the truncation that was fine for looking is not fine
for reasoning, and hours later nothing distinguishes the two.

The same rule governs edits, because **shape is not identity**. A change applied by matching
a pattern cannot distinguish "this looks like the ones I am changing" from "this is one I
meant to keep". Reverting an over-broad signature edit by matching the signature *shape*
re-introduced a default into a function that was in the keep-set, and that default then
masked the one call site that never received the argument — a correction of an over-reach
becoming the defect it was correcting for. Name the functions, files or rows a change
applies to, then verify the result by parsing what you changed rather than by searching for
a token: a textual sweep for `shot_folder` matched a call that mentions it on an adjacent
line and never passes it. Where the invariant can regress, the check belongs in an
architecture test that parses, not in a sweep run once by hand.
