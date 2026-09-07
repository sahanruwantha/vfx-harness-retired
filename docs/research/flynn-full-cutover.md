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
