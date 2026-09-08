# Full Flynn runtime cutover

The owner requires complete removal of Claude and the Claude Agent SDK. Flynn is the
single target runtime. Extend Flynn where needed; do not emulate Claude's API or retain
an engine selector. SDK changes land on main and consumers install main over SSH.

The current remaining paths and validation obligations are tracked in the
[capability matrix](flynn-cutover-capability-matrix.md). The sections below retain
implementation history.

## Ownership and order

1. **Structured tools and results (SDK).** Native text/image content, immutable structured
   data, explicit execution/refusal status, and durable observation serialization. VFX
   supplies schemas, validation and tool implementations. A result is not domain acceptance.
2. **Session execution (SDK).** Bounded steps, typed termination and lifecycle events,
   cancellation and dispatch guards around the existing SQLite runtime. The harness
   supplies context selection and finish policy. No automatic history accumulation,
   retries or session resume.
3. **Role capabilities (VFX).** Register Blender, planning, recipes, bounded file reads,
   candidate writes and documentation lookup as native tools. Keep authority checks,
   confinement and publication at their existing owners. Replace SDK hooks with explicit
   dispatch guards; they must execute before mutation, not after it.
4. **Consumers (VFX).** Migrate approach selection, plan draft/verify/materialization,
   candidate construction/repair, visual critics and acceptance judgments. Use native
   Flynn results and explicit role completion. Preserve all receipt-backed boundaries.
5. **Operational cutover (VFX).** Remove Claude dependencies, session transport, model
   defaults, credentials, project-context injection, SDK event adapters and obsolete tests.
   Public commands use Flynn directly. Preflight checks the configured provider and
   confinement. Report neutral usage; do not interpret unpriced usage as zero dollars.
6. **Proof.** Fresh installation without Claude; complete SDK and VFX regressions;
   offline role tests including refusal, timeout, cancellation and malformed outputs;
   bounded live runs through public commands. Verify native receipts independently.
   Executable fixture success does not prove visual judgments or full-shot acceptance.

## Inventory at the start

Thirty production modules directly import the Claude SDK. These include tool registration
in Blender/planning/recipes; options and hook adapters; planner draft, verify and
rematerialization; approach selection; builder live loops and script agents; critic/focus
sessions; and observability message readers. Acceptance and asset orchestration also depend
on these indirectly. Removing the package line alone cannot complete this migration.

The unfinished optional CLI selector was removed after the owner's correction. The prior
strict preflight passed, but no paid CLI run was started. The existing executable Flynn
builder and its native receipt writers remain the first consumer to build outward from.

## First SDK extension

SDK main `61a12d3` adds native `Tool.structured`, `ToolResult`, `TextContent` and
`ImageContent`. The typed result envelope retains measurements, content order and explicit
refusal through SQLite without committing state. Invalid arguments never dispatch; handler
exceptions and invalid returned results retain an unresolved effect. No image is fetched
or injected into context automatically. A harness supplies the argument validator and
chooses feedback. This extends Flynn's own broker/runtime rather than emulating a Claude tool.

The SDK's 124 tests, Ruff, formatting and mypy passed. The VFX scope test now exercises
these native results against actual compiled work-unit scope. Production role migration
and removal of the Claude package remain outstanding; this extension is not a cutover claim.

Final installation gate: both consumers installed SDK main `61a12d36baa5bd72642244c52901fe8f39f184d9`
over SSH. ARC passed 224 offline tests; VFX passed 167 focused/architecture tests,
including 34 Flynn checks with real confined replay. Ruff and diff checks passed.
The SDK wheel and source distribution built with `uv build`; native structured results
also passed a smoke test in an isolated wheel environment without Claude installed.
No paid inference ran. This was a focused consumer gate, not a full VFX suite run.

The initial VFX gate caught a KeyError in uncommitted CLI summary plumbing left from
the abandoned selector work: active run-layout objects do not share in-memory metadata.
That entire unlanded reporting change and its test assertions were removed. The existing
native usage-report writer is unchanged. The final 167-test gate used unchanged source.

## Bounded session lifecycle

SDK main `3da3579` adds `Session`, driven by explicit `SessionStep` and `SessionStop`
application decisions. Its view contains current state, the latest observation and one
previous step; request preparation still belongs to the harness. It enforces the existing
run budgets, narrows grants and persists a typed termination in the journal's terminal
outcome. Stopping does not certify domain success. Exceptions and cancellation propagate;
uncertain effects stay pending and no retry or recovery is automatic. Finished or initially
unresolved journals refuse before policy runs. Expired deadlines refuse before policy too.

VFX's native scope-transport gate now uses Session and verifies termination after reopening,
while state remains unaccepted. Production role loops are not yet migrated. The next boundary
is native dispatch guards and role-specific tool registration, followed by migration of the
planning and judgment loops; no Claude API wrapper is introduced.

Session validation: 135 SDK tests, Ruff, formatting and mypy passed. Wheel and source
builds passed, and a clean installed wheel executed a terminal session with no Claude
package present. SSH consumers installed `3da3579506003dc0ab0270081c99ce9637997b13`:
ARC passed 224 offline tests and VFX passed 149 contract/unit/architecture tests. No
VFX production source changed in this step, so the full VFX suite and live Blender/model
runs were not repeated. No paid inference ran. ARC's updated lock remains in its shared
worktree; unrelated ARC changes were not staged or committed.

## First production role: approach review

Approach review now uses a native Flynn Session and a single structured `submit_review`
operation. The harness snapshots the selected render/reference and exact current script,
compiles bounded required context plus whole optional recipe excerpts, and checks the
owning attempt before inference and before submission. Native advice cannot mutate scene
state, change authority or accept a build. The recommendation preserves the existing
`replace`/`text` revision contract, with KEEP/REPLACE derived from a validated field.

The role enforces one inference, one submission, zero external tool actions, 2,048 output
tokens and a 90-second session deadline. Required text fits 24,000 characters or refuses;
images are explicit verified local stills capped at 8 MiB each. There is no ambient Read,
Glob, shell, history accumulation or Claude fallback. Provider failure, malformed output,
stale attempts and cancellation propagate; the former broad exception-to-empty-review
path is removed. This is a deliberate replacement of the eight-turn browsing policy.

A run-owned SQLite journal records selected input identities, request and observations,
guard decisions, termination and neutral usage. A report locates that journal and marks
usage unpriced. A configured run USD cap refuses this role before inference until a price
policy exists; unknown dollars are not zero. Basic console logging and pure recipe reads
are separated from the legacy SDK registration so importing this native role requires no
Claude package. Other production roles still require Claude during their migration.

Flynn is now a required private SSH main dependency. Hatch explicitly allows the direct
reference, and CI requires `FLYNN_SDK_SSH_KEY` for read access. Production-role tests no
longer skip when Flynn is absent. This step does not claim a full Claude-free VFX install,
a live model comparison, or completion of planning/building/critic migration.

Validation for this role: 2,947 full VFX tests passed against SDK `a9f3c61`, including
real confined Blender replay. SDK main advanced independently during the run; after
freezing source through completion, the working VFX environment was refreshed over SSH
to `50a8df20fb69d01a4baced1bee617b2c075e732b` and all 58 Flynn/model-configuration
consumer checks passed, including the confined replay and lifecycle gates. A separate
fresh, non-editable VFX wheel installation resolved that same SDK commit and passed all
16 native approach tests, dependency checks and CLI help. Full-source Ruff and diff
checks passed. No live inference or visual-quality comparison ran.

GitHub CI configuration is not yet operational: the VFX repository had no secrets when
checked, so its read-only `FLYNN_SDK_SSH_KEY` must be provisioned before the workflow can
install the private dependency. No existing private key was copied into either repository.

## Native unit-plan publication capability

`agents/flynn_plan_tools.py` exposes `unit_plan_publication`, returning a native Flynn
structured tool and dispatch guard. Its sole model-authored argument is content. The
harness binds the target, selected authority, current-attempt check and live
`WorkUnitPlanTransaction`. The transaction must already claim its current plan/stamp
pair. Closed or mismatched transactions, stale selected authority, unsafe paths and
invalid content refuse before external dispatch. Content is bounded to 200 trimmed
characters minimum, 24,000 total characters maximum and 160 lines.

The existing writer now lives in `orchestration/unit_plan_content.py`. Both the current
planner tool and the native Flynn capability call this same writer; no second plan
writer or Claude-to-Flynn message adapter was introduced. The native handler reopens and
validates the integrity stamp and returns hashes for the exact plan and stamp bytes.
The observation explicitly reports that terminal gate approval is still outstanding.
A successful SDK operation neither commits SDK state nor accepts a VFX unit.

Content and its sidecar are separate file publications. The native handler claims the
actual pair even after a partial failure, leaving rollback to the owning VFX transaction.
SQLite retains an unresolved dispatched effect; it does not invent an atomic filesystem
commit or silently retry. The injected partial-write test proves the VFX owner can
restore the predecessor pair while the SDK still records the uncertain operation.

This is a planning capability gate, not the completed planning-session cutover. Global
planning, materialization and unit-planning model sessions still use their current
transport until their remaining tools, context and terminal policies are migrated.

Validation: the extraction passed all 2,962 full-suite tests, including real confined
Blender and authority checks. Final review added the exact plan/JIT selection token to
the observation and qualified the shared writer's collaborator references; all 48 focused
planning/publication/transaction checks then passed, including a new test preserving a
newer writer's bytes after inference without spending the external-action reservation.
Full-source Ruff and diff checks passed. A non-editable wheel includes the new capability
and imports it with Claude blocked. This used installed SDK main `50a8df2`; no paid
inference or planning-session cutover was claimed.


## Native global ownership-mapping publication

`agents/flynn_mapping_tools.py` registers `publish_ownership_mapping` as a native
structured Flynn tool. Its only input is the complete mapping; it accepts no output
path and caps the serialized UTF-8 input at 64,000 bytes. The shared authoring schema
now closes layer, axis, judge, and capability objects and exposes their required fields.
Schema and semantic validation reject malformed proposals before tool/external spend.

The capability requires an existing run-owned `plan-workspace/v3` workspace. Its guard
and handler verify the exact marker, run/shot binding, selected plan/JIT generation,
authored inputs and decision inputs in both the source shot and the workspace, and
previous output identities. Changed drafts, symlinks and hard-linked output files
refuse before publication. The mapping uses the existing prepared-file CAS transport;
the existing deterministic expander remains the sole compiler of generated documents.
Every declared output is reopened and hashed before the tool emits a structured draft
observation. Missing outputs cannot be certified by the expander's returned manifest.

