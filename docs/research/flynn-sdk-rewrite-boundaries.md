# Rebuilding VFX Harness on Flynn Agents SDK

Date: 2026-09-07. Status: research and migration proposal, not accepted architecture.

Reviewed checkouts:

- VFX Harness: `5977d63b76f54665ebee1f1d899c920f3c0fdb7a`.
- Flynn Agents SDK: `a8e605ec7100de5468c5bec04499758960bb3725`.
- Both worktrees were clean at the start of the original review. The baseline investigation
  changed no runtime, shot authority, SDK API, or binding rule. A subsequent review refinement
  and isolated local terminal-guard fix are described below; the broader migration remains
  proposed and requires a cross-repository boundary decision.

## Recommendation

Rebuild the model-execution layer around Flynn, and improve the VFX production controller
through bounded, independently verified migrations. Preserve the production invariants and
their tests. Reuse working domain mechanisms where appropriate; do not equate a rewrite
with discarding their accumulated evidence.

**Flynn owns how an authorized agent operation executes and is recorded. VFX Harness owns
what work is authorized, what its observations mean, and whether the shot is acceptable.**

The SDK can be based on the same architecture without absorbing the VFX application.
VFX is a useful second domain for testing Flynn's generality: a Blender mutation can be
rolled back and replayed under specific conditions, whereas a generic external action
cannot be assumed reversible. The SDK must preserve that distinction.

Do not make a Flynn step synonymous with a VFX work unit. A unit contains many diagnostic
and mutation steps, a candidate freeze, canonical replay, evaluation, and receipt-backed
publication. Neither a tool result nor an accepted SDK state revision proves that sequence.

## What exists today

The Python-source inventory counts physical lines, including comments, docstrings, and
packaged recipe spikes; it is a size measure, not a complexity or quality score.

| Checkout | Production Python | Test Python |
|---|---:|---:|
| VFX | 459 files / 152,837 lines | 273 files / 99,232 lines |
| Flynn | 10 files / 1,065 lines | 7 files / 806 lines |

VFX's largest packages are orchestration (47,445 lines), domain (30,070), agents (29,283),
and Blender (11,704). Flynn is a small execution kernel, not an implementation of that
entire lifecycle. An AST inventory found 33 direct Claude SDK import statements in 30
VFX production files, across agents, Blender tools, recipes, logging, and sandbox hooks.
Replacing one client constructor will not remove the dependency.

### VFX: substantial production control already exists

- [The unit loop](../../src/vfx_harness/agents/builder/unit_loop.py) verifies an exact
  attempt guard, restores/replays dependencies, compiles scoped context, runs live building
  and critique, finalizes a script, verifies canonical replay, repairs, and publishes outcomes.
- [The unit context compiler](../../src/vfx_harness/agents/builder/unit_context.py) derives
  active evidence, image debts, downstream framing, fault-owner options, and consumed interfaces.
  The bounded-context architecture is already partly executable, not just an aspiration.
- [Unit evaluation receipts](../../src/vfx_harness/orchestration/unit_evaluation_receipts.py)
  bind the attempt, unit digest, canonical script path, judge frames, and derived passed
  evidence. These are much stronger than a generic satisfied evaluation of one tool output.
- [Authority-state publication](../../src/vfx_harness/orchestration/authority_state_transaction.py)
  checks predecessor selection, preserved sources, and state namespaces before publishing
  a pending intent and installing the successor. This is a cross-record transaction with
  recovery, not a simple in-memory revision increment.
- [RunController](../../src/vfx_harness/application/run_controller.py) dispatches supported
  layer-view amendments from typed stops and records independently evaluated commit evidence.
  Its current automatic dispatch set contains `publish_validated_amendment`; naming other
  transaction kinds in a design document does not implement their adapters.
- [BlenderSession](../../src/vfx_harness/blender/session.py) launches a confined process,
  maintains scene state and journals, and transports checkpoint/publication bytes.
  [MCP tool assembly](../../src/vfx_harness/blender/tools/mcp.py) combines transport with
  substantial VFX policy, including image payment eligibility and black-frame diagnosis.

The valuable asset is the connected chain of scope, measurement, replay, and publication.
Moving the agent loop must preserve that chain end to end.

### Flynn: a useful first kernel with explicit limits

[Runtime.step](../../../flynn-agents-sdk/src/flynn_agents_sdk/runtime.py) currently performs:

