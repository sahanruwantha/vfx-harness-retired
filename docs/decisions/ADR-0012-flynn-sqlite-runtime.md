---
id: ADR-0012
title: Flynn owns execution durability; VFX owns production authority
status: accepted
date: 2026-09-07
supersedes: null
---

# Flynn owns execution durability; VFX owns production authority

## Context

The owner requested a VFX rewrite using Flynn, improvements in both repositories,
SQLite journaling, and a clean break without backwards compatibility. The
[repository investigation](../research/flynn-sdk-rewrite-boundaries.md) records the
starting architecture and the evidence behind the separation.

Flynn previously separated an in-memory state store and budgets from an optional
SQLite journal. A durable evaluation did not prove a state commit, reopening could
not restore spend, and satisfied tool output became state implicitly. These are
execution defects shared by VFX and ARC. VFX's work-unit acceptance, canonical replay,
claims, plans, scene ownership and publication rules are separate domain contracts.

## Decision

Adopt Flynn as the target execution kernel. Use SQLite for execution control state
and journaling, and design VFX's eventual control-state replacement around explicit
domain transactions. Do not treat this decision as evidence that the production
controller has already migrated.

| Concern | Owner |
|---|---|
| Operation identities, permitted dispatch, reservations, results, evaluation transport and recorded state revisions | Flynn |
| Tool registration, allowed roles/controls, policy changes, context relevance and selected feedback | VFX |
| Required-context fit and reliable transport of selected inputs | Flynn |
| Blender confinement, instruments, read-back, checkpoints and cumulative empty-scene replay | VFX |
| Plan/unit identity, authority generations, fault ownership, acceptance and receipt verification | VFX |
| Domain completion and whether external recovery is safe | VFX |

An SDK operation is not a VFX unit or ARC level. Successful execution, valid observations,
prediction judgments, proposed state changes and domain acceptance must remain distinguishable.
Flynn must support observation-only work without an artificial state commit. Its database
transaction can bind a recorded evaluation and state revision; it cannot certify a Blender
scene, an external artifact, or a VFX receipt. VFX owns the transaction that promotes its
domain evidence into authority.

Keep large scripts, renders and checkpoints external, referenced by verified identities.
Never claim a SQLite commit is atomic with a filesystem publication. Preserve the current
VFX publishers and independent readers until their replacement proves the same obligations.
During development, each authority surface has exactly one writer and one selected generation;
there is no production dual-write or fallback authority. JSON exports from a future SQLite
authority are projections, not an independently editable source of truth.

The target API and schema may break. Remove superseded SDK interfaces; reject unsupported
records explicitly. Existing accepted VFX artifacts remain untouched historical evidence.
Any later VFX digest/schema cutover must name the new generation and either migrate it through
an explicit verified transaction or refuse it. No heuristics and no indefinite compatibility
layer. The general preference for Claude-native orchestration does not govern this migration;
the existing Claude path retains its constraints until replaced.

SQLite ownership is local to one run and one live owner. It does not replace the shot-wide
authority fence. A missing external result remains indeterminate after restart. An explicit
SDK operation recovery is not permission for automatic VFX model-session resume.

## Implementation sequence and gates

1. **Execution foundation.** Replace Flynn's split stores with SQLite requests, operation
   reservations, dispatch/result records, evaluations and optional state commits. Prove
   terminal refusal before spend, same-process and subprocess ownership, and process-death
   behavior before/after dispatch and inside publication.
2. **Shared semantic contracts.** Complete independent assessments, structured selected
   feedback, required context, provider-neutral usage and phase termination. Migrate ARC's
   consumers explicitly. Install the exact pushed SDK commit over SSH and pass ARC's offline
   tests and replay assertions; backwards API compatibility is not the gate.
3. **One VFX unit.** After strict preflight, run a scripted executable-only unit through
   inspect, permitted mutation, read-back, freeze, confined cold replay and the existing
   receipt writer. Inject an unauthorized mutation, stale authority, false finish, replay
   failure and interruption. A diagnostic success alone is insufficient evidence.
4. **Dependent production work.** Exercise two dependent units and an independent branch,
   composition, mixed evidence media and one receipt-backed amendment. Preserve unaffected
   receipts and refuse stale evidence. Prove complete acceptance from an empty scene.
5. **Role and control-state cutover.** Replace planning, materialization, building, critique,
   repair and acceptance sessions; replace domain control-state publishers only with verified
   domain transactions. Remove the Claude dependency, hooks, old transports and obsolete
   configuration. Run full regression and architecture suites.
