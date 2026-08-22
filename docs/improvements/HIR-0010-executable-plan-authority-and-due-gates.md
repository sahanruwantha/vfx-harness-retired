---
id: HIR-0010
title: Make published plan authority executable and bind deferred requirements
status: proposed
introduced_in: unreleased
date: 2026-08-22
failure_class: published_plan_not_executable_and_brief_obligations_remain_prose
mechanism: selected_bundle_consumers_typed_meta_records_and_dependency_due_gates
adr: ADR-0004
---

# Make published plan authority executable and bind deferred requirements

## Observed failure

Planning run `20260821T161447Z-db0932` published clean bundle
`176a7495d112f50a7b3eea889633b181e8d4c27c1546d8d0f73da51476d41a7b`, but build and
standalone evaluation still read the older shot-root plan generation. Starting Layer 1
could therefore execute different authority from `plans/current.json`.

That bundle contained no `frame_delta` contract for the brief's frame-239 to frame-240
final lock. The promise existed only in prose after three planning attempts. Five
projected-composition warnings also allowed the full-shot camera to seal before any
visual blockout existed.

## Root cause

HIR-0008 made publication transactional without moving execution reads to the pointer.
ADR-0004's typed requirements, obligations, and assumptions were not implemented, so
deferred evidence had no executable lifecycle or due boundary. Composition coverage was
warning-tier and encoded no dependency on the geometry used to prove framing.

A fresh run later exposed a second publication-parity defect: the authoring workspace gate
resolved nested unit plans and spike evidence under `plans/`, but the bundle froze only the
top-level authority records. Standalone evaluation of the selected bundle therefore lost cited
evidence and the first ready Layer 1 plan even though the pre-publication gate was clean.
The same run also showed VERIFY's first gate measuring an absent `plans/global.md` after the
driver renamed the draft to `global.draft.md`; the verifier now seeds the canonical gate path
with the exact draft bytes before its session starts.

The first Layer 1 attempt, run `20260822T040521Z-1cecb3`, exposed the corresponding write-side
defect. The unit-plan resolver correctly returned the ready plan from the selected bundle, but
the JIT planner treated every resolved path as a generation target and gave its model `Write`.
It modified the immutable bundle member before any Blender mutation began. The driver was
interrupted immediately; subsequent authority resolution fails on the member hash mismatch.

After republishing, the next pre-mutation Layer 1 attempt exposed the read-side analogue: a
pathless `**/global.md` glob returned plans from several historical runs. Although the model then
read the correct selected member, requiring it to choose correctly violates singular authority.
That run was also interrupted before its first scene mutation.

The first actual blockout mutation then exposed a repository-reorganization regression in the
authoritative Blender framing diagnostic: `blender/checks.py` looked for dependency-free
`geom.py` one directory too high (`vfx_harness/geom.py`) instead of beside itself
(`vfx_harness/blender/geom.py`). The scene contract passed, but the run was interrupted rather
than accepting a weaker diagnostic substitute.

Once that loader worked, Layer 1 exposed a multi-moment convergence defect: live unit evidence
was filtered to the primary judge frame. `iris_blockout` therefore reported 1/1 contracts passed
after proving only f1, omitted its required f36 contract, and the mutation guard blocked attempts
to test the second camera pose. Canonical evaluation would eventually revisit both frames, but
live convergence had already declared a false handoff and prevented useful repair.

The first replay of the amended atomic Layer 1 bootstrap then exposed inconsistent semantic-role
matching. Scene contracts deliberately selected concrete descendants (`camera.*`,
`iris_blade.*`), while the unit mutation manifest declared their owning namespaces (`camera`,
`iris_blade`). Live evidence therefore passed, but the replay scope audit compared strings
exactly and rejected the same correctly tagged objects. Mutation namespaces now own themselves
and only dot-delimited descendants; similar siblings such as `camera_rig` remain out of scope.

That rejected replay also exposed two state-lifecycle seams: a direct build whose unit report was
`failed` returned normally and was therefore recorded as a passed run, and durable state allowed
`failed -> retryable` but exposed no public operation to apply it. Direct builds now terminate
nonzero whenever any requested unit is unpassed, and `vfx units retry` records the explicit
failure-to-retry transition with reason and evidence while leaving dependants blocked.