Neither the operation nor its SQLite evaluator selects plan authority or attests a
terminal gate. A partial expansion remains an unresolved external effect, with no
automatic retry or rollback. An input change detected after writes likewise remains
unresolved. Repeated successful draft submissions retain the preceding output identities
so another writer's work is preserved.

Scripted Session tests cover heterogeneous still/motion drafts and independently run
the real deterministic VFX gate, while proving that the live shot has no selected
plan and Flynn has no state commit. Fault injection covers schema/ownership errors,
byte bounds, changed inputs, changed authority, expired attempts, substituted run
markers, changed outputs, symlinks, hard links, partial writes and missing artifacts.
The native module imports with Claude blocked. This capability is not yet wired into
the production global-planner loop; native bounded reads, gate feedback and escalation
remain prerequisites for that session migration. No new SDK feature or compatibility
adapter is required for this boundary.

Validation: all **3,006 VFX tests passed** on unchanged source, including **43 native
mapping contract cases**; the focused mapping/authoring gate passed **62 tests**.
Full-source Ruff and diff checks passed. A fresh non-editable VFX wheel imported the
native mapping capability with Claude blocked, using the SSH-installed SDK main
`50a8df20fb69d01a4baced1bee617b2c075e732b`. The SDK source was unchanged and no ARC
gate or paid inference ran. The full suite emitted only the existing 15 Pillow
`Image.getdata` deprecation warnings.


## Native global-planner reads and gate feedback

`agents/flynn_global_tools.py` now assembles mapping publication, `read_plan_input`
and `run_gate` under one `PlanningWorkspace` and one Flynn dispatch guard. The
standalone mapping factory contract is replaced directly: publication takes the shared
binding and returns a tool. There is no old-signature alias or compatibility adapter.

Reads expose an enumerated set of planning text documents, never arbitrary paths or
search. Each request returns at most 4,000 Unicode characters, the complete source hash,
explicit slice offsets and continuation, and the workspace/selection identity. An
unwritten draft document returns an explicit refusal. The shared binding checks both
source and workspace inputs and prevents reads or writes against another writer's draft.

The native gate evaluates the actual complete workspace through the existing VFX
validator. Before and after evaluation it proves that the expected inputs and draft
outputs remain unchanged, that the workspace contains no unbound auxiliary files, and
that citations do not leave that workspace. The evaluator's permanent selection-lock
file is explicitly allowed as synchronization metadata; alternate authority pointers
and consumer markers are not allowed.

Each gate writes a run-owned `plan-gate-observation/v1` wrapper containing its exact
draft identity and complete typed evaluation. It returns a hashed report locator,
complete gate status/counts/signature, and up to 8,000 serialized characters of whole
findings, with blockers first and an explicit omitted count. This wrapper is diagnostic
evidence, not a terminal gate attestation or authority publication. Report writes count
as external actions. An evaluation whose inputs change or whose report fails remains
unresolved in SQLite and is not automatically retried.

The existing four-evaluation limit is enforced before a fifth dispatch. Two unchanged
dirty signatures signal a plateau and refuse further gate calls. If no complete finding
fits bounded feedback, `feedback_overflow` ends further evaluations and points the outer
owner to the full report. The eventual role policy must consume these explicit stop
signals; no generic SDK policy chooses VFX completion or repair.

The deterministic gate's import graph exposed logging-only Claude dependencies through
asset adapters and construction. Those modules now import the existing provider-neutral
console logger. Native global-tool imports work with Claude blocked. Production planner
sessions still require migration, including escalation and selected reference images;
this capability set does not claim the full planning or operational cutover.

Validation: the complete **3,044-test VFX suite passed** with source frozen, including
real confined Blender and authority lifecycle checks. After that run, review identified
one local read edge case: offsets beyond EOF now return a structured refusal with the
source length rather than leaving a failed operation unresolved. The final mapping/read/
gate suite passed **83 tests**, including both new EOF cases. Full-source Ruff and diff
checks passed. The final non-editable VFX wheel imports the native global tool set with
Claude blocked and uses SSH-installed SDK main
`50a8df20fb69d01a4baced1bee617b2c075e732b`. No SDK source changed, no ARC tests were
run, and no paid inference ran. Production global-planner session migration remains open.


## Native global-planner questions and reference images

The global tool set now includes `ask_supervisor` and, when authored stills exist,
`read_reference`. Questions require an explicit assumption, reason and impact scope.
Named layers/axes must exist in the current mapping; a global question may precede
mapping publication. The existing prepared-file CAS transport writes the source shot's
question stream under the current workspace/owner checks. A duplicate returns the
actual stored assumption and scope, and the observation binds the reopened stream hash.
A question does not invent an answer, change plan authority, or populate the gate's
workspace. Concurrent publication is preserved and an uncertain dispatch is not retried.
Malformed JSON and invalid question ids now refuse rather than silently disappearing
from question history. This is not a new complete event-record schema.

Reference names are enumerated from the bound authored PNG/JPEG/WebP inputs. Reads
verify the selected bytes and actual image format, return native image content plus
source identity, and enforce the existing 8 MiB limit. Unsupported content produces an
explicit refusal; changed input generations refuse dispatch. Image snapshotting is now
shared with approach review, with the old helper removed. The harness still decides
which image observations belong in subsequent bounded requests.

The native tool set has the planned question and image capabilities, but production
global planning still uses its existing session implementation. The next migration must
thread the live run-owner lease into the role guard, define bounded context and typed
termination for draft/verify/repair, and keep draft audit snapshots outside the closed
gate workspace. The existing two-pass loop writes `global.<tag>.md` inside that workspace;
those snapshots are not part of the native gate's declared input/output set. Terminal
gating and promotion remain at their existing authority owners. No SDK extension was
needed for these two VFX capabilities.

Validation: **3,087 VFX tests passed** with runtime source frozen, including real
confined Blender and authority lifecycle checks; the focused suite passed **176 tests**.
Full-source Ruff and diff checks passed. A fresh non-editable VFX wheel imports the
native global tool set with Claude blocked, using SSH-installed SDK main
`50a8df20fb69d01a4baced1bee617b2c075e732b`. The only suite warnings were the existing
15 Pillow deprecations. SDK source was unchanged; no ARC tests or paid inference ran.


## Native global planning sweep

`agents/global_planning_session.py` executes draft, verify and repair sweeps through
Flynn Session. Each invocation has an explicit name, required caller-selected role
context, finite wall-time and output-token limits, and a mandatory live ownership check.
The invocation creates one SQLite journal and refuses to reopen an existing journal or
replace its report. It does not automatically retry model errors, uncertain writes,
timeouts or cancellation. Verify and repair require a complete existing draft.

The session and tools now share the same `PlanningWorkspace` object. The old tool-factory
signature was replaced directly and its callers updated. Workspace checks run before
inference, before dispatch and before returning. A model must submit the mapping during
this sweep before `run_gate` becomes an available grant. A clean gate stops the session;
a dirty plateau, four-call cap or feedback overflow also stops it, preserving the exact
gate feedback and report locator. These are sweep outcomes, never plan publication.
Independent terminal gating and authority publication remain due at their existing owners.

Request text is compiled within 32,000 characters from the role policy, selected context
and the latest complete tool observation. Required items refuse on overflow rather than
being truncated. The SDK's default observation transport is cleared so serialized image
payloads and prior observations are not duplicated into text. Native image content is
selected only for the request immediately after that image read. Further reads replace
it; neither text nor images accumulate as implicit session history. This character cap
is not a token estimate or a cap on the separately transported tool schemas and images.

The run report preserves the input/context identity, journal locator, neutral usage,
output budget, typed termination, latest gate feedback and whether the final workspace
check completed. Spending is explicitly unpriced. No state commits or selected authority
are produced by this session.

This completes the native sweep policy, not the production global-planner entry-point
migration. The public driver currently launches planning in a child process, while its
live run-owner lease is process-bound and rejects descendant use. Passing an on-disk
claim or a no-op callback would not satisfy that ownership contract. The command cutover
must establish an actual ownership boundary (or execute this stage in the owning
process), compile exact required draft/verify/repair context, move draft audit snapshots
outside the gate workspace, and replace the old prompt/session/configuration path. No
Claude-shaped adapter or runtime selector was introduced here.

Validation: the final native session suite passed **44 tests**, including real gate
execution on still/motion fixtures, real DeepSeek request serialization through an
offline transport, latest-only text/image selection, budget exhaustion, cancellation,
timeout, stale inputs/owners, explicit gate stops and uncertain-write retention. The
complete **3,131-test VFX suite passed** on unchanged runtime source. Full-source Ruff
and diff checks passed. A fresh non-editable VFX wheel imports the session with Claude
blocked and uses SSH-installed SDK main `50a8df20fb69d01a4baced1bee617b2c075e732b`.
The full suite emitted only the existing 15 Pillow deprecation warnings. No SDK source
changed, no ARC tests ran and no paid inference ran.


## Production global-planner cutover

Global `generate_plan` now lives in `agents/global_planner.py` and uses Flynn directly.
The old Claude query loop, mtime success heuristic, global SDK hooks, compatibility tool
policy and old draft/verify/repair prompt functions were removed. JIT planning keeps its
separate implementation pending its own migration. Global model configuration is now
separate from JIT: `VFXH_GLOBAL_PLANNER_MODEL`, with `DEEPSEEK_MODEL` then Flynn's vision
model as defaults, plus explicit phase wall-time and output-token limits. Missing keys,
unsupported models and unpriced USD caps refuse before inference.

The root boundary exposes only its scoped, live same-process owner lease. It checks the
lease's actual identity and exact run before returning it; inherited environment variables,
a different run, a released lease and forked descendants cannot use it. `vfx run` now
executes only its global planning stage in that owning process. Layer isolation remains
at its existing boundary. Direct `vfx plan` uses the same owner capability. A dirty global
stage publishes its typed stop through the inherited boundary; the driver alone selects
terminal status.

The role compiles required authored brief, clause registry, frame convention and decision
inputs. Verify and repair additionally consume a complete source-verified draft snapshot
and its baseline mapping; repair includes explicit gate feedback. Snapshots bind every
generated document and remain in run reports outside the closed gate workspace. Their
bytes are checked throughout the phase. Tagged runs keep the canonical draft path and
remain diagnostic. The old `--verify-only` implicit reuse path was removed; model-free
retained-candidate promotion remains explicit through `--promote-run`.