6. **Capability evaluation.** Compare heterogeneous held-out fixtures at equal budgets,
   reporting completion, quality, latency, context growth, spend and all failures. A new
   database alone is not evidence of smarter experiment selection or better VFX.

Each pushed shared SDK change must pass the ARC SSH installation gate before it is reported
complete. Extract scheduling, memory or confinement only when a concrete shared requirement
establishes the interface. Avoid moving VFX classes into Flynn merely to reduce harness size.

## Current evidence

Development branches are `codex/sqlite-runtime` in Flynn and
`codex/flynn-sqlite-migration` in VFX. The initial SDK implementation removes `Budget`,
`InMemoryStore` and `SQLiteJournal`, introduces `SQLiteRun` schema 2, and adds required
context and preservation of prepared tool schemas. Its offline suite currently has 88 tests.

Local checkpoint: SDK commit `f6b164efa03efe5472334983c7c4a740823d65fc` passes
88 SDK tests, Ruff, formatting, mypy, release metadata validation and the scripted reopen
example. Its wheel and source distribution build; the installed wheel passes all 86
unit/integration tests outside the checkout. VFX's complete source Ruff check and its 133
architecture tests pass. The full VFX behavior suite was not run for this documentation
and rule-scope change.

The initial commits were pushed on 2026-09-07. ARC's toy, visual and official consumers
now use schema 2 and explicitly treat prediction/observation assessments as observation-only
work. Official RESET runs through a scripted Flynn operation, so its reservation counts a
scripted inference-adapter invocation rather than a model request. Replay reads dispatched
schema-2 operations and rejects old generations. A small ARC reporting projection derives
execution counts from tool reservations rather than counting rejected inference proposals.

The required sync command in the real ARC checkout installed exactly
`f6b164efa03efe5472334983c7c4a740823d65fc` over SSH and pinned it in `uv.lock`.
All 82 ARC tests, Ruff and mypy passed. This includes deterministic replay equality,
restored operation budgets, terminal refusal and old-schema refusal. ARC already had substantial
uncommitted work; the migration was prepared in an isolated copy, tested, reviewed as an exact
12-file patch, and applied only after checking every original file hash. Preimages remain at
`/tmp/arc-sqlite-apply/before`. Its unrelated changes were not staged or committed.

VFX now declares the same exact Git dependency in its explicit `flynn` development extra
and a Python 3.11 minimum. VFX CI currently has no repository secrets; the extra avoids
breaking its existing credential-free installation. Migration tests explicitly skip when
the extra is absent. Both local migration environments install it from SSH; production
cutover must configure CI authentication and make this gate mandatory. Four offline
contract tests exercise the real VFX WorkUnit/scope compiler through Flynn: required context
cannot be omitted, an ungranted mutation is rejected, and scope evidence survives reopening
without becoming a unit acceptance or state commit. These tests do not execute Blender.

Strict `vfx preflight --strict` passed on this host, including a real confined Blender
boot, kernel fences and the plan-consumer directory primitive. Two further integration
tests dispatch a frozen fixture program through Flynn into the existing confined VFX
worker, measure it with VFX's scene-contract evaluator, and compare the observation with
a fresh worker's empty-scene replay through `_run_artifact_script`. One fixture satisfies
its declared count; the other deliberately fails it. Both preserve observation/evaluation
without accepting a unit. The first attempt correctly rejected an overbroad scene reset
inside the artifact; the corrected fixture leaves reset with the harness-owned setup.

Those initial six tests covered execution/context seams without publishing unit receipts.
No paid model calls were made.

Final follow-up validation: the complete current VFX collection ran in four isolated
pytest processes with separate temporary directories: **586 + 717 + 822 + 775 = 2,900
passed**. This includes all six Flynn seam tests. Ruff passed on the complete `src` tree.
The suite reported 15 existing Pillow `getdata()` deprecation warnings. The parallel run
replaced an interrupted serial attempt; no partial run is counted as full-suite evidence.

### Executable unit integration

`agents/builder/flynn_unit.py` now supplies an explicit unit executor to the existing
layer controller through `unit_builder`. The controller continues to own dependency
selection, planning claims, checkpoint freeze, completion, failure dispatch and layer
finalization. Selecting Flynn is explicit; an unsupported raster or non-procedural unit
raises instead of falling back to Claude. Normal CLI runs still select the existing engine.