```text
read state/latest observation → prepare request → reserve inference → propose one tool
→ enforce grants/validate → reserve tool capacity → journal intent → execute
→ journal result → evaluate exact candidate → journal evaluation → commit if satisfied
```

Implemented mechanisms include per-step grant narrowing, validation before tool dispatch,
inference/tool/external-action attempt limits, cooperative deadlines, exact candidate binding,
an in-memory compare-and-publish store, and a SQLite intent/result/evaluation journal.
Unknown dispatched effects block further journaled execution. There are no automatic retries.

Important limits verified in source:

- [Contracts](../../../flynn-agents-sdk/src/flynn_agents_sdk/contracts.py) return one
  `ToolCall` from inference; `Candidate.output` and state are strings. Images can be explicit
  request inputs, but tool output has no first-class image/artifact/effect structure.
- [InMemoryStore](../../../flynn-agents-sdk/src/flynn_agents_sdk/state.py) publishes the
  candidate's output string as the next state. It does not separate a tool observation from
  a separately proposed accepted-state update, and it is not durable.
- [SQLiteJournal](../../../flynn-agents-sdk/src/flynn_agents_sdk/journal.py) does not persist
  accepted commits, budgets, requests, or the complete runtime event stream. A recorded
  evaluation is not evidence that state committed. A returned result without evaluation is
  retained, but is not an unresolved dispatch according to `unresolved()`.
- [ContextCompiler](../../../flynn-agents-sdk/src/flynn_agents_sdk/context.py) is a standalone
  whole-item character selector. Runtime does not invoke it automatically. There is no
  required-item concept, token/image accounting, or VFX relevance policy.
- [DeepSeekAdapter](../../../flynn-agents-sdk/src/flynn_agents_sdk/deepseek.py) makes an
  explicit non-streaming request, with a fixed system instruction and thinking disabled.
  Usage is exposed through a provider-specific callback, outside runtime budget settlement.
  These are facts about this adapter, not restrictions on all possible inference adapters.
- Tools/evaluators are trusted in-process code. Flynn supplies no process confinement or
  external worker cancellation guarantee. An asyncio lock serializes one Runtime instance;
  it does not fence every actor sharing one scene or production artifact.

## Ownership map

“Split” below means one owner for the generic mechanism and one for its domain policy,
with an explicit interface. It does not mean two competing implementations.

| Concern | Flynn SDK responsibility | VFX Harness responsibility |
|---|---|---|
| Inference | Explicit request/response, provider adapters, usage and termination facts | Role prompts, model/effort selection, reference preparation |
| Agent execution | Execute an explicit bounded operation; surface typed outcomes | Choose builder/planner/critic phases and next legal operation |
| Tools | Registration, grants, validation order, dispatch and result transport | `run_bpy`, projection, inspection, render probes, scope validators |
| Permissions | Mechanically enforce caller-supplied grants; reject widening | Derive grants from unit, phase, semantic roles, controls and evidence |
| Context | Size accounting, whole-item selection, required-item refusal, provenance transport | Compile active-unit packet; choose relevant contracts, recipes, frames and feedback |
| Feedback | Preserve typed tool results, refusals, legal-option fields and references | Explain failed VFX measurements and enumerate legal semantic targets |
| Budgets | Reserve/settle operation and usage capacity; retain unknown usage | Set role/unit/run caps and decide whether another attempt is useful |
| Evidence | Generic immutable references, content identity and record transport | Which scene facts, pixels, frames, settings and sources a claim requires |
| Evaluation | Bind exact input set, evaluator identity/configuration and scoped result | Execute contracts, qualify critics, reconcile evidence and determine satisfaction |
| Commit | Generic revision/owner checks and durable commit mechanics, when implemented | Derive accepted unit/layer/shot state from source-verified domain receipts |
| Planning | Execute planning calls and enforce their output/tool contracts | Extract intent, design layers and units, compile claims and validate feasibility |
| Dependencies | A generic graph algorithm only when justified independently | Build the semantic dependency graph, activation schedule and invalidation closure |
| Recovery | Preserve uncertain effects; explicit cancellation and reconciliation protocols | Prove Blender restore/replay validity and choose the legal VFX recovery transaction |
| Isolation | Reusable process/filesystem/resource backend after independent conformance tests | Blender launch requirements, GPU needs, worker commands and semantic mutation rules |
| Memory/learning | Storage/retrieval mechanics with identities and budgets | Recipe applicability, causal hypotheses, failed experiments and quality judgments |
| Parallel work | Future resource scheduling over frozen input identities | Which evidence may run together and what ordered fan-in must establish |
| Delivery | Generic artifact transport if extracted | Full-chain acceptance, render modes, media publication and preview distinction |