Verify budget exhaustion can hand the candidate to the independent outer gate only through
`PlanningSweepExhausted`, which certifies no pending operation and a current workspace.
An ordinary budget error with an unresolved operation, changed authority, or provider
failure does not authorize that continuation. All accepted plan selection still passes
through the existing terminal gate and VFX authority publisher.


The production dirty-plan probe also exposed and fixed HIR-0252: the old deterministic
gate ignored declared client blockers retained by the compiler. The shared contract
evaluator now emits plan-wide blocking findings from that typed list. Both native and
terminal gate paths use it, and the heterogeneous production-stage regression proves
that the driver receives a typed stop without selecting plan authority.


The first broad regression run exposed that consumer projections use legitimate links.
The blocker reader now resolves the exact verified global bundle through the same
source-selection helper as global layer ownership, then reads the immutable source.
It does not follow arbitrary projection links. Old synthetic mapping fixture headers
were updated to the current schema; production parsing remains strict. That initial
known-failing run was stopped and is not counted as passing validation.

Final validation on the corrected source passed all 3,157 collected tests across four
isolated groups (656 + 976 + 781 + 744). The only warnings were 15 existing Pillow
`getdata` deprecations. Complete-source Ruff and `git diff --check` passed. A freshly
installed wheel's production `global_planner` module imported with `claude_agent_sdk`
blocked, using Flynn from SSH `main` at `50a8df20fb69d01a4baced1bee617b2c075e732b`.
This proves the global role's import boundary, not removal of Claude from the whole
application: JIT planning, builders, and critics still require migration. No SDK source
changed, no ARC gate ran, and no paid inference or full-shot visual acceptance was claimed.

## Native JIT materialization tools

`agents/materialization_operations.py` now owns the six existing VFX candidate
operations independently of either model SDK. Their Claude registration is a small
transport wrapper used by the still-unmigrated JIT model session; the native Flynn
factory calls those same VFX handlers directly. There is one staging/patch/finalization
implementation, and no Claude message conversion in the native path.

`agents/flynn_materialization_tools.py` binds an existing seeded candidate to one run's
scratch directory, exact authority selection, candidate and finalization revisions,
validation sources (including every replacement-overlay artifact), authored and
decision inputs, and the layer's declared references. Its required caller-owned live
attempt check runs at registration, dispatch, execution and the guarded candidate write.
Undeclared paths, linked candidates, schema-invalid calls and stale inputs refuse; no
newer candidate is silently adopted. The native crop tool enumerates only the layer's
judge references. A complete structured observation and text are retained in a hashed
run report; model feedback has explicit text/detail bounds and omission fields.

Flynn records operations, reservations and uncertainty. VFX retains schema compilation,
mutation permission, revision CAS, deterministic gate interpretation and independent
publication. `finalization_current` comes from reopening the actual finalization
record, not interpreting success prose. Exceptions after a write remain pending in the
SDK journal, and no native observation commits state or selects plan authority.

`jsonschema` is now an explicit runtime dependency: native schema validation already
needed it for global planning and must not rely on a development or transitive install.

The production JIT model session still uses its existing transport while its other
capabilities and bounded context policy migrate. No paid inference, model-quality
comparison, automatic resume, SDK change, or ARC gate is part of this step.

The next JIT session gate requires native reference measurement, evidence vocabulary
and gap escalation, recipe lookup, supervisor questions, and bounded spike execution.
Its policy must provide the active layer's compiled context and terminate by checking
the exact finalization record. Switching the model loop before those instruments are
available would remove existing planner capabilities.

Validation: **39 focused tests passed**, including real staging/patching/unstaging,
crop-witness creation, candidate ownership and schema refusal, post-write uncertainty,
bounded feedback, and clean/dirty finalization through the real attestation writer with
a controlled gate verdict. The full suite then passed **3,179 tests** on unchanged
runtime source (667 + 973 + 762 + 777), with only the 15 existing Pillow deprecation
warnings. Complete-source Ruff and diff checks passed. An isolated installed wheel
imports the native module with Claude blocked, explicitly declares `jsonschema`, and
passes dependency checks using SSH Flynn `main` at
`50a8df20fb69d01a4baced1bee617b2c075e732b`.

## Native JIT planning knowledge

`agents/flynn_planning_knowledge.py` supplies native evidence vocabulary, recipe lookup,
and supervisor-question capabilities for one selected layer or unit. Registration and
dispatch check the caller's live attempt, exact selected authority, and authored/decision
inputs. Invalid schema and unknown question impact refuse before tool reservations.

The evidence instrument returns a registry index or one exact kind's definition,
domain, fields, and operators. Its source is the same provider-neutral vocabulary
compiler used by the existing planning transport. Recipes likewise share discovery and
section formatting; selected units retain mutation-role filtering, while layer planning
discovery remains informational. Native reads are limited to six calls, three distinct
body fragments and 12,000 emitted characters. Rereads count against the budget rather
than assuming earlier fragments remain in context. Oversized code is never truncated.

Global and JIT native questions now use one publication helper. It prepares the existing
VFX append transaction, rechecks the live binding before commit, discards only its own
uncommitted preparation on failure, then reads the durable question back. Duplicates
return the original assumption and impact; no answer, approval, or state commit is
invented. Read-only knowledge tools consume no external-action reservation; question
publication does. The caller still owns feedback selection and session completion.

This step does not switch the production JIT session. Native vocabulary-gap recording,
reference measurement and bounded spike execution remain before that cutover. No SDK
source change, ARC installation gate, paid inference, or visual-quality claim is involved.

Validation: **94 focused tests passed** across the new capabilities, existing global
questions/reference selection, recipe scope and evidence vocabulary. The final full
suite passed **3,198 tests** on unchanged runtime source (655 + 980 + 785 + 778),
with only the 15 existing Pillow deprecation warnings. Complete-source Ruff and diff
checks passed. The freshly installed wheel imports native planning knowledge and the
global planner with Claude blocked, exposes all 32 registered evidence kinds, performs
recipe lookup, and passes dependency checks. Flynn remains installed from SSH `main`
at `50a8df20fb69d01a4baced1bee617b2c075e732b`; its checkout is unchanged.

## Native vocabulary-gap publication

Layer planning now exposes `escalate_vocabulary_gap` through Flynn. Its requirement
ids come from the selected global layer's owned requirement register; the claim must
match the exact authored statement. Requests enumerate at most eight distinct registered
kinds with bounded explanations. Unit sessions receive no gap-publication grant.

Both planning transports use `orchestration/vocabulary_gap_publication.py`. It prepares
an atomic replacement of the existing shot ledger, rechecks current authority before
publication under the selection guard, and reads back the committed record and digest.
Identical requests return the stored record, including its original run id. Concurrent
writes refuse rather than overwrite; malformed existing rows refuse publication rather
than being silently repaired. Flynn records this as an external observation, with no
SDK state commit or plan acceptance.

This preserves the existing v1 gap semantics: records are shot-wide, keyed by requirement
id, and permit a separately evaluated provisional decision. The tool's selected-authority
binding is enforced at publication and recorded in the Flynn observation; this change does
not introduce generation-scoped gap expiration or change the existing gate reader's
malformed-line policy. The model's explanation remains a declared vocabulary limitation,
not executable proof that no metric exists. Materialization validation and the terminal
gate retain their existing shared decision predicate.

Reference measurement and confined spikes remain before switching production JIT sessions
to Flynn. The SDK is unchanged; this capability is VFX domain policy and publication.

Focused validation: **40 tests passed**, including native scope/budget checks, owner loss
after preparation, concurrent ledger publication, malformed-ledger refusal, duplicate
read-back, and actual materialization validation before and after publication. The built
package imports both native planning knowledge and the gap publisher from an isolated
installation with `claude_agent_sdk` blocked. Complete-source Ruff and diff checks passed.

Full regression validation passed **3,211 tests** on unchanged runtime source
(642 + 926 + 831 + 812), with the 15 existing Pillow warnings. All four groups exited
successfully. Flynn's checkout remains clean on `main` at `50a8df2`; no ARC tests or SDK
changes were required for this VFX-only capability.

## Native reference measurement

Native layer/unit planning now includes `measure_ref`, scoped to the selected layer's
judge references or the exact selected unit's evaluation references. The existing live
planning guard runs before dispatch; the measurement also checks the bound reference
hash before and after computation. The image encoder and metrics consume one byte
snapshot, and every successful response includes the original image, source digest,
dimensions and canonical fingerprint. No path-based metric cache supplies the values.

A fresh session permits six reads, including rereads. Images are restricted to verified
PNG/JPEG/WEBP stills, 8 MiB and 16 million pixels. Invalid paths refuse before tool
reservation; invalid image bytes and size violations return explicit refusals. Metric
programming failures propagate rather than being mislabeled as reference problems.
Read-only measurement spends no external-action budget and commits no accepted state.

Unlike the remaining retained-conversation adapter, the native tool never suppresses an
image because an earlier call returned it. Bounded context may have evicted that earlier
observation. Canonical metrics operate directly on decoded source pixels; there is no
intermediate transport resize or JPEG conversion before measuring. Fingerprints describe
image properties, not VFX success or permission to change a contract.

Confined spike execution remains before the production JIT session cutover. No SDK
change or paid model invocation is needed for this capability.

Focused validation passed: **97 tests** covering native measurement, global planning
inputs, and existing fingerprint/plan contracts, plus **19 planning-knowledge tests**.
The installed package imports native knowledge and measurement with Claude blocked and
computes a canonical fingerprint from verified image bytes. Complete-source Ruff and
diff checks passed. Full regression validation passed **3,228 tests** on unchanged runtime source
(798 + 798 + 816 + 816), with the 15 existing Pillow warnings. All four groups exited
successfully. SDK source and production shots were unchanged.

## Native confined spikes

`agents/flynn_spikes.py` binds a fresh layer-planning spike tool to the current VFX
owner check, selected authority and authored/decision inputs. Eligibility and hypothesis
budgets now live in the shared transport-independent `agents/spike_policy.py`; existing
planning transport imports the same policy. Reordering contract rows cannot reset the
semantic hypothesis budget. Native requests use closed bounded arguments and the existing
artifact Python policy, with four attempts per session and two failures per hypothesis.