The Flynn engine binds its database to the exact run/attempt under `checkpoints/flynn/`.
It uses the current attempt guard, authority snapshot, scope compiler, confined artifact
replay, evaluation receipt publisher and completion readers. Model grants change with the
phase: a candidate must be written before probing, and observed before freezing. The model
has no acceptance tool. An explicit insufficient-evidence abstention records its reason and
ends execution without inventing unit failure, plan-defect or completion authority.
Freeze records the observed script digest; a separate scripted Flynn
dispatch performs canonical replay, using one reserved inference-adapter invocation, tool
dispatch and external-action allowance. That scripted invocation makes no model request.
The existing script publisher checks the staged bytes against the frozen digest before
publication. SQLite records the resulting evaluation receipt identity without committing
tool output as accepted state or claiming that the unit checkpoint has completed.
As in the current builder, a failed canonical evaluation publishes its frozen script for
diagnosis and binds the failure ledger to those exact bytes; it earns no completion receipt.

Full results stay in SQLite. VFX selects the latest measured feedback, preserving its source
digest, and checks that the selected feedback, objective, exact scope and unit plan fit the
context cap together. It does not accumulate prior turns or silently drop required material.
Reopening SQLite is diagnostic only: the engine refuses re-entry into the same attempt and
does not offer session recovery or a budget reset.

The lifecycle fixtures seed selected planning authority through the existing test
materialization helper; they do not prove planning quality or run a paid planner. From that
authority onward, attempt claims, confined Blender replay, unit evaluation, checkpointing
and completion are real. Failure cases cover unmet evidence, undeclared roles, script
execution errors, false finish, revoked claims, interruption and frozen-source substitution.
The completion reader must also reject later script drift despite SQLite's terminal record.

This remains an opt-in scripted migration route. Provider usage/USD settlement, live model
adapters, visual judgment, heterogeneous dependent composition and public CLI selection
remain separate gates. The current layer and shot acceptance authorities are retained.

Validation on the final unchanged source: **653 + 757 + 697 + 804 = 2,911 passed**
in four isolated pytest processes. This includes nine Flynn lifecycle cases, the
frozen-script substitution regression and the explicit executor/fence routing test.
Ruff passed on all `src`, and the public `vfx --help` command passed. The suite emitted
15 existing Pillow deprecation warnings. The earlier full run is not counted as passing:
one fixture assertion was corrected, and a sealed-outcome test correctly detected a source
edit during that run. The complete rerun held source bytes unchanged and passed every test.
The Flynn dependency remained the non-editable SSH installation of `f6b164e`; later SDK
provider-diagnostic commits are outside this validation. No paid inference was used.

### First live executable unit

[HIR-0248](../improvements/HIR-0248-flynn-inspection-phase.md) records the first
DeepSeek probe failure and the VFX phase-policy fix. A fresh live probe then completed
inspect/write/probe/freeze in four model calls, passed independent confined canonical
replay, and verified native checkpoint/completion. Planning authority was seeded by a
test fixture. The normal CLI, visual roles and dependent composition remain unmigrated.
The SDK dependency is unchanged; 40 focused VFX regressions and complete-source Ruff pass.

### Dependent executable layer

[HIR-0249](../improvements/HIR-0249-flynn-predecessor-context.md) records a missing
predecessor-context defect exposed by the three-unit layer gate. The Flynn adapter now
supplies the exact attempt DAG, durable state and source-verified completion authorization
to the existing context compiler. The scripted gate exercises an independent unit,
a producer and its consumer through native checkpointing and composed layer finalization.
It also verifies failed-consumer preservation and refusal before inference on stale
producer bytes. These are executable-only fixtures with seeded planning attestations;
visual media, receipt-backed amendments and full-shot acceptance remain separate gates.
The approved live DeepSeek follow-up used 10 model calls. The independent unit earned
a verified completion receipt, but the producer rewrote three candidates without probing
and then abstained at its action budget. The consumer did not run and no composed layer
was published. HIR-0249 records this remaining VFX phase-policy gap; live dependent
composition remains unproven.
The complete unchanged-source suite passed **2,914 tests**, plus complete-source Ruff,
CLI help and strict preflight. The SDK source and pinned dependency are unchanged.

### Measured candidate revision