Keep `Shot`, `WorkUnit`, Blender roles, camera availability, contract-frame schedules,
image payments, judgment debt, materialization, rematerialization, and `shot.json` out of
SDK core. Their names and meaning are VFX contracts, even when their underlying algorithms
are reusable.

Likewise, “replay” has two different meanings: Flynn can eventually reconstruct runtime
state from recorded events; VFX rebuilds a scene by executing deterministic artifacts in
Blender. Neither operation substitutes for the other.

## Improvements that should drive the SDK

These are proposed capabilities unless explicitly marked as reproduced behavior.

### 1. Separate operation evidence from production acceptance

Introduce an explicit result/effect record and distinguish execution success, an observation,
a candidate state update, an evaluation, and a committed revision. A render tool returning
success should retain its evidence even when a later visual claim fails. A diagnostic must
not replace the accepted script chain simply because its result was valid.

VFX should initially keep all accepted production state behind its existing writers. Any
Flynn state used within a model phase must have an explicitly narrower meaning. Avoid
dual-writing SDK accepted state and `shot.json` as competing authorities. If commit mechanics
are later extracted, one authority must remain canonical and the other must be a derived
projection with a tested crash protocol.

The replacement contract should make these independent facts representable:

| Fact | Required behavior | Owner of its meaning |
|---|---|---|
| Execution outcome | Distinguish returned, failed and interrupted execution from effect certainty | SDK execution mechanics; adapter supplies effect evidence |
| Observation | Retain returned evidence even when an assessment fails | Harness observation schema; SDK immutable transport |
| Scoped assessment | Multiple independently bound assessments may disagree without erasing observations | Harness evaluators; SDK input binding |
| State proposal | Optional explicit proposed value, base revision and supporting evidence, separate from tool output | Harness derives value; SDK checks commit preconditions |
| State commit | No proposal means no commit and no accepted-state revision increment | SDK mechanism, harness publication authority |
| Domain acceptance | A separate domain determination supported by its own evidence | Harness only |

An observation-only step must be a normal result, without a dummy state update or a contrived
failed evaluation to prevent publication. For ARC, prediction accuracy is one application-defined
assessment scope, never a mandatory SDK field. A wrong prediction can coexist with authoritative
level completion and can motivate a separately evaluated belief update. A failed evaluation of
the exact state proposal still cannot authorize that proposal's commit. For VFX, a valid diagnostic
observation can coexist with a failed appearance claim and an unchanged accepted script chain.

Tests must cover those combinations in both consumers before replacing the current state contract.

### 2. Complete the model-call contract

Return normalized content/proposals, usage, stop reason, provider identity and request identity
through a provider-neutral result. Preserve structured errors and unavailable usage. Permit
explicit role instructions and supported generation settings in the request/config contract.
Keep tool execution and retry choices outside provider adapters.

One proposal per call is a reasonable initial discipline. VFX still needs an explicit
`finish_candidate`/`cannot_express_in_scope` or equivalent typed phase-ending route; the
runtime must not infer completion from free-form text. Structured critic answers can be
transported through a schema-validated report tool without treating the report as a pass.

### 3. Make feedback and multimodal evidence first-class

Typed results should carry complete structured refusals and explicit artifact/image references.
The next request needs the relevant tool result and evaluation feedback, including internal
diagnostics, while retaining the latest scene observation separately. Today, the automatic
request contains accepted state and the latest observation-marked output; other feedback
must be added by application policy through `prepare_request`.

Feedback selection stays entirely in the harness: Flynn transports only the selected bounded
feedback and its identities. It must not append an ever-growing history automatically. Persistence
of a result does not imply inclusion in the next prompt. This applies equally to ARC experiment
feedback and VFX repair feedback.

The SDK owns transport and accounting. VFX owns crop identity, reference provenance, render
settings, and whether a particular image can pay a claim. Do not mark every diagnostic as an
environment observation merely to force it into the next request.

### 4. Strengthen bounded context without discarding required authority