After bootstrap acceptance, the missing dependent-unit plan exposed a separate JIT read leak:
the layer planner ran at shot-root cwd under the global staging guard, so relative `**/brief.md`
and `runs/<id>/**` discovery enumerated historical run workspaces. The session was interrupted
before it wrote its target. JIT planning now has a strict read allowlist—authored brief/refs,
explicit decision records, the one selected bundle root, and the exact unit target—and omits the
global-plan completion hook that could otherwise validate shot-root compatibility artifacts.

The clean JIT plan then failed in the model-free consumer gate because the view linked only the
plan Markdown, while view validation treated every linked plan as an immutable bundle member and
could not see the adjacent JIT authority sidecar. Consumer views now link the plan and sidecar as
one pair; validation proves the link resolves to the exact shot-root target, the sidecar names the
selected bundle hash and relative path, and the plan bytes match its recorded SHA-256.

The first mechanism-detail build attempt then showed that the builder's selected-plan guard
blocked shot-root `plans/` and historical runs but not legacy top-level contracts such as
`scene_checks.json`. The session read a stale 420-line contract set instead of the selected
bundle's 249-line member and was interrupted before semantic mutation. Builders now reject every
shot-root compatibility plan/contract artifact while continuing to allow authored inputs, the
selected bundle, and their active run evidence.

With both moments finally active, canonical replay revealed that the finalized housing-only
script retained its temporary measurement rig and camera. Those objects had no semantic roles,
so role-pattern validation could not see the scope leak; the next camera unit inherited them.
Revoking that checkpoint then exposed the plan defect beneath the scene defect: the blockout-only
unit was required to pass projected bbox checks before the camera existed, while its declared
scope explicitly prohibited a camera and its camera-owning dependent could not start until the
blockout passed. The composition gate proved a dependency edge without proving that the evidence
producer itself had a camera available. The planner had been driven into that workaround by a
second gate defect: a direct multi-frame composition context required every bound per-frame bbox
contract to match each judge frame, rather than validating the set once and selecting its matching
member per frame. No direct context with more than one frame could pass that predicate.
A corrective fresh plan was then stopped when its isolated workspace omitted the newly recorded
amendment and durable plan resolutions. The isolation boundary correctly excluded prior plans and
runs, but it also excluded the two explicit cross-run decision channels named by ADR-0004, causing
the planner to report that no prior evidence existed.
On the next attempt, the planner's spike tool invoked the raw `blender` argument and hit an
unusable `/snap/bin/blender` launcher, even though the warm Blender boundary already smoke-tested
candidates and selected the runnable `/snap/blender/current/blender` binary. Planning and building
therefore disagreed about whether the same installed Blender was available.
The corrected attempt reached contract measurement and then received a dated provider response:
"specified API usage limits" with a future access-restoration time. The resilience classifier did
not recognize that wording as terminal and performed a guaranteed-to-fail retry.

Fresh planning run `20260822T094204Z-8c2aa5` then exposed two lifecycle defects before Blender
mutation. First, the spike tool retained exact scripts, output, and renders only under run scratch,
outside the isolated authoring workspace and immutable bundle. VERIFY could reproduce the result
but the gate could not resolve it, so the model removed the citation and published a weaker prose
restatement. Second, the plan classified Layer 1's provisional radius/standoff calibration as
assumption A9 due before the very unit whose executable bbox contracts were meant to settle it.
Build run `20260822T104527Z-d33edd` correctly failed closed at that boundary with no model cost,
but the schema could express only entry gates, not evidence produced at unit completion. Nine
later obligations had the same circular form: their entry gates required contracts owned by the
gated layer itself.

Two subsequent clean publications exposed the remaining requirements-register failure in two
distinct forms. Bundle `e569660145e463c3...` omitted the brief's B7 final-lock clause from
`requirements.json` entirely, while bundle `ea495a1b0dfa20ec...` registered that exact line but
resolved its two-frame temporal promise only to a single-frame core-brightness image check. Both
passed the old closure gate. Register-to-known-id closure therefore proved neither source-clause
completeness nor evidence suitability; the recurring 239→240 gap was not a planner-memory problem.