[HIR-0250](../improvements/HIR-0250-flynn-candidate-observation-phase.md) enforces
write/probe/revise-or-freeze phases, reserves the full remaining path, and transports
actual replay failures to the model. All 58 focused regressions pass. In the final live
retry, the independent unit repaired a script from observed error feedback and completed;
the producer completed too. The consumer passed its probe but the turn's model-call cap
stopped before freeze. These runs spent 18 calls total and published no composed layer.
A fresh live layer run is still required; no automatic session resume is authorized.

### Live dependent-layer completion

[HIR-0251](../improvements/HIR-0251-flynn-current-candidate-context.md) closes a
remaining repair-context gap by including exact current candidate source and SHA-256
under the existing required-context cap. All 59 focused regressions passed. A fresh
DeepSeek run completed the independent unit, producer and consumer in 14 model calls,
including one measured repair. Native empty-scene composition passed all three scene
contracts at both judge points, and a separate process verified the published layer
receipt. Planning remains seeded fixture authority; visual roles, amendments, full-shot
acceptance and the CLI cutover remain separate gates. The SDK is unchanged.

## Consequences

SDK durability defects can be fixed once for both harnesses. Domain acceptance remains
independently testable. The tradeoff is an explicit consumer migration and a measured staged
cutover rather than treating existing orchestration as a mechanical import replacement.