Add required versus optional items and fail before inference when required items cannot fit.
Account for the complete request, tool schemas, and multimodal inputs as supported by the
provider. Character counts remain useful diagnostics but are not token or cost limits.

VFX supplies a complete active-unit packet plus optional retrieval candidates. Flynn must not
decide that a required protection contract is less relevant than a recipe because of priority.
Transport tests should assert what the model actually receives, not only the compiler output.

### 5. Enforce terminal and budget boundaries before spending

**Reproduced offline:** finish a SQLite journal, then call `Runtime.step()` with two inference
and two tool calls available. The call raises `ContractError: Episode already ended; use a
fresh journal`, with zero journal entries, but both remaining counters are now **1**. Inference
has already run; tool execution has not. Runtime checks unresolved dispatches before inference,
but the terminal check occurs in `Journal.begin`, after both reservations.

Owner: SDK. Add a lifecycle check before inference/reservation, retaining the transactional
check before dispatch. For concurrent writers, define a run/episode ownership protocol; an
early read by itself is not a concurrency guarantee. The regression test must assert zero
adapter calls and unchanged budgets on an already-terminal episode.

Follow-up implementation: the local SDK change adds `outcome()` to the Journal protocol and
checks it before preparing requests or reserving capacity. SQLiteJournal already provides that
method. Regression tests cover open-connection and reopened terminal journals; a separate test
preserves the existing dispatch refusal when the journal finishes during inference. This does
not introduce distributed episode ownership or refund already-started inference.

More broadly, add durable reservations and settlement before advertising restart-safe budgets.
Do not reset spend by constructing a new in-memory Budget. Keep reservation semantics clear
for provider failures whose actual usage cannot be obtained.

### 6. Treat durability and worker cancellation as separate contracts

Specify a full lifecycle: request/reservation, dispatched intent, returned result, evaluation,
commit intent/commit and terminal state. Test death between each pair. Recovering an evaluated
record must not invent a missing commit or silently repeat an external mutation.

Blender requires an acknowledged worker outcome, verified rollback, or a quarantined unknown
effect. Existing tools offload synchronous calls to threads; `BlenderSession.call()` waits for
responses without a per-command timeout. Wrapping that in an asyncio deadline alone cannot
prove the worker stopped. This is an integration risk requiring fault injection, not a claim
that a real run hung during this review.

Keep VFX's proven confinement in place while a generic backend is designed. Extract only the
process/resource mechanism after non-Blender negative tests, including denial of evaluator files,
credentials, undeclared paths and network access. Do not move Blender semantics into Flynn to
make the backend appear general.

## Harness improvements to pursue alongside migration

1. **Create provider-independent VFX tool services.** Separate decorators/MCP envelopes from
   domain validators, scene transactions, read-back and artifact capture. Flynn registers the
   resulting services directly. A permanent generic MCP client is not required for in-process
   services just because the old SDK used MCP.
2. **Split unit phase coordination from inference sessions.** The current unit loop mixes
   lifecycle, client history, critique policy, artifacts and publication. Express live build,
   freeze, finalization, canonical evaluation and repair as explicit domain phases. Each phase
   derives its own tools and context from current authority.
3. **Preserve authoritative read-back.** The architecture already addresses missing measurements
   through projection, feasibility, semantic introspection and controlled probes. Make these
   results reliably reachable through the new transport before tuning model prompts.
4. **Audit context failure handling.** `compile_unit_build_context` catches several failures while
   assembling protected/active evidence. The rewrite should distinguish missing optional
   diagnostics from inability to derive required authority. Source inspection identifies this
   audit target; this review does not prove that it permits an invalid published unit.
5. **Make evidence compatibility explicit early.** Frames, medium, reference bytes, settings,
   candidate and dependency identities belong in the compiled evidence schedule. HIR-0241 shows
   why an EEVEE-calibrated claim cannot be rechecked on a solid plate. Its later section and
   current tests implement grouping; the earlier refusal in that record is not the final behavior.
6. **Preserve actionable rejection content.** HIR-0244 traced apparently poor reasoning to a
   tool response cutting off the legal threshold window. The SDK transports structured feedback;
   VFX generates the complete domain explanation. Test the final model-visible payload.
7. **Keep acceptance separate at every level.** Tool returned, candidate evaluated, unit sealed,
   layer composed and shot accepted must remain distinct. Failed/deferred/superseded outcomes
   must not collapse into a generic retry loop.

## Migration sequence and exit evidence