Fresh bundle `4c0fe3883ddb551d...` finally bound both the exact-return and 239→240 contracts,
but its Layer 1 authority exposed the same prose/execution split on a human-approved camera
decision. Durable resolution A2 named "the 16-keyframe set described by the corrected global
plan" without embedding that set. Fresh-plan isolation correctly withheld the old plan, so the
planner reconstructed different coordinates. It then instructed the sole camera-owning Layer 1
unit to key only f1/f36 and defer the other fourteen keys to later layers, while `layers.json`
gave no later unit mutation authority over `camera` or `cam_rig`. The published gate was clean
because it could validate declared claims but could not prove adoption of the operative values
inside a prior human decision. Starting Layer 1 would therefore have made the approved full-shot
spine impossible by construction.

The replacement draft run `20260822T142749Z-581b29` exposed two adjacent forms before
publication. First, it calibrated image checks against absolute `/tmp/adversary_*.png` paths;
those files existed only in the authoring process and were absent from the run artifact index,
so the proof could disappear while its numeric claim remained. Second, its ignition unit removed
its temporal-evidence label after write-time validation noticed that no temporal contract was
bound, even though the brief still required a traveling pulse front and timed response. The run
was deliberately stopped before publication because its process had imported the gate before
these newly discovered invariants were loaded; its partial workspace remains failure evidence.

## Decision criteria

- A selected bundle is singular authority once its pointer exists.
- Normative brief requirements are content-addressed and close through typed records.
- Deferred evidence blocks at a dependency outcome.
- Camera acceptance requires projected context that already exists in the unit DAG.
- Pointer-less archived fixtures retain a named, temporary compatibility window.

## General mechanism

- Bundles require `requirements.json`, `obligations.json`, and `assumptions.json`, plus a
  harness-authored provenance member covering all staged authored-input hashes.
- Planner writes to staged `brief.md`, `refs/`, and the workspace marker are denied;
  in-process plan evidence tools independently reject paths outside their workspace.
- Plan loaders resolve and verify `plans/current.json`. A run-scoped consumer view lets
  the folder-shaped deterministic gate inspect immutable bundle bytes beside authored refs
  without copying authority back to shot root.
- Requirements cite the full brief hash and line span, then resolve to contracts,
  obligations, or an explicit typed decision. The gate checks cross-record closure.
- Human-approved resolution values are self-contained `values.contract` records. The candidate
  must copy every field exactly into a scene contract carrying the same `decision_id` and bind it
  to a required claim in the owning unit. Exact animation decisions use the generic
  `keyframe_schedule` primitive, which checks every scheduled semantic property and rejects
  missing or extra keyed frames; prose references to an unavailable prior plan cannot substitute.
- Planner checks may name only candidate-relative adversaries already persisted in authored refs
  or `plans/evidence/`; absolute, escaping, missing, and process-local `/tmp` paths block. Explicit
  brief motion language independently requires a required temporal contract, so changing a unit's
  evidence label cannot downgrade chase, stagger, propagation, dependency-order, fracture, return,
  or continuous-camera laws into static proxies.
- The gate deterministically enumerates every substantive brief body paragraph, list item, and
  table data row and requires each source clause to overlap a register citation. It additionally
  derives terminal N-frame hold windows from explicit brief language and requires the cited
  resolution to reach a required rendered `frame_delta` contract over that exact terminal window;
  a single-frame look proxy cannot satisfy a temporal promise.
- Obligations declare `before_unit`, `unit_completion`, `before_layer`, or `before_acceptance`
  due gates. A completion obligation is automatically discharged only after canonical unit
  acceptance and only when every exact evidence binding it names passed. Assumptions remain
  human-decision records and cannot use the machine-completion gate. The plan gate rejects an
  entry gate that depends on a contract produced by the gated layer or non-upstream unit.
  Append-only resolutions remain keyed to the exact bundle hash.
- Layers declare typed `evidence_domains`. A `projected_composition` layer binds direct
  bbox contracts or depends on a blockout unit checked at the same frames. Missing context
  is blocking.
- Publication identity is returned through `PlanLoopResult`, so terminal run metadata
  retains the pointer, bundle, and content hash.
- Every generated JIT unit plan carries a sidecar binding its content hash to the selected
  global bundle hash. Reuse after a global replan or an untracked edit fails closed.
- Every nested `plans/**/*.md` member present at publication is frozen at its workspace-relative
  path. Bundle-aware execution and consumer views trust those bytes through the bundle manifest;
  only unit plans generated after publication use shot-root sidecars.
- A successful global-plan spike deposits a harness-authored Markdown record under
  `plans/evidence/spikes/` containing the full script, full Blender output, content hashes, exit
  status, and a bundled render when present. The tool returns that workspace-relative citation;
  publication freezes the record and image while retaining run scratch as non-authoritative
  forensic detail. JIT sessions without a global workspace marker cannot write this surface.