SQLite reduces cross-record publication gaps, but its hardware/filesystem assumptions still
apply; see the [SQLite atomic commit documentation](https://www.sqlite.org/atomiccommit.html).
The first implementation uses local rollback journaling with full synchronization. WAL mode
is a later measured concurrency choice, not a prerequisite for SQLite-based journaling.

### Provider-neutral usage records

[The accounting integration](../research/flynn-usage-accounting.md) adds SDK schema-3
usage records and a VFX attempt-scoped audit projection. Schema-2 journals remain
readable for historical inspection only; execution still refuses them. The VFX
SDK dependency names `codex/sqlite-runtime` directly, following the owner's explicit
branch-installation preference. This does not change VFX acceptance or resume authority.

### Optional model-output budgets

[Output-token admission](../research/flynn-output-token-budget.md) adds schema-4
reservations before inference. The harness selects the cap; the SDK preserves
unknown usage and enforces future admission. Scripted canonical replay reserves
zero output tokens but retains every existing VFX authority check. Input usage
remains accounting only until an adapter supplies a verified pre-request bound.


## Production global planning

Global draft, verify and repair now use Flynn directly. The root owner runs this stage
in process so the native dispatch guard can verify its actual process-bound lease;
run-directory metadata is not a substitute capability. Layer process isolation remains
separate. Required phase context and immutable draft snapshots belong to VFX, as do the
independent terminal gate and selected-plan publisher. JIT planning and other remaining
roles retain their existing implementations until their native boundaries migrate.

The gate now enforces declared client blockers from the compiler's preserved ownership
mapping (HIR-0252). This repairs VFX evidence interpretation; no SDK mechanism derives
client intent or plan acceptance.

### Native materialization capabilities

The six layer-candidate operations now share provider-neutral VFX handlers and schemas:
stage, unstage, patch, status, crop-witness creation, and finalization. The remaining
Claude JIT session has only a transport wrapper around those handlers. Native Flynn
registration invokes the shared operations directly, validates their closed schemas,
and checks the exact live attempt, candidate/finalization bytes, authority generation,
validation inputs and references before dispatch. Candidate mutations additionally
check the live attempt at the existing guarded write boundary.

Each native observation names the before/after byte identities and a complete hashed
run report. Feedback text and structured detail are bounded, with omissions explicit.
A claimed clean result earns `finalization_current` only through the existing durable
reader. No native tool selects authority or commits unit state. This capability change
does not switch the remaining JIT model session or remove its other tools.

### Native planning knowledge and questions

Evidence descriptions and recipe discovery now have shared VFX implementations outside
model transports. Native JIT knowledge registration binds the selected layer or unit,
current authority generation, authored/decision inputs and a caller-owned live attempt
check. Evidence vocabulary is an index plus exact-kind detail; recipe discovery retains
the existing section format and filters by actual mutation roles for a selected unit.
Layer-level discovery grants no mutation permission. Read observations name their scope
and content identities, without committing domain state.

Native recipe retrieval allows rereads because selected observations may have left
bounded context. Six reads, three distinct fragments and 12,000 cumulative response
characters prevent unbounded reconstruction. Retained-conversation roles keep their
existing duplicate-read refusal. Global and JIT native supervisor questions share the
same prepared VFX publication path and read back the actual stored question; neither
assumptions nor successful tool calls become answers or plan approval.

Native layer planning also exposes vocabulary-gap publication. VFX derives the allowed
requirement ids from selected global ownership, requires the authored statement and
registered attempted kinds, bounds explanations, and serializes the durable update.
Flynn transports the stored record and ledger digest as an external observation. It does
not certify that the vocabulary is insufficient, select a provisional decision, or
commit accepted state. The existing shot-wide gap semantics and independent VFX decision
evaluation remain unchanged. Unit sessions have no gap-publication grant.

Native reference measurement is likewise VFX-owned: the selected layer or unit determines
which judge references are measurable, the canonical metric registry computes values,
and the response binds image and fingerprint to one verified byte snapshot. Each of six
permitted reads includes the image even when repeated; a previous observation is not
proof that bounded context still contains it. Flynn transports the multimodal result and
counts the tool call without committing domain state or charging an external action.

Native spike execution keeps VFX eligibility, hypothesis budgets, confinement, scene
contracts and evidence publication outside the SDK. Flynn records one external operation
and its structured observation. Construction and measurement run in separate confined
Blender processes so script stdout cannot become a measurement channel. Reports bind
current planning authority and exact scratch artifacts; explicit planning-evidence
publication remains a separate VFX transaction, not a consequence of tool success.

Native materialization session policy also belongs to VFX. The session combines the
existing materialization and exploration capabilities under one exact candidate check,
supplies bounded required context plus the latest selected observation, and stops only
after reopening current VFX finalization. Flynn owns the fresh journal, dispatch guards,
usage, limits and terminal execution record. The session never calls the authority
publisher, and a spent or uncertain invocation cannot restart its budget. Diagnostic
spikes need no promotion into contract evidence for this session: typed materialization
validation and finalization, followed by the existing publication transaction, remain
the acceptance path.

Production layer materialization now uses that session through a DeepSeek adapter.
VFX owns provider configuration and rejects unpriced USD caps. Its live ownership is
the existing shot-wide execution fence, retained from the public planning entry point
through separate authority publication. This supports controller child stages without
pretending an inherited run path is the root owner's process capability. Unit planning
reuses its already-held builder lease; standalone materialization acquires its own.
Fresh candidate paths preserve failed attempts, and SQLite request/result/usage records
replace the old materialization-only Claude transcript and cost callbacks. Known
inference failures and exhausted budgets compile the existing VFX typed stop; unexpected
errors and lost ownership are not converted into domain success or retried automatically.

## Native unit planning

Production unit-plan generation uses Flynn with a fresh bounded SQLite session under
VFX's existing builder fence and exact planning claim. Native content publication is
held inside the claim guard and owns only the schema-selected target and integrity
stamp. It previews the independent consumer gate, with at most three evaluations.
The outer planning transaction reruns the terminal gate and decides attestation or
rollback. A clean preview remains an observation, never an SDK state commit or VFX
acceptance. Model responses no longer make the transaction adopt whatever bytes are
present at session exit. The selected unit's knowledge and reference tools, confined
spikes, required authority context and latest feedback remain bounded by this session.

## Executable builder dispatch ownership

The native executable unit engine registers one claim/candidate guard on both its
model runtime and its scripted canonical runtime. The same check precedes request
preparation and ledger startup. A claim revoked during inference refuses dispatch
before tool or external reservations, while inference usage remains recorded.
Candidate ownership starts absent and advances only to the digest of source this
attempt requested and successfully wrote. Pre-existing, changed, missing or linked
candidate bytes refuse continuation; neither replay nor a subsequent write silently
adopts them. The final publisher still checks the exact frozen bytes and independent
VFX evaluation. This uses Flynn's existing DispatchGuard contract; it introduces no
new SDK acceptance semantics and does not enable raster or generated-asset units.

## Native builder inspection source

`inspect_unit` reconstructs the selected prefix with the same prepared-input,
empty-scene setup and artifact replay helpers used by canonical verification. It
sets the active unit's frame before the evaluation barrier and scoped object read.
Prepared script and construction dependencies must still match after replay and
report publication. The model receives owned objects, read-only predecessor objects,
a prefix count and the inspection report identity; the complete ordered input
identities stay in that run report. A leftover Blender scene, successful prose or an
inspection report cannot establish unit acceptance. This is a VFX instrument built
on Flynn's existing tool, guard and journal contracts.

## Production executable-unit routing

The public builder facade now selects its engine through `builder/unit_dispatch.py`.
A procedural unit that the shared typed evidence predicate classifies as executable-only
runs through Flynn. Other construction/evidence requirements keep their explicit existing
engine. This is domain routing before execution, never a response to failure: native
provider, budget, claim and replay errors propagate without invoking Claude.

The dispatcher requires the exact building claim, selected authority and live shot fence.
Native execution additionally requires the claim's active run, the supported DeepSeek
model and credentials, finite output/time limits and at least four total steps including
scripted canonical replay. Unpriced USD caps refuse before provider construction. The
existing engine retains its candidate guards and canonical evaluator, and the layer
controller still publishes and independently reopens unit and layer completion receipts.

## Shared image-payment operation

Runtime image checks are a VFX operation, independent of the model transport.
`evidence.image_check_operation` owns the closed argument schema, immutable handle
lookup, candidate/adversary measurements, debt matching and result. The existing
Claude tool delegates to it; the native Flynn image-check capability invokes the
same operation through the exact building claim's prepared publication transaction.
Flynn records an observation and external operation, never a unit state commit.

The native capability requires the live builder fence, current run and unit digest,
the caller's current-candidate check, and unchanged run-owned render bytes. Its
returned dispatch guard must be registered by the caller. Image capture, adversary
selection, debt compilation and canonical replay remain harness responsibilities.
This capability does not enable production raster routing on its own.

## Native candidate and adversary capture

`builder.flynn_image_capture.UnitImageCapture` combines cold capture and the shared
payment tool behind one dispatch guard. Its frame choices come from the exact unit's
judge points. It freezes the deterministic scene setup, ordered prior source bytes and
construction dependencies, and retains receipt-backed prior-list checks. Each capture
renders at most one missing pre-unit adversary and one current candidate at EEVEE/0.5;
the scenes are reconstructed independently from empty. Cached adversaries remain bound
to the original prefix and setup. Candidate handles include the exact script digest and
are retired on a new candidate generation; stale handles cannot authorize payment.

Image files are published from verified byte snapshots into the run's render directory.
The complete capture report retains replay inputs outside model context. Flynn receives
the two images, compact identities and a report locator/digest, with no acceptance or
state commit. Capture, candidate, prior, receipt and report substitutions fail closed.
Production raster routing remains gated until the native builder's probe/freeze and
canonical replay consume this capability under existing VFX completion rules.

## Native builder structured feedback

The builder now selects structured tool results through Flynn's strict result reader.
It preserves status, complete text and structured data, replaces image blocks with
ordered image indices in text, and supplies their unchanged URLs and detail levels as
explicit image inputs. Every selected observation carries the original journal output's
SHA-256. Only the latest observation's images are sent; subsequent text-only feedback
removes them. Required text still must fit the existing context budget.

Refused results receive a failed execution assessment while remaining available as
observations. This does not change VFX acceptance or publish a state revision. Plain
canonical replay feedback retains its measured-point selection. Unsupported or malformed
Flynn result envelopes fail closed. This connects the builder's feedback preparation
to the native capture/payment seam; raster probe/freeze integration remains outstanding.

## Native executable image-unit lifecycle

Explicit native unit execution now supports procedural units whose required claims are
executable at every judge point and whose raster medium is EEVEE. Other raster media,
required qualitative decisions and generated construction are refused before execution.
The executor retains the live builder lease through capture, payment and completion.
Production routing for this supported subset is described in the layer integration section below.

Capture and payment tools share the exact candidate/prefix guard with the engine. Their
execution clears the previous probe, including when a payment is refused. Phase grants
reserve room for the remaining captures, payment batches, probe, freeze and canonical
replay; rewriting a captured candidate also reserves recapture. Bounded context lists
current candidate handles by frame and the unpaid debt cards, without retaining old images.

The probe binds both the candidate and the current payment-row fingerprint. Payment
changes during replay refuse the observation. Freeze reopens unpaid debts and image bytes
and requires the probed fingerprint. The same check runs before canonical dispatch and
again before script publication. A substitution between freeze and canonical dispatch
refuses before that external operation or script publication. Existing VFX replay and
evaluation/completion readers remain authoritative; the ledger records their canonical
primary-frame render and measured score. Flynn publishes no unit state revision.

## Production executable image units

The dispatcher and explicit native executor now share one eligibility predicate:
procedural construction, executable required claims at every judge point, no provisional
visual requirements, and EEVEE for raster evidence. Unsupported units retain their existing
migration path; native failures never cause a second-engine retry. Composed layer look
judgment remains separate and is not waived by this unit routing decision.

The layer gate exposed two native integration defects (HIR-0253). The v2 image observation
omits duplicated ownership bindings while preserving payment handles, pixel/source identities,
settings and the complete report locator/digest. Guards, registry and report retain every
binding. The context cap is unchanged. Native image evaluation now identifies the canonical
primary render as its candidate, matching the layer checkpoint; the replay closure separately
binds the exact unit script. Existing completion and layer publication readers remain unchanged.

Validation uses real camera and dependent image layers, payments, empty-scene replay and
receipts with scripted inference and a scripted layer look judgment. The driver gate executes
the actual layer builder through its subprocess seam and reopens current publication; it
must refuse substituted composed output despite a successful child exit. This does not claim
live planning, visual judgment quality, whole-shot acceptance or complete Claude removal.

## Native critic observation transport

`agents/critic_transport.py` records one bounded structured critic opinion through Flynn.
It uses the shared transport-independent `domain/critic_verdict.py` schema, preserving its
existing axes, score, typed observation, focus-frame and reference-usability contracts.
The legacy critic consumes that same schema. Its old schema facade export is removed.

The caller supplies exact scope/phase, requested model, prompt, declared axes/frames,
ordered image paths and a current-authority check. Reference and candidate are required,
with at most two focus panels, one motion strip and one prior image. Native image snapshots
retain verified original bytes instead of using the old Claude JPEG preprocessing; the
explicit native image-shape generation therefore cannot inherit old qualification.
The context cap is 24,000 characters and every context item is required. There is one
inference call and one non-external verdict submission, at most 8,192 output tokens and
180 seconds. No retry, fallback or state commit occurs.

Authority, active run and image bytes are checked before inference, before submission and
before returning the opinion. Invalid responses spend inference but do not submit a tool.
Cancellation and provider failures propagate with their durable spending/termination records.
Reports bind the requested prompt/context/response schema and image identities to the journal,
whose usage records carry requested and provider-reported model identity separately.
Neither a schema-valid opinion nor either identity is
qualification: reports and returned results explicitly set `qualification_verified` and
`acceptance_authorized` false.

This is a native transport capability, not a production critic switch. The existing
qualification reader compares artifact fields to claim fields; runtime critic invocation
does not compare its actual model, prompt and evidence shape to those fields. A production
switch must close that gap, including the composed layer's independent look judgment.
Reusing a previous model's qualification implicitly is not an allowed migration mechanism.

### Reported model identity admission

Flynn's `InferenceUsage.model` identifies the requested model; it did not previously
retain the response's model field. SDK main now adds nullable `response_model` to the
existing immutable usage JSON. DeepSeek records it on valid and rejected responses,
and leaves missing or malformed identity unknown instead of copying the request.
This is transport provenance, not proof of the provider's model weights.

The native critic requires an explicit requested provider as well as model. Its dispatch
guard selects the one usage row for the exact operation and compares provider, requested
model and response model to those intended inputs. Unknown or different identity refuses
before verdict submission, with inference usage and termination retained. Scripted
observations are explicitly `not_applicable`; they never claim a matched model.
The report records this check separately from qualification and acceptance, which remain
false. There is no alias inference or automatic retry. The SDK owns identity transport;
VFX owns the equality policy and the eventual qualification decision.

### Critic image manifest

`agents/critic_images.py` compiles the bounded image list once into explicit role,
path and label entries. Both transports consume those entries in order. The production
critic renders its attachment-order text from that same list, and the prompt identifies
the motion strip by its label instead of assuming it is third. Focus panels retain
their contract ids, crop metadata and reason alongside their manifest label; the prior
attempt remains context only and is never a scoring target.

All declared production images must exist before any encoding or provider dispatch.
Missing motion/prior images no longer disappear silently, an empty optional path is
invalid rather than absent, and more than two focus panels refuses rather than truncates.
The native `vfx-harness.critic-images/v2` shape includes each label beside its role,
position and verified byte identities in required context and the durable input report.
Its actual attached URL order is tested against those identities. This changes the
evidence representation that qualification must cover, and grants no qualification or
acceptance. No engine route or receipt authority changes.