Sequence after review: **terminal guard → explicit result/state semantics → structured selected
feedback and required context → one scripted VFX unit retaining existing acceptance boundaries**.
Provider transport improvements follow concrete needs demonstrated by these consumer slices.

**Every shared SDK contract change has an ARC gate from the beginning.** Run SDK checks, push
the authorized revision, install it into ARC through the existing SSH workflow, verify the installed
commit against the lock, then run all offline ARC tests and relevant replay assertions. A stage
does not pass this gate on the strength of SDK tests, import inspection, or an editable checkout.
Inspect `scripts/sync_sdk.sh` output against the intended commit, not only against a matching lock.
An unpushed change is locally validated work with this integration gate pending.

| Stage | SDK work | VFX work | Exit evidence |
|---|---|---|---|
| 0: establish authority | Terminal guard; adopt a VFX-neutral ownership contract and gap ledger | Reconcile active rules/docs; inventory Claude-dependent behavior and baseline fixtures | Zero terminal inference/spend; ARC SSH compatibility gate; each behavior has one owner and preservation test |
| 1: semantics and first consumer slice | Explicit independent result/state semantics, selected feedback, required context and phase-ending surface | Run one executable-only unit with scripted inference through public Flynn tools, retaining VFX receipt writers | ARC gate after each shared change; inspect → mutate → read-back → freeze → cold replay → receipt; unauthorized edit and false finish rejected |
| 2: model execution parity | Provider-neutral usage/errors, role configuration, request/step budgets and multimodal result transport | Port critic/finalizer/builder phase surfaces and complete context delivery | Captured requests prove exact grants and feedback; provider/idle/budget failures cannot reach publication |
| 3: crash boundaries | Add only lifecycle/budget or worker primitives justified by concrete shared requirements | Preserve existing claims, fences, checkpoints and terminalizers; adapt only proven interfaces | ARC gate and crash matrix prove no duplicate unknown mutation, no invented commit and no renewed budget |
| 4: small production chain | Improve only abstractions this slice demonstrates | Two dependent units plus an independent branch; mixed-medium evidence; one legal amendment | Preserve unaffected work, supersede changed closure, reject stale evidence, compose and accept from empty |
| 5: replace all roles | Stabilize public API and installation provenance | Migrate global planning, materialization, unit planning, build, critique, repair, acceptance and remaining SDK-bound utilities | Production has no Claude Agent SDK imports/dependency; complete regression suite passes |
| 6: prove value | SDK invariant suite and non-VFX consumer compatibility | Fresh heterogeneous shots and held-out injected failures | Equal-budget comparisons report quality, completion, cost, latency, context growth and every failure |

The first slice needs a confined real Blender run after strict preflight, in addition to fast
offline tests. It should start with executable scene claims so transport and publication
defects can be distinguished from subjective judgment. The two-unit follow-up must include
composition and full-chain acceptance; stopping at a successful tool call would miss the
hardest integration boundaries.

Keep the existing runtime usable while proving slices in isolated fixtures. A migration adapter
is acceptable only as an explicit temporary development boundary with an exit criterion;
do not ship an indefinite dual-runtime compatibility layer or silently switch providers after
failure. Retirement must include old imports, hooks, configuration and implicit session behavior.

Broad scheduling, memory systems, confinement and durable commit infrastructure are explicitly
deferred extractions. A roadmap stage is not authorization to build a generic subsystem. Require
a concrete shared requirement and a tested interface first; preserve VFX's existing mechanisms
meanwhile. Recorded SDK lifecycle gaps remain visible limitations rather than promises of an
immediate general-purpose recovery system.

Each split improvement needs: observed failure, earliest owner, interface change, invariant test,
consumer test, and measurable success criterion. Land SDK contracts before their consumer pin.
Flynn's existing workflow requires syncing the adjacent ARC consumer after pushed SDK changes.
The follow-up terminal guard is local and unpushed; ARC SSH installation and compatibility
validation remain pending. The inspected ARC consumers use SQLiteJournal directly, but source
inspection is not evidence that the modified SDK has been installed or passed ARC tests.

VFX declares Python >=3.10; Flynn requires >=3.11 and uses 3.11 features. The integration must
raise VFX's supported minimum and test it. Pin the tested SDK revision/package rather than
relying on a sibling editable checkout. VFX's inspected tracked files contain no dependency
lockfile; a reproducible installation decision is part of this migration.

