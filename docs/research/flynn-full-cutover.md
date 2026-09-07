# Full Flynn runtime cutover

The owner requires complete removal of Claude and the Claude Agent SDK. Flynn is the
single target runtime. Extend Flynn where needed; do not emulate Claude's API or retain
an engine selector. SDK changes land on main and consumers install main over SSH.

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
