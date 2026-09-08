# Flynn production cutover capability matrix

This is the current worklist for completing ADR-0012, rather than a claim of full
migration. Historical implementation evidence remains in `flynn-full-cutover.md`.
SDK main is the shared execution kernel; VFX retains production authority. At inventory,
VFX installs SDK main `189a4776559a09120111f6557ee14731993874c6` over SSH.

## Ownership and remaining paths

| Production behavior | SDK mechanism already available | VFX responsibility and current state | Remaining migration and gate |
|---|---|---|---|
| Approach selection | Bounded session, structured result, usage journal | Native role selects approach under harness policy | Include public-command coverage in final cutover proof |
| Global draft, verify, repair | Tools, guards, required context, SQLite session | Native root-owner session; VFX compiler, gate and publication | Remove superseded planning servers/hooks once callers are gone; retain owner-loss and invalid-candidate checks |
| Materialization and rematerialization | Guarded tools, selected observations/images, lifecycle | Native candidate session; exact validation sources and VFX authority transaction | Exercise receipt-backed controller amendment during final E2E |
| Unit planning | Required context, bounded session and guarded writes | Native exact planning claim; independently gated plan/stamp pair | Keep stale claim, partial publication and rollback gates |
| Procedural executable builder without raster | Phase grants, budgets, guarded dispatch, structured observations | Native inspection/write/probe/freeze; independent canonical replay | Preserve failure propagation, scope and checkpoint tests |
| Procedural executable EEVEE image builder | Same execution mechanisms plus image transport | Native cold candidate/adversary capture, payments and canonical replay | Preserve recapture, stale-image and independent completion gates |
| Procedural executable Workbench image builder | No missing generic SDK mechanism identified | Native capture derives the same medium as canonical replay; focused solid lifecycle passes | Passed full 3,535-test regression including default driver routing and source-substitution refusal |
| Qualitative and provisional unit builders | Native session and native critic transport exist | Legacy unit loop still active; VFX owns claim qualification, evidence reconciliation and repair | Wire qualified judgment into complete native build path, with measured qualification and refusal before autonomous blocking |
| Generated construction with executable claims | Guarded scripted preparation, budgets and SQLite records | Native preparation reuses VFX staging/promotion; exact pointer, GLB and witness bindings survive cold replay and completion | Preserve generation failure, witness/source substitution and insufficient-budget checks; live service validation remains owed |
| Simplify construction | Native scoped candidate execution already exists | Legacy builder still executes this mesh/volume carrier route; ADR-0009 requires the derived family, not an obligatory predecessor mesh | Migrate real carrier construction with executable family/scope evidence, exact priors where declared and canonical replay; do not invent a new predecessor requirement |
| Unit, layer, debt and acceptance critics | Native bounded verdict transport, exact configuration and usage records | Shared production inference uses Flynn; measured admission gates scoped qualitative decisions | Complete explicit layer/debt/acceptance credential selection and qualified live validation |
| Composed layer look and judgment debts | Native images and verdict observations | VFX derives exact typed debt claims; measured qualification can bind them explicitly | Select current plan-backed qualification for exact claims; retain independent layer look, replay groups and receipts |
| Shot acceptance | Native critic mechanism available | VFX replays accepted chain and publishes typed acceptance outcome; critic inference is native, unbound scope remains unresolved | Bind acceptance-owned qualification and prove complete passing moment coverage with qualified live judgment |
| Blender, recipe and planning tool registration | Native tool schemas, validation and structured text/image results | Some shared operations already native; legacy MCP decorators/servers remain | Remove obsolete adapters after remaining consumers migrate; preserve semantic scope and confinement |
| Builder recipe retrieval | Native structured tool transport and required-context compiler | Planning and native builders share bounded retrieval; exact unit roles, context admission, complete-path grants and SQLite fragment-use telemetry | Remove legacy registration after qualitative/simplify callers migrate; retain scope, reread, context-overflow and canonical-path budget checks |
| Logging, configuration, installation and preflight | Provider-neutral usage/identity/termination, known/unknown/not-applicable accounting | Native failure causes and exact inherited stop propagation are preserved; session reports coexist with legacy configuration | Remove remaining Claude integration; provide phase-specific recovery contracts where needed; verify fresh install, provider preflight and honest usage reporting |