## Documentation conflicts to resolve before implementation

- VFX README says no automatic recovery controller exists. `run_shot.py` instantiates
  `RunController`, and controller tests execute its dispatch paths. AGENTS.md describes the
  receipt-backed controller. Update the operational description to match the verified scope.
- VFX AGENTS.md prefers Claude SDK-native orchestration. The user's Flynn rewrite direction
  supersedes that choice for this migration; explicitly replace the standing rule and its
  mirrors when adopting the new architecture. Do not silently preserve a hidden Claude loop.
- ADR-0001 describes AGENTS.md as a concise routing index, while current AGENTS.md explicitly
  makes itself the complete binding rule set and has 1,653 lines. Resolve this authority
  documentation conflict; do not use it as permission to discard invariants.
- Flynn ROADMAP still describes an editable consumer dependency, while README/AGENTS require
  a pushed SSH-pinned SDK and consumer sync. Update status descriptions from installation evidence.
- Both repositories label broad architecture documents as mixed implementation/target design.
  Preserve that distinction. For example, full durable recovery and generic confinement are
  still absent from the inspected Flynn production modules despite appearing in design sections.

These conflicts are recorded rather than adjudicated into new binding rules by this research
note. They do not prevent code inspection or offline characterization, but should be reconciled
in the architecture decision before behavioral migration.

## Validation performed and limits

Original review baseline:

- Flynn: `.venv/bin/python -m pytest -q` — **67 passed in 3.93s**.
- Flynn: `.venv/bin/ruff check src tests examples scripts` — **All checks passed!**
- Flynn: `.venv/bin/mypy` — **Success: no issues found in 11 source files**.
- VFX: `.venv/bin/ruff check src` — **All checks passed!**
- VFX: `.venv/bin/python -m pytest -q src/tests/architecture` — **133 passed in 87.99s**.
- VFX: controller, run-shot controller, composed-medium and complete-rejection test files —
  **31 passed in 6.17s** across `test_run_controller.py`, `test_run_shot_controller.py`,
  `test_composed_group_medium.py`, and `test_rejection_reaches_the_builder_whole.py`.
- Additional temporary scripted probe reproduced the finished-journal budget consumption
  described above. It executed no external action and made no network request.

This is a source-level architecture investigation with offline characterization. It is not an
exhaustive review of every VFX module. The full VFX suite, real Blender execution, strict
preflight, paid inference, ARC consumer sync and cross-runtime quality benchmarks were not run.
Passing the selected tests establishes those checks only, not rewrite readiness or improved
shot quality. No production artifacts or historical shot receipts were re-verified in this review.

Follow-up terminal-guard validation:

- Before the production fix, the two terminal-entry cases failed because inference executed;
  the in-flight terminal refusal test passed: **2 failed, 1 passed, 9 deselected**.
- After the fix, the full SDK suite reports **70 passed in 0.92s**.
- Ruff checks passed; formatting reports **19 files already formatted**; mypy reports
  **Success: no issues found in 11 source files**.
- The scripted example reports **Accepted: True; revision: 1; State: 5**.
- No SDK push, ARC installation/lock update, ARC test run, or live inference was performed.

## Original proposed next decision

Complete the terminal guard's ARC SSH compatibility gate, then adopt the ownership map as a
cross-repository ADR. Specify and test independent result/state semantics, selected feedback
and required context with ARC compatibility at each step. Next, implement the smallest scripted
VFX unit through Flynn with the existing canonical replay and receipt boundary intact. Keep
generic execution defects in Flynn and VFX reasoning, instruments, planning and acceptance
improvements in the harness.

## Implementation follow-up: 2026-09-07

The owner adopted SQLite and explicitly removed backwards compatibility as a requirement.
[ADR-0012](../decisions/ADR-0012-flynn-sqlite-runtime.md) records the accepted ownership
direction, schema policy, implementation sequence and current validation limits. The
source descriptions above are the investigation baseline, including links to SDK modules
that the breaking rewrite has now removed. They are not current API documentation.

The separate development branches are `codex/sqlite-runtime` and
`codex/flynn-sqlite-migration`. The initial SDK tranche adds SQLite control state, durable
operation reservations, atomic evaluation/state publication, explicit observation-only
work, conservative recovery, required context and prepared-schema preservation. The VFX
branch adopts the decision; production VFX execution has not been switched to Flynn.