- Two-pass verification keeps the immutable draft audit file and also seeds `plans/global.md`
  from it, so write-time gate calls and the verifier assignment measure the same candidate.
- A retained clean candidate can cross a later publication migration only through
  `vfx plan --promote-run <run-id>`. Promotion is a model-free, new-run transaction that checks
  source terminal status and authored-input identity, copies the complete candidate surface,
  reruns the current deterministic gate, and publishes a new immutable bundle. It never repairs
  or reselects the source bundle in place.
- A ready unit plan already present in the selected bundle is consumed model-free; the JIT
  planner is not started for it. When a genuinely missing unit requires JIT authoring, its
  PreToolUse contract permits mutation of exactly the declared shot-root target and always
  denies every content-addressed bundle path. Bundle hash verification remains the final
  fail-closed boundary if either control regresses.
- Layer-plan completion reports the selected bundle member or its exact JIT authority sidecar;
  it no longer rewrites the legacy shot-root `plan.provenance.json`, which represented neither
  the unit plan nor its selected global generation.
- Build-session filesystem reads may enter `runs/` only through the currently selected bundle
  or the exact active run that owns the session's journals and evidence. Pathless Grep and
  recursive Glob patterns capable of enumerating historical runs are denied; normal
  authored-input, build, asset, and state reads remain available.
- Blender judgment-free checks load their dependency-free geometry implementation from the
  co-located `blender/geom.py`; a no-Blender import regression test exercises the same path the
  warm worker uses before a build can depend on it.
- Live convergence aggregates bindings from every required claim in the active unit, across all
  declared moments, and the static evidence producer probes every frame named by those bindings
  before deduplicating by contract id. Per-frame critic scoping remains narrow, while the
  mutation guard cannot close merely because the primary judge's subset passes.
- Canonical replay and the model-free revalidation fast path snapshot scene objects before a
  scoped unit script. Every newly persisted object must carry a semantic role matching that
  unit's declared role patterns; untagged measurement scaffolds and foreign roles fail replay.
- Checkpoint invalidation is an explicit atomic state transaction: it archives revoked checkpoint
  authority, makes the failed unit retryable, blocks its complete downstream closure, and records
  the reason and evidence. The public `vfx units invalidate` command owns that operation.
- A bbox-owning unit must have a camera in its transitive dependency context. On an empty scene,
  the camera and first measurable blockout may be one atomic scoped unit with direct bbox bindings;
  a blockout cannot claim projected evidence through a camera owned only by its dependent.
- Direct multi-frame composition bindings validate that every named contract is a bbox contract
  and that each judge frame has at least one matching member; they do not require every per-frame
  contract to carry every frame.
- Fresh plan staging still excludes prior plans, builds, questions, and runs, but copies
  `plan_amendments.jsonl` and `state/plan-resolutions.jsonl` as byte-isolated, read-only inputs.
  Their hashes are recorded separately as decision-input provenance, so this is declared
  carry-forward rather than filesystem leakage. Appending a durable resolution does not stale the
  currently selected bundle; it becomes identity-bearing input only for the next plan generation.
- Plan spikes use the same smoke-tested Blender resolver as warm build sessions. Desktop launchers
  that exist but cannot execute in the current process context are rejected in favor of a verified
  binary; the shot plan no longer compensates for infrastructure launcher failures.
- Provider responses naming a reached usage limit or a future regain-access time are terminal,
  alongside authentication, billing, and spend-limit failures; session resilience does not retry
  them as unknown or transient capacity errors.
- Durable state can move to a changed selected DAG through public `vfx units replan`. The
  transaction requires the exact old run and bundle hash, verifies both immutable bundle
  manifests and their `layers.json` hashes, then atomically records added, removed, changed,
  invalidated, preserved, and superseded units. It never discovers authority by scanning runs.

## Rejected patch-level alternatives

- Copy the selected bundle back to shot root: recreates mixed-generation reads.
- Add frame 239 to prose: that promise already survived multiple runs without evidence.
- Leave composition warning-tier: it seals camera authority before framing is falsifiable.
- Infer composition from axis words: new candidates declare a typed evidence domain.
- Manually copy missing members into the old bundle: destroys content-addressed immutability and
  bypasses the same publication path future candidates must use.