The shared critic checkpoint passed all 3,553 regression tests. An AST inventory still
finds 24 production modules importing the Claude SDK, concentrated in the remaining
builder loops/options/drain/facade, tool servers, hook adapters and message logger.
This is a remaining-dependency count, not a count of active agent roles.

## Retired utilities and deletion scope

`agents/asset_builder.py` and `agents/distill.py` are fail-closed retired entry points,
not active agent roles. Do not reintroduce their old workflows as part of the runtime
migration. Generated assets still execute through typed builder construction. Recipe
lookup remains an active tool; recipe verification remains executable evidence.
Unused axis-classifier options and distillation prompts/reexports are cleanup candidates
when their legacy builder modules are removed.

## Remaining critic authority boundary

`agents/builder/critic.py` now invokes Flynn through `critic_session` and the existing
native transport. The Claude query loop, options, image/message helpers and error retries
are removed. Native image order, usage, cancellation, source identity and exact invocation
admission remain guarded; pure score interpretation lives in `domain/critic_verdict.py`.
Only IDs verified by native admission and bound to the selected claims reach qualified
reconciliation. Every required qualitative claim and requested axis must be covered;
otherwise the observation cannot pass work or provide visual repair instructions.

Complete production qualification selection remains owed:

- An implicit `layer:<id>:qualified-look` receipt row is an obligation, not a measured
  credential. Keep that obligation and select explicit owning claims/credentials.
- Bind debt credentials through the selected authority path and exact composition group.
- Bind acceptance claims to full-shot scope, current accepted chain and required moments.
- Calibrate the actual role prompt, images and provider configuration. Observer, focus,
  evidence-audit and tie-breaker invocations cannot silently inherit each other's proof,
  nor the historical Claude noise measurement. Missing admission refuses the invocation.
- The current exact prompt fingerprint includes candidate basenames and dynamic
  evidence values. Define a typed qualification protocol separating its calibrated
  rubric from variable observations before expecting one credential to work across
  probe and canonical replay. Preserve exact per-invocation context provenance;
  dropping fingerprints or normalizing arbitrary prompt text is not a qualification
  mechanism.
- Prove qualified live judgments with reviewed controls; fixture passes are not model
  qualification. Unqualified layer/acceptance opinions currently remain unresolved.

## Shared SDK capability decision

Generated construction now uses the existing harness-owned staging and guarded promotion
operation (`unit_construction.resolve_unit_construction`) through Flynn. Canonical path
derivation is shared by scripts and construction pointers, including numeric layer
normalization. Prepared replay binds construction from the canonical locator while
reading candidate script bytes from the exact scratch source. Real GLB import,
completion, source substitution and cold capture are covered by integration fixtures;
external generation and plate judgments remain fixture adapters in those tests.

The installed SDK already provides tools, guarded dispatch, required bounded context,
selected image inputs, distinct observations/evaluations/state updates, durable budgets,
provider identity/configuration, usage and termination. Extend it only when a remaining
production path demonstrates a missing reusable contract. Render-medium selection,
asset authority, pricing, qualification and domain completion belong to VFX.

Every SDK change must pass its own tests and the required ARC SSH installation gate,
then VFX must install and verify that exact pushed revision. VFX-only changes do not
require ARC testing. No session may fall back to another runtime after native failure.

## Completion evidence still owed

- Remove production Claude imports, dependency, hooks, options, credential handling,
  message adapters, fallback paths and installation instructions. Historical ADR/HIR
  evidence can continue to describe the runtime that actually ran.
- Run full source Ruff and relevant failure injections; full regression for changes
  spanning packages or import order. Freeze production source during receipt-sensitive runs.
- Run strict public preflight before paid inference, then bounded public-command E2E
  through planning/building/qualified visual judgment/acceptance and an amendment.
- Reopen the selected journals, usage, receipts and accepted evidence independently.
  Scripted observations do not prove live inference or qualification, and an executable
  fixture does not prove visual quality. Do not manufacture labels or human review.

The goal stays active across intermediate commits until this evidence exists.
