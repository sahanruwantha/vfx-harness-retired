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