`blender/spike_execution.py` constructs a private scratch scene and evaluates it in a
second Blender process with automatic Python execution disabled. Both processes use the
mandatory descriptor-pinned filesystem confinement and worker syscall policy. Only the
scratch outputs are writable; plan/shot authority is not mounted as writable. The model's
stdout is diagnostic text, never the contract-result channel. Trusted probes in the fresh
process produce measurements, and the parent applies the existing contract predicates.
Execution failure or timeout produces no invented measurements or passing result.

The construction/evaluation pair shares a bounded timeout. Output files are capped at
8 MiB; feedback carries bounded diagnostics and links to a hashed run report. One-shot
workers exit explicitly after their trusted program completes: Blender's audio teardown
otherwise attempts denied socket operations and hangs even with `-noaudio`. Exceptions
remain failures, and neither network restrictions nor the artifact policy are relaxed.

Native spike reports are scratch observations with exact source/artifact hashes and
`planning_evidence_published=false`. They are not silently promoted into the existing
immutable planning-evidence receipt format. Production JIT session wiring and explicit
publication of admissible spike evidence remain before the old planning transport can
be removed. The existing transport's direct process launcher is not used by native
spikes; its retirement accompanies that cutover. No SDK source change is required.

Focused validation passed **69 tests**, including real confined Blender construction,
fresh-process socket measurement, rejection of a forged stdout success marker, a
960×540 render, and a real timeout without invented measurements. Native tool tests
cover scope/argument refusal, owner loss, external budgets, hypothesis failures and
report identity; existing planning and eligibility tests also passed. The installed
package imports native spikes with Claude blocked. Complete-source Ruff and diff checks
passed. This validation used temporary fixtures and no paid inference or production shot.

Full regression validation passed **3,238 tests** on unchanged runtime source
(805 + 805 + 805 + 823), with the 15 existing Pillow warnings. All four groups exited
successfully; complete-source Ruff and diff checks remained clean. SDK source was unchanged.

## Spike citation acceptance prerequisite

Inspection before implementing native evidence publication found that the existing
`plan-spike/v1` reader verified script/output hashes and exact contract equality, but
accepted stored `pass` flags without re-evaluating the reported values. Its result mapping
also silently replaced duplicate ids, and an empty contract/result set could claim success.

The citation gate now requires a nonempty, distinct contract set and exactly one result
per contract. It validates the contract, refuses errors and nonnumeric or nonfinite
measurements, and applies the existing scene predicate to the recorded value. Stored
success flags must agree; they cannot override a failed predicate. Non-object records
produce a blocking schema finding. These checks belong to VFX, not Flynn.

This is a prerequisite correction, not the native publication operation. The v1 reader
still consumes recorded measurements; recomputing a predicate does not independently
prove their provenance. Native publication must bind the fresh-process readings and
their complete execution sources, retain Blender version identity, and freeze that
evidence through the owning plan transaction. The current native spike capability is
layer-bound, while the old depositor only publishes in a global workspace; copying the
old depositor into native JIT would not establish the missing publication ownership.
Native reports therefore remain explicitly unpublished scratch observations.

The previous committed gate was replayed offline against three injected records:
an out-of-band value claiming success, a failed result hidden by a duplicate passing
result, and empty contracts/results claiming success. It reported zero blockers for
all three. The corrected gate rejects each. Focused validation passed 60 tests,
including 21 new refusal cases; the existing positive fixture now declares a valid
scene-contract lifecycle, semantic role selector, and normalized bound.

Full regression validation passed **3,259 tests** (810 + 810 + 810 + 829), with
the 15 existing Pillow warnings. All groups exited successfully on unchanged runtime
source. Complete-source Ruff and diff checks passed. No SDK changes, ARC runs, paid
inference, or production-shot changes were needed.

## Native layer-materialization session

`agents/materialization_session.py` now assembles native materialization, planning
knowledge, reference measurement and confined spike capabilities in one bounded Flynn
session. The materialization capability set exposes its exact current-input check and
identity to its caller, so session policy, request preparation, sibling capabilities,
dispatch and return all use the same candidate revision boundary.

Each invocation creates a fresh SQLite journal with explicit wall-time and output-token
limits. Reusing its name refuses, including after budget exhaustion. Required layer
context and the latest structured observation must fit the 32,000-character envelope;
feedback overflow refuses another inference rather than dropping required content.
Images are forwarded only with their selected observation, then replaced. Tool results
remain observations, without SDK state commits. The run report records usage, budget,
termination and whether finalization was current at return; pricing remains unassigned.

Successful finalization prose cannot stop the session. After a successful finalize call,
the session reopens VFX's current finalization record, verifies the exact candidate again
at return, and leaves authority selection to `publish_materialization`. A candidate
changed between steps or during termination cannot be reported as finalized.

The preceding investigation's ordering is corrected for this stage: spike-evidence
publication is not a prerequisite for materialization. Its transaction accepts typed
contracts and finalization, while the native global planner deliberately has no Blender
capability. Promoting exploration into a new acceptance surface is unnecessary here.
Spikes remain explicitly diagnostic. A future citable spike contract would still need
its own provenance-complete VFX publication path.

This provides the session boundary, not the production entry-point cutover. The legacy
`planner/rematerialize.py` adapter still invokes Claude; replacing that invocation,
its configuration and transcript accounting with this session is the next integration
step. Unit-plan generation, builders and judgment roles also remain to be migrated.

That adapter change must also remove the old write-feedback hook dependency from the
materialization kickoff path, enforce a current process-owned run lease, and supply
explicit materialization model/time/token settings. The session deliberately receives
an inference adapter and required context from its caller rather than choosing provider
configuration or importing the legacy planner facade itself.

Focused regression validation passed **83 tests** across native materialization tools,
the session and existing global planning. The isolated installed package imports the
session and capability set with `claude_agent_sdk` blocked. Complete-source Ruff and
diff checks passed; no model calls or production shots were used.

Full regression validation passed **3,276 tests** (815 + 814 + 814 + 833), with
15 existing Pillow warnings. All four groups exited successfully on frozen runtime
source. Ruff and diff checks remained clean. SDK source and ARC were unchanged.

## Production materialization cutover

`planner/rematerialize.py` now calls `materialization_runtime.execute`, which configures
the native Flynn session with the existing charter and exact compiled layer authority.
It no longer constructs a Claude client, MCP tool server, SDK options, retry loop, or
write-feedback hooks. The materialization-only hook and obsolete deny-list/default-model
exports were removed. The mixed planner package still contains the legacy unit planner;
the standalone native runtime and session import without Claude.

Materialization has independent model, time and output-token settings, defaulting to
the configured DeepSeek model, 600 seconds and 32768 tokens. An explicit model override
is validated, credentials are required before inference, and unpriced USD limits refuse.
Unit planning retains its own model setting until that role is migrated. The existing
requirement-derived step cap remains unchanged.

The previous step's root-owner lease proposal is corrected: controller materialization
can execute in a child stage. The public planning boundary instead retains the existing
shot-wide execution fence, reuses a supplied builder lease, and passes it through every
native request and dispatch to publication. A concurrent builder or expired lease
refuses. Both candidate and journal get fresh identities, preserving earlier attempts.

The SQLite journal replaces materialization's optional transcript/cost callbacks and
retains complete requests, calls, observations and neutral usage. The session report
selects it and the final candidate identity. The VFX publisher still independently
verifies finalization and commits selected authority; known inference failures and
budget exhaustion produce the existing typed materialization stop without retries.

Production unit planning, builders, critics and remaining legacy planning tool adapters
are not part of this cutover. Their migration remains necessary before removing the
Claude dependency from the package as a whole.

Focused regression validation passed **98 tests** across the native session, production
adapter, rematerialization preservation, typed stops and architecture checks. The final
production-boundary set passed **11 tests**, including an additional public-entry-point
run through real staging, finalization and authority publication using scripted inference
and a controlled gate. These tests reopen the journal's complete requests/calls/results
and usage, check fresh settings and credentials, reject expired/contending fences, and
confirm the public wrapper retains or acquires the same live lease. The isolated installed
native provider/session modules import with Claude blocked. No paid inference or production
shot was used.

Full regression validation passed **3,291 tests** (818 + 818 + 818 + 837), with
15 existing Pillow warnings. All groups exited successfully on frozen runtime source.
Complete-source Ruff and diff checks passed. SDK source and ARC were unchanged.

## Production unit planning cutover

`planner/generate.py` now calls `unit_planning_runtime.execute` inside its existing
work-unit plan transaction. It no longer opens a Claude session, creates MCP servers,
attaches legacy kickoff blocks, binds callback cost/transcript writers, or retries an
uncertain session. The transaction claims its predecessor before inference and native
publication tracks each owned plan/stamp pair. Session exit no longer adopts arbitrary
current bytes. Exact selected-bundle plans retain model-free reuse.

The native runtime requires the live shot-wide execution lease and the exact unit's
planning claim, including its phase and shot. Each content publication holds that
claim. It uses the supported Flynn DeepSeek vision adapter, required role and unit
context bounded to 32,000 characters, output-token and wall-time caps, and a fresh
SQLite journal. Unit-selected reference measurements, bounded recipes and vocabulary,
supervisor questions and confined diagnostic spikes use the existing native tools.
Only the latest tool feedback and images reach the next inference request. There is no
layer vocabulary-gap grant or general write/shell surface.

The session must publish its own plan and obtain a clean preview for the owning layer.
At most three previews run the current consumer-view gate; complete reports retain
findings while model feedback is bounded. The outer transaction independently reruns
the terminal gate, verifies the exact plan/stamp pair, then attests or rolls back.
A clean preview grants no gate attestation, build acceptance, or SDK state commit.
Changing authored inputs, selected authority, claim ownership or either output refuses
continued work; an unrelated writer's bytes cannot be adopted at session completion.

