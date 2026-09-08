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
| Generated construction | Generic confined ProgramWorker exists; structured external tools exist | Asset generation belongs to typed builder construction, not the retired standalone asset command | Port generation dispatch and pinned asset dependencies into native tools; verify generation failure, source substitution and cold replay |
| Unit, layer, debt and acceptance critics | Native bounded verdict transport, exact configuration and usage records | Production critic still uses Claude; native calibration/publication/admission mechanisms exist | Replace critic invocation while retaining citations, focus/motion inputs, executable reconciliation and qualification checks |
| Composed layer look and judgment debts | Native images and verdict observations | VFX derives exact typed debt claims; measured qualification can bind them explicitly | Select current plan-backed qualification for exact claims; retain independent layer look, replay groups and receipts |
| Shot acceptance | Native critic mechanism available | VFX replays accepted chain and publishes typed acceptance outcome; critic dependency remains indirect | Migrate shared critic and prove complete passing moment coverage with qualified live judgment |
| Blender, recipe and planning tool registration | Native tool schemas, validation and structured text/image results | Some shared operations already native; legacy MCP decorators/servers remain | Remove obsolete adapters after remaining consumers migrate; preserve semantic scope and confinement |
| Logging, configuration, installation and preflight | Provider-neutral usage/identity/termination, known/unknown/not-applicable accounting | Native session reports exist alongside Claude message readers, credentials, defaults and dependency | Remove all active Claude integration and engine selection; prove fresh install, provider preflight and honest usage reporting |

## Retired utilities and deletion scope

`agents/asset_builder.py` and `agents/distill.py` are fail-closed retired entry points,
not active agent roles. Do not reintroduce their old workflows as part of the runtime
migration. Generated assets still execute through typed builder construction. Recipe
lookup remains an active tool; recipe verification remains executable evidence.
Unused axis-classifier options and distillation prompts/reexports are cleanup candidates
when their legacy builder modules are removed.

## Next shared critic migration boundary

`agents/builder/critic.py` is the common production invocation used by unit evaluation,
composed layers and acceptance. Replace that invocation with `critic_transport.execute`
and preserve the existing deterministic reconciliation around it. Native transport
already retains failure/usage/termination and checks exact image and configuration
identity. VFX still must:

- Select typed claims from current authority and the exact judge point. Pass only
  measured, admitted claim IDs into reconciliation; the legacy designation alone is
  insufficient proof.
- Bind the live unit/layer/acceptance execution guard throughout preparation, inference
  and result consumption. Retain original focus/motion/prior image order and all
  declared required inputs.
- Supply the actual role prompt and provider configuration for qualification. Replacing
  the invocation must not silently reuse Claude calibration or its model-noise estimates.
- Give composed layer look and debts explicit selected qualification authority. The
  existing implicit `layer:<id>:qualified-look` receipt row is an obligation, not a
  measured credential. Do not remove that obligation to bypass qualification.
- Remove the superseded query loop, options, Claude image/message helpers and retry
  behavior in the same migration; native failures propagate with their journal intact.

These are identified remaining implementation obligations, not completed behavior or
permission to relax qualification gates.

## Shared SDK capability decision

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