- Delete or reinitialize stale work-unit state after publication: erases the dependency and
  checkpoint history needed to prove which outcomes were superseded.

## Validation

- Unit coverage proves authored staged inputs cannot be written, bundle reads remain
  selected after shot-root changes, and incomplete bundles cannot publish.
- A final-lock fixture maps the brief line to an acceptance-due obligation and proves
  acceptance blocks for missing or mismatched evidence, then clears for the named
  frame-delta contract.
- A composition fixture proves camera coverage clears only through its declared,
  bbox-checked blockout dependency.
- Focused verification: `.venv/bin/ruff check src tests`; `.venv/bin/python -m pytest -q
  tests/unit/test_plan_authority.py tests/unit/test_plan_improvements.py
  tests/unit/test_plan_records.py tests/unit/test_planner_outcomes.py` (28 passed).
- Full verification: `.venv/bin/python -m pytest -q` (86 passed), `.venv/bin/vfx --help`,
  and `git diff --check`.
- Current verification after checkpoint revocation, composition-bootstrap, explicit decision-input
  staging, and Blender/provider boundary fixes: `.venv/bin/ruff check src tests`;
  `.venv/bin/python -m pytest -q` (97 passed); `.venv/bin/vfx --help`; `git diff --check`.
- Fresh planning run `20260822T012738Z-36f47e` published bundle
  `67f71c40973a0ee0fc91728d8870b448704765b443cf256db29464f4905d66b1` with zero
  authoring-gate blockers. Standalone evaluation then reproduced four publication-parity
  blockers: two unresolved nested citations, the missing spike artifact, and the missing first
  Layer 1 unit plan. This is the regression fixture for nested bundle members; the published
  bundle remains immutable and is not silently repaired.
- Fresh OAuth-backed run `20260822T071535Z-9e11af` published complete bundle
  `e6bb08cfedc9ddfffa9382cf2bc0c4d4bb50aa5aa3c3c4f20e01392ec8a02530`; its publication
  gate was clean (128/128 fingerprints, 16 executable checks, 39 scene contracts). Standalone
  evaluation then correctly refused the stale Layer 1 work-unit DAG, reproducing the missing
  public replan-application seam addressed by this slice.
- Replan-command verification: focused authority/state suites (40 passed), full suite (99 passed),
  `.venv/bin/ruff check src tests`, both public help surfaces, and `git diff --check`. Applying
  `vfx units replan` moved Layer 1 from bundle `da8156fdbf0c940e...` to `e6bb08cfedc9ddff...`;
  standalone evaluation then returned CLEAN against the selected bundle and revised unit state.
- Dependency-outcome and spike-publication verification: focused record, plan-improvement, and
  plan-authority suites (42 passed). The strengthened standalone gate reproduces all nine circular
  obligations in bundle `c914b5ec0c5a9aa7...`; build run `20260822T104527Z-d33edd` proves the
  unresolved A9 entry gate fails before model spend or scene mutation.
- Requirements completeness and evidence-suitability verification: focused record,
  plan-improvement, and plan-authority suites (47 passed). The strengthened standalone gate
  reports every uncited substantive clause in `e569660145e463c3...` and rejects
  `ea495a1b0dfa20ec...` because its terminal 239→240 hold resolves only to a single-frame proxy.

HIR status remains proposed until a subsequent bundle includes the nested members, the standalone
gate resolves it, and Layer 1 reaches its first accepted empty-scene checkpoint.

## Release and rollback

The previous six-member bundle remains immutable but no longer satisfies current
execution authority. Publish a new plan; do not edit or copy the old bundle. Reverting
this slice restores stale shot-root execution and unbound deferred requirements, so it is
diagnostic rollback only.

## Remaining limitations

- Pointer-less archived fixtures retain compatibility reads until republished.
- JIT unit plans generated after global publication are hash-pinned shot-root files rather than
  immutable, content-addressed publication transactions.
- Register completeness still depends on the adversarial verifier; narrowed verifier
  output and a heterogeneous omission eval remain outstanding.
- Resolution records do not yet authenticate an engine or human writer.
- Publication remains in-process rather than an independent commit subprocess.
- Gate warnings are not yet grouped by owning migration or likely action, so a genuine warn-tier
  defect can still be visually camouflaged by migration bookkeeping.
- No Layer 1 empty-scene checkpoint has accepted this slice yet.