`VFXH_PLANNER_MODEL` now defaults to `DEEPSEEK_MODEL` (or Flynn's vision model).
`VFXH_UNIT_PLAN_SECONDS` and `VFXH_UNIT_PLAN_OUTPUT_TOKENS` default to 600 and 32768.
Credentials remain outside reports and settings. Unpriced USD caps fail before the
adapter is constructed. Session reports select complete SQLite requests, observations,
usage and termination; they explicitly record that terminal gate attestation is external.

Focused validation passed 49 publication/session/transaction tests and 45 planner,
configuration and architecture checks. The production path uses real planning claims
and execution leases with a scripted adapter, including an independent terminal gate
that rejects after a clean session preview. Other cases cover repair after a failed
preview, preview caps, latest-image replacement, stale ownership, foreign output,
authored-input changes during evaluation, strict configuration, bounded context and
spent invocation refusal. The isolated installed native runtime/session import with
Claude blocked. No paid inference, production shot, SDK source or ARC changes were made.

Remaining: migrate production builder and critic sessions, then retire their Claude
transport and the now-unused legacy planning/tool infrastructure as one checked removal.

Full regression collected 3,318 tests. Its four groups produced 818 + 818 + 818 +
836 passes (3,290), with 15 existing Pillow warnings and 28 setup errors, all from
one obsolete `test_global_planner_cutover.configured` patch of the removed Claude
`query` symbol. The fixture now asserts that retired transport is absent. Its complete
30-test module passed on rerun, covering every affected case. Production source stayed
frozen throughout the full run; only that test fixture changed. Complete-source Ruff,
diff checks and the isolated installed native imports passed. Thus every collected
case is covered by a passing run; the original full invocation itself exited nonzero
for the explicitly resolved fixture errors.

## Executable builder ownership before production cutover

Inspection of the opt-in native builder found two gaps: unit-claim checks happened
inside handlers after tool/external budget reservations, and the engine reread its
scratch file without retaining the exact revision it had written. A newer candidate
could therefore reach a later probe or be overwritten before the frozen-digest check
became relevant.

`builder/flynn_unit.py` now registers the same exact claim/candidate DispatchGuard on
its model and scripted canonical runtimes. Request preparation and ledger startup
also check that identity. Ownership begins with an absent candidate and advances to
the SHA-256 of requested source bytes only after a completed write. The handler then
reopens and verifies those bytes. It never establishes ownership by hashing whatever
happens to be present. Missing, linked and substituted candidates fail closed, and a
revoked claim consumes no additional tool/external reservation. Inference usage is
still recorded, and a failure after actual dispatch remains an uncertain external
operation in SQLite.

This is a prerequisite fix, not the production builder cutover: the existing native
engine still supports procedural executable-only units and refuses raster/judgment
debts and generated-asset construction. Existing VFX canonical replay, evaluation and
completion publishers remain unchanged. No SDK source, ARC or production shot changed;
Flynn already supplies the required generic guard and durable reservation contracts.

Validation: 34 dispatch, budget, real confined Blender lifecycle and dependent-layer
tests passed. The final nine-case dispatch module also passed after adding canonical
revocation between scripted inference and dispatch (35 distinct tested cases in total).
The cases preserve foreign bytes, reject deleted/symlinked candidates before external
spend, refuse pre-existing candidates before ledger startup, retain uncertain writer
effects, and prove that frozen candidate substitution never reaches canonical publication.
Complete-source Ruff and diff checks passed. This changes one existing builder module
without import-order or package-boundary changes; the complete repository suite was not
rerun, and no live inference was performed.

## Native builder cold-prefix inspection

The opt-in builder's `inspect_unit` previously applied an evaluation barrier to the
worker's current scene without reconstructing accepted priors. That scene could hold
unaccepted objects or omit an accepted producer, so its observation disagreed with
what later canonical verification would execute.

Inspection now prepares the exact ordered replay inputs, resets to an empty scene,
applies the shared preamble, executes the shared prior replay path, sets the unit
frame, then applies the evaluation barrier and reads objects. It rechecks script and
construction dependency bindings after replay and after recording its report. Missing,
failed or substituted inputs propagate as execution failures, leaving no successful
observation. Owned objects and read-only predecessor objects are selected through the
shared semantic-role matcher; seeing predecessors does not expand mutation permission.

The new `vfx-harness.unit-inspection/v1` report binds the actual claim, frame, complete
ordered script/dependency identities and scoped objects. Bounded model feedback carries
only those scoped objects, prefix count and report locator/digest. Both explicitly
carry `acceptance_authorized: false`. Existing VFX replay and completion receipts
remain the acceptance boundary; the production builder and visual-debt migration
remain outstanding. SDK source, ARC and production shots were unchanged.

Validation passed 35 builder budget, dispatch, real confined Blender lifecycle and
dependent-layer tests, plus four new inspection failure cases (39 distinct tests).
The lifecycle fixture starts with an unaccepted object and a wrong current frame;
inspection removes the object and selects the actual unit frame. A direct Blender
frame read was then verified in all four canonical lifecycle cases. The dependent
chain verifies that the consumer sees its accepted producer, excludes unrelated
objects and records exact ordered prior paths. Missing, failed and substituted
inputs, including substitution during report publication, yield no model observation.
Complete-source Ruff and diff checks passed. This change stays within the existing
builder module and its dependency graph; the full repository suite was not rerun.
No live inference was performed.

## Production procedural executable builder

The builder facade now exports `unit_dispatch.build_unit`. It requires the live shot
fence and exact building claim, then chooses by typed construction and the shared
`_unit_requires_raster` predicate. Procedural executable-only units use Flynn by default;
raster/judgment-debt or generated-asset units retain their explicit existing engine.
This choice occurs before execution. Native configuration, provider, budget and replay
failures propagate without a retry or fallback through Claude.

The native path requires its claim's active run, the supported DeepSeek vision model
and `DEEPSEEK_API_KEY`. Independent executable-builder settings default to 600 seconds,
32768 output tokens and 12 total steps including scripted canonical replay (minimum
four). Unpriced USD caps refuse before provider construction. The provider is created
inside the retained execution lease and the core engine rechecks its exact unit/candidate
guards. Existing script publication, cold replay, checkpoint, unit completion and layer
finalization readers retain all acceptance decisions.

Focused validation passed 12 tests. The default production layer path (without a custom
unit_builder injection) executed the dependency fixture through a scripted
provider, actual confined Blender, canonical replay and real unit/layer publishers.
It earned all completion receipts, reopened the passing layer publication, and retained
five guarded journal operations per unit with no SDK state commits. Other tests verify
exact argument/budget forwarding, credential/model/USD/step/phase/run/resume refusal
before provider construction, explicit raster routing and propagation of native budget
failure without calling the legacy engine. A temporary installed package resolves the
public builder entry point to the new dispatcher. No paid inference or production shot
was used, and SDK source and ARC were unchanged.

Remaining: native raster/image-payment and visual-judgment builder capabilities,
generated-asset execution, critic sessions and retirement of their Claude dependencies.

The full 3,343-test regression executed against frozen production source: 3,341 passed
and two source-inspection assertions failed because they still inspected the facade's
former raster implementation. Those assertions now inspect the owning raster engine
and also pin the public dispatcher identity; both complete affected modules then passed
all 47 tests. No runtime fix was needed. The full run emitted 15 Pillow `getdata`
deprecation warnings. Complete-source Ruff and diff checks passed.

## Shared image payments and native Flynn transport

The former `propose_checks` body now lives in `evidence.image_check_operation`.
It imports without Claude, shares one schema and measurement/publication path across
transports, and returns explicit kept and unpaid ids alongside complete feedback.
Oversized batches and unknown fields fail validation instead of being silently
truncated. Existing necessity margins, debt matching, adversary selection and
runtime evidence publication retain their VFX ownership.

`builder.flynn_image_checks.image_check_tool` binds that operation to a live builder
lease and exact building claim. It verifies the current run, unit digest, image
records' run/unit/parent-chain identities and actual bytes before dispatch and
publication. The caller supplies its current-candidate check and must register the
returned Flynn dispatch guard. A revoked candidate discards only the staged payment;
successful results carry `accepted: false` and do not commit SDK state.

Focused validation covers 65 distinct tests: a real VFX authority/claim and guarded
payment write through a scripted Flynn runtime and SQLite journal; candidate change
during preparation; wrong run/unit/chain and replaced images; invalid handles and
settings; argument bounds; existing image provenance, debt, instrument and threshold
tests. The shared operation also imports in a subprocess that blocks Claude imports.
These image fixtures are synthetic stills; no new Blender render or paid inference
was used. SDK source and ARC were unchanged.

Production raster routing remains gated. Next is binding pre-unit adversary capture
and current candidate render handles to this capability, then proving image-payment
and canonical replay together before switching raster units to Flynn. Generated
assets and remaining critic sessions still require migration.

Final validation ran all 3,355 tests against frozen production source: 3,354 passed
and one source-inspection assertion still looked for rejection rendering in the
former wrapper. It now inspects the shared operation and proves the production
wrapper delegates there; the full five-test feedback module passed after correction.
An earlier interrupted run identified a direct render-path construction; the adapter
was changed to the central read-only run-artifact resolver, its 23-test architecture
and native contract check passed, and the full run above started fresh afterward.
The full run emitted 25 Pillow `getdata` deprecation warnings. Complete-source Ruff,
diff checks and a final isolated package import with Claude blocked passed.

## Native cold image capture connected to payment

Added `builder.flynn_image_capture.UnitImageCapture`, an isolated native capability
combining `capture_unit_frame` and `propose_checks` with one dispatch guard. It freezes
the scene preamble and exact ordered prior script/dependency inputs. Receipt-backed
prefixes retain their current-publication checks. Frame choices derive from the bound
unit's declared judge points; capture resets and replays the prior scene independently
for the harness-selected adversary and current candidate, at fixed EEVEE/0.5 settings.

Candidate records include the exact candidate SHA-256. Rewrites refuse payment until
recaptured and retire old candidate handles; same-prefix adversaries can be reused.
Images, candidate inputs, priors and the capture report are reopened before registration.
Partial or substituted captures register nothing. Detailed replay inputs stay in the
run report; the model gets two immutable image snapshots and compact report identities.
The combined tools use Flynn's existing structured results, guards, budgets and SQLite
observations. No SDK change, ARC work or live inference was required.

A real confined Blender fixture cold-rendered a dim prior surface and brighter candidate,
measured their actual images and published a runtime check through the native payment
tool. It began with a stale warm-scene object and wrong frame, and confirmed both were
excluded from the cold candidate. This proves capture/payment transport, not unit
acceptance or a general visual-quality result. Production routing is unchanged: next is
integrating these capabilities with native probe/freeze and canonical acceptance replay.

Validation passed 60 focused contract, architecture, image-debt/provenance and real
Blender integration tests against frozen source. Failure injection covered candidate
substitution during inference and rendering, prior bytes/order/receipt changes, scene
setup changes, invalid frames, failed renders and replaced reports. Payment refusal
after inference preserves the inference reservation and spends no external dispatch.
Ruff on the complete source tree, diff checks and an isolated installed-package import
passed. The tests emitted 22 existing Pillow `getdata` deprecation warnings. This adds
one isolated builder capability without changing existing runtime imports or routing;
the full repository suite was not rerun.

## Builder image feedback transport

The native builder's request preparation now decodes Flynn structured results rather
than serializing image payloads into bounded text. It retains full text, status, data
and original observation SHA-256, preserves label/image ordering through image indices,
and sends image URLs and detail levels through explicit image inputs. Selection replaces
the prior images on every request. Refused operations receive failed assessments while
their complete correction feedback remains available; no state revision is published.

The capture/payment contract now uses this production feedback preparation. An additional
SQLite-backed three-step test proves that two images reach the next request, a subsequent
refusal removes them, the full legal-threshold explanation survives, and original outputs
remain in the journal. Invalid and unsupported result envelopes fail closed; plain
canonical feedback retains failed measurements and missing-evidence identities.

All 62 focused feedback, capture, payment, dispatch, phase-budget and real Blender unit
lifecycle tests passed, with 15 Pillow deprecation warnings. Complete-source Ruff passed.
An additional isolated import with `claude_agent_sdk` blocked failed at the existing
`agents/builder/__init__.py` import. Native execution must not be confused with a
Claude-free package: the shared initializer and remaining raster/generated/critic
paths still require migration. No SDK changes or paid inference were needed here.

The full frozen-source regression subsequently passed all 3,374 tests in four isolated
processes (830 + 848 + 848 + 848), with 37 existing Pillow deprecation warnings.
Complete-source Ruff and diff checks passed after the run.

Next, retain the live builder lease for the capture capability, add budget-aware capture
and payment grants, invalidate measured-candidate status after evidence changes, and
reopen required payments at freeze. Prove a genuinely accepted executable image-contract
unit and injected unpaid/stale cases through existing cold canonical replay and receipt
readers before enabling production raster routing. Qualitative and generated construction
need their own explicit migrations; they must not fall through to an implicit critic call.

## Native executable image-unit completion

The explicit Flynn unit executor now retains the live builder lease and registers the
cold capture/payment capability alongside its candidate and claim guard. It admits only
procedural execution, required executable claims at every judge point and EEVEE raster
evidence. Production raster dispatch remains unchanged for the next layer/driver gate.

Capture and payment clear the previous probe. The next request contains current frame
handles and unpaid debt cards, while image transport remains limited to the latest
observation. Phase grants account for missing captures, payment batches and the remaining
probe/freeze/canonical path; a rewrite of a captured candidate reserves recapture too.
Batch size comes from the shared payment tool schema.

The probe binds the candidate and payment-row fingerprint, refusing a payment change
during replay. Freeze reopens payment coverage, the fingerprint and captured bytes;
canonical dispatch and pre-publication repeat that check. The canonical verifier supplies
the ledger's primary-frame image and score. Existing evaluation and completion readers
reopen the actual artifacts; the Flynn journal has no unit state commits.

The real Blender fixture creates an emissive mesh through the permitted artifact API,
declares geometry and illumination plus visibility evidence, and pays an image contract
against a cold pre-unit adversary. Normal execution and recapture followed by a fresh
probe both earn completion through the real VFX readers without a model critic. Replacing
captured bytes after freeze refuses before canonical external spend and publishes no
candidate. Additional injections cover unpaid evidence and payment changes during probe,
during freeze inference and after freeze. This proves the unit seam, not full-shot quality
or readiness of qualitative/generated construction routes.

Fixture preparation exposed a test-helper defect: unit-local geometry/illumination
capabilities were being copied into the global capability map, whose vocabulary contains
only camera. The helper now projects only declared global capabilities; unit capabilities
remain on the real materialized unit and pass ordinary validation. No production gate was
relaxed to admit the fixture.

All 78 focused tests passed, with 42 existing Pillow `getdata` deprecation warnings.
Complete-source Ruff and diff checks passed. No SDK change or paid inference was needed.

The full frozen-source regression passed all 3,390 tests in four isolated processes
(848 + 848 + 847 + 847), with 64 existing Pillow deprecation warnings. Final complete-source
Ruff and diff checks passed. The next gate is default raster dispatch through layer and
driver completion; this unit result does not claim that gate or complete Claude removal.

## Production executable image routing and layer/driver gate

The real two-layer fixture exposed two integration defects: duplicated image ownership
metadata overflowed required context, then the evaluation receipt's script candidate
disagreed with the layer checkpoint's canonical render. HIR-0253 records both fixes.
The v2 image observation carries compact payment/pixel identities with the full report
locator and digest; registry/report ownership checks remain complete. Native image evaluation
now names the canonical primary render, while its replay closure independently binds script
bytes. The fixed context cap, checkpoint derivation and completion readers are unchanged.

Production unit dispatch now shares the explicit native executor's eligibility predicate.
It selects Flynn for procedural executable units, including EEVEE image units, with coverage
at every judge point and no provisional visual requirement. Unsupported units keep their
existing migration path. Native errors never trigger another engine.

The integration gate executes an accepted camera layer followed by a scene predecessor and
an emissive image unit, with authored order deliberately reversed from dependency order.
Real Blender captures, measured payments, probe/freeze/canonical replay, unit checkpoints,
completion receipts and composed layer publications all execute. A second test uses the
production unit dispatcher through the driver's subprocess seam, then substitutes the
composed image script and proves that a successful child exit cannot advance the driver.
The failure preserves the accepted camera bytes and all existing unit completion records.

The composed image layer still owes an independent look judgment. That judgment is scripted
in these tests, as is unit inference; executable unit checks do not waive it. This is a
transport/publication gate, not proof of visual quality, CLI preflight, live planning or
whole-shot acceptance. No SDK change, ARC testing or paid inference was needed.

All 30 focused routing/capture/layer/driver tests passed in 263.02 seconds, with 25 existing
Pillow deprecation warnings. The frozen-source full regression passed all 3,398 tests in
four isolated processes (840 + 840 + 859 + 859), with 84 existing Pillow deprecation
warnings and the longest process taking 998.38 seconds. Complete-source Ruff and diff
checks passed.

Next is the remaining critic transport in `builder/critic.py` and its Claude message
helpers in `critic_focus.py`: native image inputs, structured verdict submission, current
execution guards, bounded attempts and neutral usage. VFX qualification, panel citation,
evidence reconciliation and layer acceptance stay authoritative. Solid, qualitative and
generated units and a clean installation without Claude remain separate migration gates.

## Native critic observation capability

Added `agents/critic_transport.py`, with no Claude dependency, for one bounded opinion
through Flynn's native image inputs, structured tool, guards, SQLite journal and usage
records. Extracted the unchanged critic schema into `domain/critic_verdict.py`; both
transports consume it, and the obsolete builder schema facade export is removed.

The capability verifies original image snapshots in reference/candidate/focus/motion/prior
order and reopens their source bytes around inference and submission. It binds current
caller authority and the owning run, caps context at 24,000 characters, and permits only
one inference plus one verdict submission with no external action or state commit.
Its output cap is 8,192 tokens and its deadline is 180 seconds. Errors and cancellation
propagate without retries. The journal and report retain usage even when submission fails.

Reports include scope/phase, requested model, exact prompt/context/schema digests and
image identities. Configured provider/model metadata remains in the SDK's usage records.
Neither is a qualification credential. Every returned/report result explicitly grants
no qualification or acceptance authority. The existing production critic is not switched.

The qualification prerequisite is concrete: `domain/work_units/claims.py` validates
`judge_model`, `prompt` and `evidence_shape` against the claim's artifact, but runtime
invocation never compares its own identity to those fields. The composed layer's implicit
look judgment also needs an explicit binding for the new runtime. Native transport uses
verified original image bytes, so it cannot silently inherit qualification for the former
Claude JPEG preprocessing either. Next, derive and verify that invocation binding before
allowing native opinions into reconciliation and layer acceptance; keep domain judgment
and receipt publication in VFX.

All 33 focused transport, typed-critic and execution-guard tests passed in 6.52 seconds.
They include a real DeepSeek adapter over mocked HTTP, malformed/ungranted submissions,
changed images/authority/run, timeout/cancellation spending, invalid input refusal and an
isolated import with Claude blocked. Twelve prior/current schema combinations compare
identically. The frozen-source full regression passed all 3,418 tests in four isolated
processes (845 + 845 + 864 + 864), with 84 existing Pillow deprecation warnings. The
longest process took 1,019.91 seconds. Final complete-source Ruff and diff checks passed.
No SDK change, ARC testing or paid inference was needed. Flynn's guard context already
exposes the operation id and its public records retain the associated inference usage.
Subsequent inspection found that this usage carried requested model identity only; the
next increment below supplies the missing provider-reported identity.

## Reported model identity prerequisite

Inspection of Flynn's DeepSeek adapter found that `InferenceUsage.model` always used
the configured model, discarding the response's model field. A qualification check
against that value would merely compare two requested identities.

SDK main commit `b39431f` adds independently nullable `response_model` to neutral usage.
It retains the provider's value even on rejected responses and leaves absent or invalid
values unknown. Existing immutable usage JSON transports the new field; historical
absence never implies a match. Matching and qualification policy remain harness-owned.

The native VFX critic now binds requested provider/model in its input record and checks
the exact operation's durable usage before submission. Wrong provider, different
request, different response model, and missing response model refuse while retaining
spending. Scripted observations remain explicitly not applicable. Reports expose the
identity check separately, with qualification and acceptance still false. There is no
claim that a provider's reported name proves which weights it served.

The production switch remains gated on exact prompt/evidence qualification and explicit
composed-layer look authority. No production look obligation or receipt writer changed.

The next implementation must resolve these source-backed gaps before dispatching a
production critic through the native transport:

- `validate_qualification` verifies an artifact against the claim's declarations, not
  the current invocation. Qualification admission must check the selected source bytes
  and the effective model, instructions, response contract and image representation
  before spend, then retain the same source checks around consumption.
- `_composition_judge_unit` synthesizes judgment-debt claims as qualified without
  attaching the normal qualification record. A broad layer look judgment can instead
  have no composition unit at all. Neither case may inherit a constituent unit's
  executable success as judge qualification; each needs explicit owning authority.
- `critic_prompt` describes a motion strip as the third image, while focus panels can
  precede it. Native prompt image references must come from the same ordered slot
  manifest used to attach images before that prompt/evidence layout is qualified.
  The shared manifest increment below resolves this layout prerequisite.
- Qualification of a prompt program needs an explicit distinction between its fixed
  instructions and bounded per-invocation facts. Recording a fully rendered prompt
  digest is useful provenance, but does not demonstrate that a calibration suite
  covers a different invocation. Do not substitute a caller-provided version label
  for that contract or automatically grant qualification after a structured response.

The identity increment passed all 208 SDK tests and strict type/lint checks. SDK commit
`b39431f667b5b2b21c32b36fb310082e959cc60c` was installed from SSH into both VFX and ARC;
ARC's required offline gate passed 522 tests in 92.39 seconds and its updated lock was
preserved. VFX's 38 focused critic and execution-guard tests passed in 5.35 seconds.
The frozen-source full VFX regression passed all 3,423 tests (847 + 846 + 865 + 865),
with 84 existing Pillow warnings. The longest process took 1,164.90 seconds. Complete
`src` Ruff and final diff checks passed. No paid inference was used.

## Shared critic image manifest

The critic's previous image layout had three concrete inconsistencies: focus panels
preceded a motion strip still labelled "THIRD", missing declared motion/prior files
were silently omitted, and focus panels beyond two were dropped while their metadata
remained in the prompt. These are harness input defects, not failures of model judgment.

Both transports now use the pure `agents/critic_images.py` manifest compiler. It
validates the closed slot counts and explicit paths, and assigns the labels each
transport uses with its actual attachments. The production prompt refers to semantic
labels; its attachment-order text is generated from the same compiled list as the
encoded images. Focus contract metadata is preserved and prior images remain context
only. The production transport proves every declared image exists before encoding any
of them and refuses excessive focus inputs instead of truncating them.

Native image shape v2 records the labels alongside source and input digests. Tests
compare the six-image manifest against the actual mocked HTTP request order, exercise
zero/one/two focus panels before motion, and prove missing evidence refuses before
provider invocation. This is a changed qualification input, not a qualification result.
Explicit prompt-program qualification, judgment-debt authority and the composed layer's
look qualification remain prerequisites to switching the production critic to Flynn.
No SDK change or ARC test is needed for this VFX-owned contract.

Validation: all 57 focused critic, medium-rubric and execution-guard tests passed in
6.49 seconds. The frozen-source full suite passed all 3,437 tests (850 + 850 + 869 +
868), with 84 existing Pillow deprecation warnings; the longest process took 1,000.14
seconds. Complete-source Ruff and final diff checks passed. No paid inference was used.

## Explicit native qualification admission

Native critic execution can now admit selected parsed qualification claims. It verifies
their exact source artifacts, existing error-rate budgets and suite bindings, then checks
their new `native_invocation_sha256` against the actual invocation before reserving
inference. Old artifacts without that binding refuse native admission. Unqualified
observation mode remains available for diagnostics/calibration, with no claimed authority.

The fingerprint includes full prompt text, ordered scope and claim semantics, response
schema/tool description, and image role/layout, format, dimensions, colour mode, frame
count and detail. Scope and claim facts enter bounded required context instead of being
smuggled into the model through the SDK's unbounded initial audit state. The initial SDK
state is now a fixed observation-only marker. Exact image source hashes are checked
separately, allowing different pixels within the same qualified protocol.

SDK main `189a4776559a09120111f6557ee14731993874c6` adds a pure configuration description
and durable fingerprint derived from the actual dispatched payload. VFX binds provider
system instructions, thinking/effort and output ceilings before spend, then compares that
actual fingerprint before consuming a verdict. Tests change settings between admission
and dispatch and restore them afterward: the journal still exposes the substituted
configuration, the verdict refuses, and spending is preserved. The SDK also preserves
requested model identity from the dispatched payload if adapter fields later change.

The SDK's 218 tests and strict type/lint checks passed; the strengthened journal and
configuration checks passed 28 tests. Both consumers installed the pushed commit over
SSH. ARC's required offline gate passed 542 tests in 116.59 seconds, and its updated lock
was preserved alongside existing work.

Selected qualification is an artifact admission check, not a new calibration result or
proof that an observed claim passed. Implicit layer-look/debt placeholders refuse this
API. Production routing still needs an owning wrapper that derives complete qualification
from selected authority, a calibrated native artifact for that invocation, and
the existing VFX evidence reconciliation and receipt acceptance. No live model was
qualified or invoked by the fixture tests, and the production critic is not switched.

Validation: 87 focused checks passed in 9.79 seconds. The finalized-source full VFX
suite passed all 3,467 tests (858 + 857 + 876 + 876), with 84 existing Pillow warnings;
the longest process took 1,075.06 seconds. An earlier full run was deliberately stopped
to add image dimensions/mode/frame-count binding, then replaced by this complete run.
Complete-source Ruff and final diff checks passed. No paid inference was used.

## Artifact-free native calibration trials

Tracing layer/debt ownership exposed a prerequisite: the native observation path omitted
claim semantics unless it first admitted an existing qualification artifact. Added explicit
`calibration_claims` so a model trial can exercise exactly the invocation later admitted,
without circularly requiring that artifact. Admission and calibration are mutually
exclusive; shared validation retains explicit typed scope and bounded context.

Calibration records its invocation profile/digest and verifies configuration against
durable dispatched usage. Changed claims/settings, missing configuration metadata and
scripted adapters refuse. Successful trials remain unqualified observations and authorize
no state commit or VFX acceptance. The request-equivalence test uses actual adapter
serialization with mocked HTTP, not a live calibration result.

Validation: all 99 focused qualification, transport, image-manifest, rubric, typed-critic
and execution-guard tests passed in 9.42 seconds. This change stays within the native critic
concern and adds no package/import dependency changes; the full suite was not rerun.
No SDK change, ARC test or paid inference was needed. Independent measured-suite evaluation,
selected layer/debt qualification and the production critic cutover remain outstanding.

## Independent native calibration evaluator

Added a read-only VFX evaluator for labeled native model trials. Each trial binds report
bytes and the canonical Flynn inspection snapshot; the evaluator reopens original images,
verifies terminal model identity/configuration, and derives its verdict from the submitted
tool call and recorded observation. It reconstructs the claimed prompt, scope, tool and
image representation from the actual request. Changing and rehashing only a report cannot
change what the evaluator measures.

All six architecture-defined control types and at least two distinct trials per case are
required. The evaluator derives all five rate numerators and denominators, including
pairwise repeatability and irrelevant-change comparisons. Score changes within the same
pass band remain errors. Unusable-reference mistakes and missing owned defect citations
are additional explicit blockers. Duplicate trials, missing coverage, mixed invocation
profiles, changed source bytes and invalid baseline labels refuse evaluation.

The new `vfx-harness.critic-calibration-evaluation/v1` report cannot be consumed as an
admission artifact: qualification and acceptance are always false. Labels and declared
irrelevance remain authored ground truth, and the minimum sample count is a mechanical
coverage rule, not statistical proof. Reviewed held-out labels, qualification publication,
layer/debt ownership and production critic cutover remain outstanding.

Focused validation passed all 87 evaluator, admission and transport tests in 40.80 seconds.
The metric tests run the actual DeepSeek adapter against mocked HTTP and reopen its real
SQLite records, including injected bad votes, scope leakage and score instability.
No paid inference or SDK change was needed; ARC was not touched.

Frozen-source full validation passed all 3,498 VFX tests (865 + 865 + 884 + 884),
with 84 existing Pillow deprecation warnings. The longest test process took 1,099.50
seconds. Complete-source Ruff and final diff checks passed.

## Measured qualification publication

Added the VFX publication adapter for an exact hash-selected calibration suite. It
re-evaluates the recorded trials, refuses failed measurements, and writes the evaluation
plus a derived candidate qualification artifact under the owning run. The source selection
and owning-run check remain current through return; no selected plan or claim is mutated.

Native admission now requires the exact measured claim id and `calibration_proof` binding
the suite request and evaluation. It re-derives the proof from journal/image sources through
consumption. Asserted-rate artifacts without this proof refuse, and editing/re-hashing an
evaluation cannot replace measured results. Source substitution during inference preserves
usage and refuses observation consumption.

Positive admission fixtures now produce actual offline model-adapter journals and publish
measured artifacts, replacing their hand-authored pass-rate fields. The 87 admission,
calibration and transport checks passed in 190.16 seconds; the final publication-specific
checks passed all 14 tests in 33.16 seconds. No paid inference or SDK change was needed,
and ARC was not touched.

This publishes a candidate credential, not reviewed label authority or a selected layer/debt
qualification. Reviewed real-model calibration and the owning production wrapper remain
cutover gates; no production critic route changed.

Final frozen-source regression passed all 3,512 VFX tests (869 + 869 + 887 + 887),
with 84 existing Pillow deprecation warnings. The longest process took 1,321.25 seconds.
Complete-source Ruff and final diff checks passed.

## Binding measured qualification to composed judgment debt

Composed judgment debt now derives typed `Claim`/`EvidenceBinding` values with the same
identity, statement, subjects, controls, fault owner and debt-owned judge points. They
carry no qualification credential, but their exact semantics can enter native calibration.

An explicit `bind_claim` operation reopens the measured artifact and its sources, compares
the full calibrated semantics and exact suite binding, and returns a new qualified claim.
It changes neither the input claim nor selected plans. The composed judge accepts a
complete explicit debt-claim selection only when ids and semantics match its derived
obligations; partial, duplicate, unbound, untyped or differently owned selections refuse.
Native admission remains the independent proof and invocation check.

The positive test runs an actual composed debt through mocked-provider calibration,
measured publication, binding, composition and native admission, while retaining
`acceptance_authorized: false`. Negative tests cover changed owners, propositions,
subjects, frames, properties, source selection/bytes and revoked authority. Judge-frame
coverage tests confirm that a debt still owes judgment only at its own points.

All 66 focused binding, publication and composed-judgment tests passed in 56.07 seconds.
No SDK change, ARC validation or paid inference was needed. This does not invent an
implicit layer-look qualification or switch production critic routing; reviewed live
calibration and plan-backed selection by the owning wrapper remain migration gates.

The frozen-source full suite passed all 3,531 VFX tests (874 + 873 + 892 + 892),
with 84 existing Pillow deprecation warnings. The longest process took 1,194.29 seconds.
Complete-source Ruff and final diff checks passed.

## Executable Workbench image production path

Native capture now derives the exact canonical medium from the unit, records it on
candidate and adversary, and refuses a worker returning another medium before handle
registration. Procedural executable solid-image units use the default Flynn dispatcher.
The obsolete EEVEE-only routing condition and unused raster eligibility parameter are
removed. Shared payment instructions now require the harness-selected settings.

Initial integration fixtures were rejected by existing authority gates: adding material
capability without required image claims was invalid, and removing an actual emission
capability made image debt lack its declared optical source. The fixtures were corrected
without changing those gates. Workbench measures geometry; the emission fixture still
really emits and declares illumination. The production solid fixture has no look
capabilities and correctly owes no composed look vote; the EEVEE variant retains its
scripted look critic. Both still require independent replay and completion/publication
receipts, and artifact substitution cannot be hidden by a successful child exit.

Validation: complete-source Ruff and public `.venv/bin/vfx --help` passed. The focused
solid capture/lifecycle run passed four tests, including recapture and post-freeze
substitution refusal. Full regression passed **3,535 tests**, with 121 Pillow deprecation
warnings, using unchanged production source throughout. Logs are under
`/tmp/vfx-spike-regression-wtbsetqq`: shard 0, 875 passed (27 warnings); shard 1,
874 passed (37 warnings); shard 2, 893 passed (35 warnings); shard 3, 893 passed
(22 warnings). This includes default solid/EEVEE driver routing and publication
substitution refusal after complete dependent-layer builds.

SDK main and VFX's SSH installation both remain
`189a4776559a09120111f6557ee14731993874c6`. No SDK/ARC changes or paid inference were
needed. These scripted-provider Blender tests do not establish live visual judgment
or full-shot acceptance. The capability matrix records the remaining complete-cutover
obligations, led by the common production critic and its qualification authority.

## Shared production critic inference cutover

The common `_critique` caller now runs through `critic_session` and Flynn's bounded
observation transport. It preserves typed native usage, cancellation, errors, image
identity and exact configured-model admission. The Claude query/retry loop, image and
message adapters, critic options and unused axis-classifier options are removed. Pure
score interpretation moved from the legacy drain into `domain/critic_verdict.py`.
Motion/focus failures now propagate instead of preserving a prior verdict as success.

The production consumer intersects native verified IDs with the selected artifact-bound
claims and requires complete qualitative-claim and requested-axis coverage. Otherwise
the recorded opinion has `needs_human` and an explicit `qualification_gap`, cannot pass
work, and supplies no autonomous visual repair instructions. Independent executable
failures retain their diagnostics and authority. The exact native observation report
and digest are included in the verdict and transcript. Scope without explicit owning
claims, including current implicit layer-look and acceptance calls, remains unresolved;
this migration does not manufacture their qualification.

Validation: 36 focused production/image-manifest/guard/model/rubric tests passed;
12 production failure/import tests passed; 13 consumer-decision and typed-critic tests
passed. These sets overlap and are not an additional unique-test total. An initial
consumer fixture omitted required qualification metadata and was correctly refused;
its explicitly stubbed test metadata was corrected without weakening the parser.
The production tests use the actual native HTTP adapter with a mock provider, current
plan/claim authority, images and SQLite journals. They prove known usage on rejected
responses, cancellation without retry, stale image refusal, configuration refusal and
no acceptance from an unqualified score. Consumer stubs isolate coverage decisions;
they do not constitute real qualification credentials. Existing measured-admission
tests continue to verify the independent proof mechanism.

Complete-source Ruff, public `.venv/bin/vfx --help`, and full regression passed. The
full suite ran against unchanged production source: **3,553 tests**, 121 Pillow
deprecation warnings. Logs: `/tmp/vfx-spike-regression-r60sqoi6`; shard 0 passed
879 tests (22 warnings), shard 1 passed 879 (32), shard 2 passed 898 (40), and
shard 3 passed 897 (27). No paid inference, SDK change or ARC synchronization ran.

An AST inventory still finds 24 production modules importing the Claude SDK: the
remaining builder loops/options/drain/facade, guard adapters, legacy planning and
Blender tool servers, recipe adapter, sandbox hooks and message logger. Full removal
of those paths, native failure-to-stop projection, explicit layer/debt/acceptance
qualification selection and reviewed live validation remain required by the goal.

## Native terminal causes and child-stop preservation

The root run boundary now derives `model_budget_exhausted` from Flynn budget
exhaustion and `model_session_failure` from native inference failures, including
rejected responses. Explicit phase causes remain authoritative. Generic contract
errors and tool proposal rejections stay distinct from model transport failures.
Cooperative cancellation without recorded signal intent is a failed run with
`cancelled_without_intent`; it cannot issue an interruption receipt.

Failure injection also reproduced a publication collision: a child build boundary
published an immutable stop, then the root planning boundary classified the same
exception under its own command and tried to publish a different stop. Both the
normal failure publication and its fallback conflicted, masking the native error.
The inherited boundary now carries its exact successful publication on the original
exception. The root consumes it, preserving the original exception, SDK usage,
child stop bytes and child audit bytes. No existing file is adopted for another
exception. The new inherited-failure test failed on this collision before the fix.

The conservative engineering classification still names the missing phase-specific
recovery contract. Typed SDK failures identify a cause; they do not prove a legal
retry, resume, environment recovery or domain acceptance transaction.

Initial focused validation passed **70 tests** across native failure injection,
run ownership, run artifacts and terminal-cause vocabulary. These are offline
boundary tests, not live provider validation. Complete-source Ruff and public CLI
loading passed. VFX still installs SDK main at
`189a4776559a09120111f6557ee14731993874c6`; the SDK checkout is unchanged and no
ARC synchronization or paid inference was needed.

Full regression passed against frozen production source: **3,574 tests**, 121
Pillow deprecation warnings. Logs: `/tmp/vfx-spike-regression-yxhlw116`;
shards 0/1/2 passed 889 tests each (22/22/40 warnings), and shard 3 passed
907 tests (37 warnings). The full run includes the strengthened child-stop test
requiring unchanged stop and audit bytes through root terminalization. This is a
migration checkpoint; remaining builders, qualification selection and live
end-to-end proof still prevent declaring the cutover complete.

## Native generated construction and canonical asset identity

The native executor now supports the selected `generate` route when all required
claims are executable and every judge point is covered. VFX invokes its existing
staging/promotion operation as a scripted Flynn tool before model execution, records
it in the same SQLite run, and retains pointer, GLB and witness bindings through
dispatch and publication. It checks capacity for the minimum complete remaining
path before staging, including required captures/payments. Model context receives
the selected identity and pinned-import instruction, not generator-selection tools.

The real-GLB fixture exposed two dependency-path defects: replay looked beside a
scratch candidate rather than its canonical locator, and construction independently
used directory `1` where canonical scripts use `01`. The shared pure path leaf and
canonical dependency selection fix both without adopting alternate legacy paths.
An isolated worker probe proved the actual memory-pin/import transport worked.
The retained integration test then passed actual cold replay, unit completion and
image capture, and refused completion after the asset changed. HIR-0254 records
the failing pin trace and ownership decision.

Focused validation passed 32 construction/path/failure tests, followed by 15
neighboring procedural/image and revised budget checks (7 Pillow warnings), then
the staged-byte cancellation test. These sets overlap; they are not a combined
unique total. Initial fixtures were corrected to supply mandatory visibility
evidence and use the supported Blender object lookup; neither production gate was
relaxed. Generation and plate judgment use fixture adapters, while GLB import,
canonical evaluation, capture and completion are real. No paid generation,
live model qualification, SDK change or ARC synchronization was performed.

Complete-source Ruff and public CLI loading passed. The full frozen-source
regression passed **3,588 tests**, with 121 Pillow deprecation warnings. Logs:
`/tmp/vfx-spike-regression-8bo7fjfh`; shards 0/1 passed 888 tests each (27/40
warnings), and shards 2/3 passed 906 tests each (37/17 warnings). An earlier run
was stopped before correcting source whitespace and is not counted as validation.
HIR-0254 is accepted on this evidence. Qualitative builders, simplify, native
builder recipe retrieval, remaining Claude dependencies and qualified live
end-to-end validation remain open; this is not completion of the cutover.

## Bounded native builder recipes

Native builders now register the shared Flynn recipe tool used by planning. VFX
retains query ranking, mutation-role filtering, six-read/three-fragment/12,000-character
limits, exact unit identity and complete-path phase grants. Flynn transports results
and preserves usage and recipe-use observations in the attempt's SQLite journal.
There is no new agent runtime, SDK policy or process-global telemetry accumulator.

Before a result is exposed, the builder uses the same required-context construction
as the next model call to prove the whole observation fits. Oversized fragments are
refused without partial code; the next legal smaller selection can proceed. Feedback
replaces the previous observation. Retrieval cannot consume the calls needed for
write, required image capture/payment, probe, freeze and independent replay.

The first focused run exposed a shared ranking defect: an unrelated verified recipe
received a positive score with zero query matches and masked an out-of-scope refusal.
HIR-0255 records the failing trace and relevance-first correction. The corrected
focused run passed 54 tests, including three actual confined Blender/canonical runs,
and an additional owner-loss check passed. Its initial assertion was corrected to
match Flynn's documented uncertain dispatch after a raised tool: no returned result
or state commit is invented, and the operation remains charged.

Complete-source Ruff, public CLI loading and whitespace checks passed. The full
frozen-source regression passed **3,598 tests**, with 121 Pillow deprecation warnings.
Logs: `/tmp/vfx-spike-regression-0uldrfo6`; shard 0 passed 886 tests (40 warnings),
and shards 1/2/3 passed 904 each (37/22/22 warnings). HIR-0255 is accepted on this
evidence. No SDK change, ARC synchronization or paid inference was needed for this
VFX-only port.

Read-only review also corrected a pending matrix assumption: simplify is a typed
mesh/volume carrier route under ADR-0009, not necessarily decimation of a predecessor.
Its migration must prove real carrier construction, declared prior dependencies and
scope; it must not add an unrequested predecessor requirement to make a fixture easy.

Strict public preflight also passed during this checkpoint, without model spend:
`/tmp/vfx-flynn-cutover-preflight.json`, environment-result/v2,
result digest `67168745545f7a85cc7c2372a53931a0401ad4201f363e1a5abb6372952d4f1e`.
This verifies the current environment checks, including confined Blender and the
plan-consumer directory primitive; it does not verify DeepSeek authentication or
qualitative capability. Installed package metadata still selects SDK `main` commit
`189a4776559a09120111f6557ee14731993874c6`, non-editable.
