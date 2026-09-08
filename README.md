# VFX Harness

An **agent-driven Blender VFX pipeline**. You write a shot as `brief.md` plus reference
frames. Agents plan the work, build it in headless Blender, and the harness only accepts a
piece when typed contracts, replay, and (when needed) a visual critic agree.

The model proposes. Disk evidence decides. A layer does not pass because an agent says it
looks right.

North star and target architecture:
[`docs/architecture/pipeline-end-goal.md`](docs/architecture/pipeline-end-goal.md),
[`docs/architecture/staged-pipeline.md`](docs/architecture/staged-pipeline.md).
How to run and diagnose a shot:
[`docs/operations/running-vfx-harness.md`](docs/operations/running-vfx-harness.md).

## Words used here

These are the names the rest of this file and the CLI use. They are not department stages.

| word | meaning |
|---|---|
| **shot** | One folder: `brief.md`, `refs/`, then generated plans, build scripts, state, and runs. |
| **brief / refs** | Authored intent. Only these change what the shot is *supposed* to be. |
| **layer** | A department / ownership boundary (camera, form, look, …). Not one giant agent session. |
| **work unit** (unit) | The smallest plan/build/repair job inside a layer. One goal, one mutation scope, one checkpoint. |
| **DAG** | Dependency graph. Units and layers run in the order of `depends_on` edges, not the order they were written in a list. Independent ready units use authored position only as a tie-break. |
| **global plan** | Sparse shot map: which layers exist, who owns which brief clauses, reserved roles, hard constraints. It does **not** design every contract and unit up front. |
| **bundle / pointer** | One immutable plan generation on disk. `plans/current.json` is the single selected pointer. Readers use that generation only — never a mix of old and new files. |
| **materialize** | First time a deferred layer is turned into a concrete unit DAG plus contracts. `vfx plan --layer N`. |
| **rematerialize** | Replace that published layer design because it was wrong, a gate now rejects it, or a new global plan made the old view stale. Matching accepted units stay; changed ones are superseded. Not a wipe of the layer. |
| **gate** | Deterministic checker. No model. `clean` means the plan is structurally executable, not that future scene numbers already pass. |
| **JIT** | Just in time. A later layer stays `jit_deferred` until its upstream outcomes exist and it is materialized. |
| **digest** | Hash of a unit's identity (goal, deps, mutation, contracts, publish/consume). Same id + same digest = same unit. |
| **authority-state transition** | One gate-attested plan/JIT publication that atomically selects authority and moves every affected durable unit binding. Exact unchanged work is preserved; changed and downstream-invalidated work is superseded. There is no standalone state-replan command. |
| **`--discard-accepted`** | Heavy remat authorization: permit a validated replacement to retire accepted orphans. It is not required for comparable current authority and never authorizes an out-of-band state wipe. |
| **checkpoint** | Accepted Blender/script snapshot for one unit. Repair and retry start here. |
| **canonical replay** | Rebuild from an empty scene using the published scripts. Warm live success is not the artifact of record. |
| **composed** | After a layer's units pass, replay them together. Units can pass while the composed layer still fails. |
| **layer finalization** | Claimed public layer boundary: one v2 receipt per actual replay group, one v1 evaluation receipt over the ordered contiguous group prefix, then one mechanically derived v2 terminal receipt. |
| **claim / contract** | A checkable proposition (scene fact, image metric, timing). Required claims AND together; a pass cannot average away a fail. |
| **role / control** | Semantic tags on Blender hosts (`bvfx_role`, `bvfx_control`). Plans select these, not object display names. |
| **write-cluster** | The one mutation family a unit is allowed (role namespace × host × instrument). Mixed families cannot publish. |
| **evidence** | Measurements and renders on disk. Model prose is not evidence. |
| **critic** | Visual judge for qualitative leftovers. It cannot override a passing measurement of the same fact. |
| **hypothesis_falsified** | Builder proved the selected unit authority cannot work inside its scope. Stop. A reviewed replacement must publish through its owning plan/materialization boundary; that publication, not a follow-up state command, derives the exact preservation and invalidation effect. |
| **cannot_express_in_scope** | Repair cannot fix this with the unit's allowed mutations. Remaining repair budget stops. Read the typed stop; do not infer replan, rematerialize, or retry from this label alone. |
| **stop envelope** | The immutable typed reason a run stopped, plus its one legal route and exact evidence. Exit code, `status.detail`, and `contract_gap` are summaries, not dispatch authority. |
| **superseded / blocked / passed** | Lifecycle of a unit: replaced by a newer DAG, waiting on a failed/replaced dependency, or accepted. Not scene-quality scores. |
| **fail closed** | On stale, ambiguous, or incomplete authority, stop. Do not guess or silently use an old file. |
| **`--force`** | Debug only. Never a deliverable. |

Related commands that are easy to mix up:

| you want | command |
|---|---|
| First design of a deferred layer | `vfx plan <shot> --layer N` |
| Replace that published design and atomically move affected state | `vfx plan <shot> --layer N --rematerialize --owner … --trigger … --evidence …` |
| Handle a typed finding that needs new authority | Stop; publish a reviewed replacement through the owning plan/materialization command. No public finding-consumption adapter or automatic controller exists yet. |
| Retry a truncated or now-fixed unit | `vfx units retry <shot> --layer N --unit ID --reason … --evidence …` |
| Release a dead pre-terminal layer finalizer | `vfx finalizations release <shot> --layer N --claim-id lfc-… --reason … --evidence <file> …` |
| Promote a retained clean plan without re-authoring | `vfx plan <shot> --promote-run <run-id>` |

Builders never rewrite plans or widen their own scope.

`vfx finalizations release` is a reviewed finalization-only abort, not a unit retry or
authority replacement. It requires the exact active claim and shot-local evidence, snapshots
every reviewed byte at a SHA-256-derived immutable locator, and archives the complete existing
ordered contiguous replay-receipt prefix. Its v2 request, evidence, and receipt records reject
gaps, substitutions, or inconsistent group counts. It changes no unit row and refuses a terminal
finalization. An ordinary pre-crash critic output is archived as review evidence but is not a
sealed judgment receipt, so the fresh higher-revision claim may need to pay for judgment again.

Layer finalization executes every planned evidence group through a fresh replay. Each
`vfx-harness.layer-replay-receipt/v2` binds that group's required claims, actual deterministic
evidence, exact references, and any group-specific `solid | eevee` render plus auxiliary
captures. A typed replay-stage failure seals a failed group without invented point or critic
evidence. `vfx-harness.layer-evaluation-receipt/v1` accepts only the executed contiguous group
prefix and can pass only when all planned groups ran. The terminal
`vfx-harness.layer-finalization-receipt/v2` derives its result from that evaluation; downstream
outcome readers reopen the evaluation, every group receipt, and every named source byte.

## The flow (and what can happen)

Happy path:

```text
brief.md + refs/
        │
        ▼
   preflight  ── fail → fix env (auth, Blender, config). Not a VFX bug.
        │
        ▼
   GLOBAL PLAN (draft → verify → gate → repair)
        │         fail → repair until clean, stalled, or budget. Do not build.
        │         client question → vfx escalate, then continue.
        ▼
   for each layer, in DAG order:
        │
        ├─ not yet designed?     materialize   (vfx plan --layer N)
        ├─ published design bad? rematerialize (same command + --rematerialize)
        │         fail → no new selected view; previous design stays live.
        │
        └─ for each ready unit, in DAG order:
                 plan unit → gate → BUILD
                      │
                      ├─ pass → checkpoint, next unit
                      ├─ repairable miss → bounded repair from checkpoint
                      ├─ cannot_express / hypothesis_falsified → STOP
                      │         follow only the current typed stop action
                      ├─ interrupt → STOP; no automatic session resume
                      └─ units passed, composed layer failed → STOP (exit 9)
        ▼
   ACCEPT (empty-scene full chain)  ── fail → declared fault-owning layer
        ▼
   RENDER mp4
```

`vfx run <shot>` walks this under one run id and **stops at the first unaccepted
boundary**. A stopped run selects `reports/stop-envelope.json` by digest from
`status.json`; bare child exits fail closed as `harness_defect`. Do not stack later
layers on a broken earlier one. If envelope publication or read-back fails, status marks
the envelope unavailable and no action is authorized. There is no automatic recovery
controller yet.

### 1. Preflight

```bash
vfx preflight --strict
```

Strict preflight prints a secret-safe `vfx-harness.environment-result/v2` JSON record;
`--output <path>` writes the same typed result atomically. During `vfx run`, the run
layout exists before this probe so a failure can publish a run-scoped infrastructure
stop without creating or advancing shot authority. Version 2 embeds the exact probe
specification and revision instead of re-deriving historical probe identity from current code.

**Can happen:** missing auth, wrong env var name, Blender missing, config error. These can
look like a successful empty agent session (`cost=$0`, one turn, nothing built). Fix the
environment. Do not diagnose them as a look or planning problem.

### 2. Global plan

```bash
vfx plan shots/<shot> --until-clean
vfx evals plan shots/<shot>
```

Reads only `brief.md` and `refs/` in an isolated workspace. Publishes a sparse map:
layer DAG, who owns which brief requirement, reserved interfaces, genuine blockers.

**Can happen:**

| outcome | what you do |
|---|---|
| `clean` | Selected authority and its gate-attested state effect commit atomically. Structurally executable — not “the scene already passes.” |
| blocking findings | Repair the plan; rerun the gate. Do not hand-edit bundle files. |
| stalled / budget | Reported as failure. Pay for a new attempt, or promote a retained clean candidate. |
| crash mid-plan | Previous `plans/current.json` stays. The failed run is diagnostic only. |
| `ask_supervisor` questions | `vfx escalate` before the affected layer. |

### 3. Materialize a layer

```bash
vfx plan shots/<shot> --layer N
```

Turns one deferred layer into units, contracts, and claims, now that upstream outcomes
exist. A dependency-root layer may materialize immediately after the global plan.

**Can happen:**

| outcome | what you do |
|---|---|
| publishes | The selected view and its exact durable-state effect commit atomically. Run `vfx build`; only its exact claimed attempt may plan and build the first ready unit. |
| gate / session fail | Nothing selected. No unit state from a partial candidate. |
| max-turns | Failed transaction, not a select. A leftover candidate file is not a plan. |

Do not hand-author placeholder units or edit `state/jit-layers/current.json`.
`vfx plan --layer N --unit ID` is a deterministic refusal; standalone paid unit
planning could leave an orphan planning claim and is now owned exclusively by `vfx build`.

### 4. Build units

```bash
vfx build shots/<shot> --layer N
# or the whole chain:
vfx run shots/<shot>
```

Each unit mutates only its declared roles/controls, then must prove itself: live checks,
and later empty-scene replay of the published script.

**Can happen:**

| outcome | what you do |
|---|---|
| unit passes | Checkpoint. Dependants become ready. |
| executable miss, in scope | Repair from the last checkpoint. Regression restores the snapshot. |
| qualitative miss | Critic on owned axes only; still cannot override a passing measurement. |
| `cannot_express_in_scope` | Stop remaining repairs. Typed finding, not more mutations. |
| `hypothesis_falsified` | Plan is wrong. Stop, review the typed finding, and publish amended authority through its owning boundary. The authority-state transaction moves affected state; there is no follow-up `units replan`. |
| interrupt / SDK failure | Stop. Start a new run from the fault-owning unit. The operator-only `vfx units retry` command requires independent review; it neither consumes the stop action nor emits a receipt. A legacy checkpoint-and-journal row does not authorize automatic session resume. |
| dead pre-terminal layer finalizer | Restart fails on its active claim. After proving the process exited, release only that exact claim with `vfx finalizations release`; accepted unit receipts and checkpoints remain unchanged. |
| unpassed prior | Refuse (exit 6). Do not build on a layer that never passed. |
| brief changed since plan | Refuse (exit 8). Replan first. |
| unanswered question on this layer | Refuse (exit 5). Escalate. |

Never copy an old render or script into a run and call it a resume.

### 5. Rematerialize (when the *design* is wrong)

```bash
vfx plan shots/<shot> --layer N --rematerialize \
  --owner <who> --trigger "<why>" --evidence <path> [--evidence <path> ...]
```

Use this when the published unit DAG or contracts must be replaced: defective design, a
new harness rule that the selected view fails, or a new global plan generation.

It designs against **current global authority**, not against the view being thrown away.
Before the write-ahead intent is selected, a failure leaves the previous design live. After that
point readers fail closed until deterministic recovery finishes the already staged replacement.

Publication and unit-state movement are one gate-attested authority-state transaction:

- an exact unchanged unit binding may keep its immutable completion receipt, even when a sibling
  change gives the containing layer a new generation;
- changed, removed, or downstream-invalidated units become `superseded`, even if they had passed;
- the old terminal layer receipt is revoked whenever the complete layer binding changes; and
- every preservation must continue across each immediate-predecessor coordinator edge. An
  A → B → A sequence cannot recover a receipt that B invalidated.

**Not rematerialize:** a `keyframe_schedule` path miss, a repairable scene fail, or a
finding whose remedy does not change authority. The first two stay in bounded build/repair. The
last remains stopped because no receipt-backed finding-consumption adapter currently authorizes
a same-authority reopen.

### 6. Authority-state transitions and typed findings

There is no public `vfx units replan` command. Global-plan and materialization publishers stage
the proposed authority capsules, complete durable-state before/after images, and exact
preservation/invalidation effect for the gate to verify. Publication then commits the selected
pointer and affected state through one write-ahead, independently evaluated transaction.

An immutable unit completion receipt may cross a changed layer transition only when every
contiguous coordinator edge preserves its exact unit binding and source closure. A terminal layer
receipt crosses only unchanged complete layer bindings. Typed findings remain evidence for a
reviewed authority change; they are not themselves dispatch receipts, and no automatic recovery
controller currently consumes them.

### 7. Accept and render

```bash
vfx accept shots/<shot>
vfx render shots/<shot>
```

Acceptance replays the **whole chain from an empty scene** and judges the approval
moments. A missing or unaccepted layer refuses (exit 7). `--repair` routes a failure to
the layer that owns the failing axis for diagnosis only — it does not mutate unit state
or let a later layer compensate. When all prerequisites are present but a selected
moment fails, full acceptance persists its exact evidence and returns a typed
`human_decision_required` stop.

Successful full acceptance stores `vfx-harness.acceptance-outcome/v1`, bound to the
selected bundle and JIT view, accepted script chain, exact moment set, and evidence
bytes. Full `vfx render` re-resolves and re-hashes that outcome before starting Blender;
missing, failed, or stale acceptance refuses. `--upto` and `--force` are preview modes
and default under the current run's `scratch/previews/`, never `deliverables/`.

## Commands

```
plan → materialize/rematerialize per layer → build units → accept → render
```

| stage | command | what it does |
|---|---|---|
| **plan** | `vfx plan <shot>` | Sparse global map + gate. `--until-clean` loops repair against the gate. |
| **layer design** | `vfx plan <shot> --layer N` | Materialize the layer DAG; a successful publication atomically commits its verified authority-state effect. It performs no paid unit planning. `--unit` is retired. |
| **build** | `vfx build <shot> --layer N` | Claim each ready unit, then own its paid planning and additive build in one exact attempt. |
| **run** | `vfx run <shot>` | Whole driver. Stops on first unaccepted boundary. `--dry-run` previews. |
| **recover authority** | `vfx recover-authority-state <shot>` | Deterministically roll an exact pending authority-state WAL forward; performs no planner, Blender, render, critic, or model work. |
| **recover environment** | `vfx recover-environment <shot> --run-id ID --idempotency-key KEY` | Reverify an exact typed infrastructure stop after external repair; never edits the environment. |
| **release finalization** | `vfx finalizations release <shot> --layer N --claim-id ID --reason … --evidence <file> …` | Archive one exact orphaned pre-terminal claim after review; preserves all accepted unit authority and permits a higher-revision fresh claim. |
| **accept** | `vfx accept <shot>` | Empty-scene full chain; publish an exact typed acceptance outcome. |
| **render** | `vfx render <shot>` | Encode a deliverable only from the current passing acceptance outcome. |
| **inspect** | `vfx inspect <shot> --list-runs` | Read generated runs in the supported order. |

Also: `vfx escalate` (answer plan questions),
`vfx evals plan` / `vfx evals checks`.

## Run outputs

Every shot-scoped producing command writes under `shots/<shot>/runs/<run-id>/`.
Standalone preflight is the typed stdout/output-path exception described above. Read a
shot run in this order:

1. `runs/latest.json`
2. that run's `manifest.json`
3. `status.json` — terminal state and, when unaccepted, the digest-selected stop-envelope pointer
4. `reports/summary.json`
5. `artifacts.json`

For a failed or interrupted run, read and validate `reports/stop-envelope.json` through
that status pointer. `detail`, `terminal_cause`, and the process exit code remain useful
operator summaries, but none authorizes retry, replan, recovery, or escalation. A legal
action in the envelope names the only allowed route. There is no automatic controller.
`recover_environment` is the sole receipt-backed public adapter: an operator may invoke it
after repairing the environment, and every other action remains non-dispatchable.

Open `reports/layers/`, `plan_gate.json`, evidence, transcripts, or checkpoints only when
the summary names a reason. Do not diagnose by listing the shot or grepping every log.

```bash
vfx inspect shots/<shot> --list-runs
vfx inspect shots/<shot> --run <run-id> --json
```

Authored inputs and the accepted build stay at the shot root. Durable cross-run state is
`state/`. Shot-root `logs/`, `renders/`, `.artifacts/`, `.snapshots/`, `.versions/` are
unsupported: no write destination, no evidence authority. See
[`run artifact contract`](docs/architecture/run-artifacts.md).

This is a strict migration. Obsolete schemas are rejected, not translated.

### Model lanes

Native planning, executable building and critic roles use Flynn and the configured
DeepSeek vision model. `VFXH_CRITIC_MODEL` defaults to `DEEPSEEK_MODEL` (or Flynn's
supported vision model). Remaining legacy builder roles still use `VFXH_EXECUTION_MODEL`.

`VFXH_PLANNER_MODEL` selects native Flynn unit planning and defaults to `DEEPSEEK_MODEL`
(or Flynn's vision model). `VFXH_UNIT_PLAN_SECONDS` and `VFXH_UNIT_PLAN_OUTPUT_TOKENS`
default to 600 seconds and 32768 output tokens per claimed attempt. A fresh SQLite
journal records usage and observations; VFX independently gates publication or rolls
back the exact plan/stamp pair. Unit planning requires `DEEPSEEK_API_KEY`. `VFXH_GLOBAL_PLANNER_MODEL` selects the
native Flynn global planner and defaults to `DEEPSEEK_MODEL` (or Flynn's vision model).
Global planning requires `DEEPSEEK_API_KEY`; it uses no Claude session.
Layer materialization also uses Flynn and `DEEPSEEK_API_KEY`.
`VFXH_MATERIALIZATION_MODEL` defaults to `DEEPSEEK_MODEL` (or Flynn's vision model).
`VFXH_MATERIALIZATION_SECONDS` and `VFXH_MATERIALIZATION_OUTPUT_TOKENS` default to
600 seconds and 32768 output tokens per attempt. Unpriced USD caps are refused.
Its session reports point to SQLite journals containing requests, tool observations and usage;
successful finalization still requires the separate VFX publication transaction.
`VFXH_BUILDER_MODEL`, `VFXH_SCRIPT_MODEL`,
`VFXH_ASSET_MODEL`, and `VFXH_DISTILLER_MODEL` override individual roles. Changing the
critic model, prompt, or evidence layout is a new judge configuration: qualify it before
its qualitative verdicts can block. The active lane is stored on the run; a different
lane does not reuse another lane's pixels.

## Fail-closed by design

The pipeline refuses rather than proceeding on unreviewed work:

- building a layer on a prior that never passed (`UnpassedPrior`, exit 6)
- judging acceptance on a partial chain (`IncompleteChain`, exit 7)
- rendering without a current passing typed acceptance outcome (`IncompleteRender`, exit 7)
- building when `brief.md` has changed since the plan was written (exit 8)
- building with unanswered questions that affect this layer or one of its owned axes
  (exit 5); downstream-only questions do not block unrelated earlier layers
- units passed but composed layer verdict did not (exit 9)

Where a debugging override exists, it never produces a deliverable. Forced and partial
renders are previews even when their scripts execute successfully.

## Judging, in short

Decide in this order: scene/interface facts → executable image contracts → isolated
passes → qualitative critic → human only for genuine uncertainty.

- The critic must see the reference and candidate as images. It declares
  `reference_usable` rather than grading a mismatched plate against brief prose.
- Scores are noisy; a verdict next to the pass boundary uses best-of-three (median).
- Canonical replay compares pixels to the accepted plate. It does not re-ask the critic
  whether identical output “looks deterministic.”
- A critic complaint about an exact count, size, or position must cite a **failed**
  check. A claim that contradicts a passing check is discarded.
- `look_capabilities: []` means executable-only: no critic look vote, no beauty score
  on a blank plate.

Live-scene contracts (`scene_checks.json`) measure facts Blender knows: projected bbox,
visibility, counts, nodes, animation. Selectors are semantic roles, not object names.
Builders tag with `bvfx_role` / `bvfx_control`. Before a later layer starts, earlier
persistent interfaces are replayed; a failure stops at `fault_owner`.

Image checks live in `checks.json` (planner, immutable) and `runtime_checks.json`
(builder-proposed, evaluation-only). Mixing origins in `checks.json` is a schema error.
Five gate rules, each from a real escape:

| rule | rejects |
|---|---|
| **reachable** | the reference itself fails it |
| **has teeth** | the named adversary passes it too |
| **beats noise** | reference vs adversary is smaller than the metric's own noise |
| **proof reproduces** | shipped spec does not reproduce its recorded `proof` |
| **not fragile** | a 1% region nudge inverts the verdict |

Builder checks must pass on this unit's render and fail on the harness-captured state
from before the unit ran. The author does not pick the adversary.

```bash
vfx evals checks     # Blender only, no model
vfx evals plan       # no model, no Blender; plan artifacts on disk
```

## Quickstart

Requires **Blender 5.x** on `PATH` (headless) and Python ≥ 3.11.

Flynn is a required dependency, installed from the private
`sahanruwantha/flynn-agents-sdk` repository over SSH with `pip install -e ".[dev]"`.
`pyproject.toml` tracks SDK `main`; no sibling editable checkout is used.
To refresh an existing installation after that branch advances, run
`.venv/bin/python -m pip install --force-reinstall --no-deps "flynn-agents-sdk @ git+ssh://git@github.com/sahanruwantha/flynn-agents-sdk.git@main"`.
CI requires the repository secret `FLYNN_SDK_SSH_KEY`, a read-only SSH key authorized
for the private SDK repository. The key is installed only in the ephemeral runner.

Approach review now uses Flynn directly, with `DEEPSEEK_API_KEY` and
`VFXH_REVIEWER_MODEL` (falling back to `DEEPSEEK_MODEL`, then Flynn's supported vision
model). Text-only model configurations refuse before inference. Its fixed budget is
one model call, one advisory submission, 2,048 output tokens and 90 seconds; required
text must fit 24,000 characters and each selected image must fit 8 MiB. Required context
is never truncated. The current script is required; relevant recipe excerpts are
included whole when space permits. This replaces the former eight-turn file-browsing
session. A recommendation grants no mutation or acceptance authority.

Each review writes a SQLite journal in the owning run's `checkpoints/flynn/` and an
advisory report under `reports/` with source digests, context selection, termination,
neutral usage and explicit `unpriced` status. It refuses a configured
`VFXH_RUN_MAX_USD` because this role does not yet have a price policy. Provider,
validation and cancellation failures propagate after durable recording.
Global draft, verify and repair now use native Flynn sessions as well. The global
planner runs in the root-owner process, uses 32,000-character bounded text context,
and stores complete phase snapshots outside its gate workspace. It requires explicit
wall-time and output-token caps (`VFXH_GLOBAL_PLAN_SECONDS`, default 600, and
`VFXH_GLOBAL_PLAN_OUTPUT_TOKENS`, default 32768). It also refuses unpriced USD caps.
Qualitative/generated-asset builder roles are still being migrated; the Claude dependency
remains for those builder sessions until their native gates pass. Preflight still checks those
remaining roles and confinement; a successful preflight does not verify DeepSeek
credentials. No Claude fallback exists in approach review or global planning.

The production dispatcher selects `agents/builder/flynn_unit.py` for procedural units
whose typed requirements need only executable evidence. Other evidence/construction
routes retain their existing engine. This includes EEVEE and Workbench solid image
units with executable required claims covering every judge point and no provisional
visual requirements.
They capture current candidate/adversary images, pay image debts, and bind the canonical
primary render to the layer checkpoint. A composed layer's independent look judgment
remains required. Qualitative and generated units still await migration.
Flynn failures never fall back to Claude.
The shared production critic now records native Flynn observations with explicit image
identity, usage and termination. Only complete selected claim scope with measured native
qualification can authorize its visual verdict. Unqualified opinions are diagnostic and
cannot pass work or supply autonomous repair instructions. Layer-look and acceptance
qualification selection remain unfinished migration gates; their obligations are retained.
`VFXH_EXECUTABLE_BUILDER_MODEL` defaults to `DEEPSEEK_MODEL` (or Flynn's vision model).
`VFXH_EXECUTABLE_BUILDER_SECONDS`, `VFXH_EXECUTABLE_BUILDER_OUTPUT_TOKENS` and
`VFXH_EXECUTABLE_BUILDER_MAX_STEPS` default to 600, 32768 and 12. Steps include the
scripted canonical invocation; at least four are required. `DEEPSEEK_API_KEY` is required,
and unpriced USD caps refuse before provider construction. The existing layer controller
owns checkpointing and completion. Development callers may still inject `unit_builder`
with explicit inference and limits for offline tests.
Both model and canonical dispatch recheck the live unit claim and exact candidate
revision before tool/external budget reservations. The engine refuses pre-existing,
replaced, deleted or linked scratch candidates instead of adopting them. Writes bind
the requested source digest, and uncertain writes never become a fresh observation.
`inspect_unit` starts from an empty scene and replays the selected prefix at the unit
frame. It reports owned objects and read-only predecessor objects, with a digest-bound
run report containing the exact replay inputs. It never treats a leftover worker scene
as accepted state or grants acceptance from an inspection.
The scripted lifecycle gate runs real confined Blender and the production unit receipt
writers without model calls:

```bash
.venv/bin/python -m pytest -q src/tests/integration/test_flynn_unit_lifecycle.py
```

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env          # set ONE auth variable (below)

vfx preflight --strict
vfx plan  shots/barrel_roll
vfx build shots/barrel_roll --layer 1
```

The CLI loads the repository `.env`; shell-exported variables win.
`VFXH_ENV_FILE=/absolute/path/to/file` selects a different dotenv. Importing the Python
package never loads credentials.

**Auth — check it before you spend anything:**

```bash
vfx preflight
```

Only two names are read: `CLAUDE_CODE_OAUTH_TOKEN` (subscription, `claude setup-token`)
or `ANTHROPIC_API_KEY` (pay-per-token). When both are set the **API key wins**. Prefer
the API key if you care about cost numbers (`MAX_BUDGET_USD`, per-layer stop, eval
deltas): those read `total_cost_usd`, which is real money under an API key.

A plausible wrong name (`CLAUDE_API_KEY`) is silently ignored. A stale token then
yields `subtype=success`, `cost=$0`, one turn, and a limit message as ordinary text. A
build in that state “succeeds” having built nothing. `preflight` names the dead
variable; `empty_success` catches the zero-cost/zero-tool shape at runtime. A *wrong*
key (rather than misnamed) costs wall time: 401 retries with backoff, ~190s per call.

Whole shot under one run id:

```bash
vfx run shots/barrel_roll            # --dry-run to preview
```

It skips layers already recorded as passed, stops at the first failing layer, and
preserves the numeric exit code for CLI compatibility. Recovery meaning comes only from
the child-selected typed stop envelope; a bare exit is reclassified as a harness defect,
not guessed into a retry or replan. This is checkpoint reuse across runs, not automatic
resume of an interrupted model session.

## What a run leaves behind

```
shots/<shot>/
  brief.md refs/            authored spec and reference board
  plans/current.json        selected plan pointer (the only read path)
  plans/…                   immutable bundles behind that pointer
  build/                    accepted delta scripts (the artifacts of record)
  answers.md                supervisor decisions; these outrank inference
  shot.json                 ledger: verdicts, rounds, acceptance
  state/                    durable worklists, unit state, contract gaps
  runs/latest.json          selected structured run
  runs/<run-id>/
    manifest.json           schema, invocation, layout, authority
    status.json             terminal state and selected stop-envelope digest
    artifacts.json          complete file catalog
    reports/summary.json    findings, cost, trajectory, typed stop summary
    reports/stop-envelope.json  immutable terminal stop authority when unaccepted
    reports/layers/         one aggregate report per layer
    logs/                   console, transcripts, costs
    evidence/               judged renders and comparisons
    checkpoints/            Blender state, scripts, journals
    scratch/                disposable probes
    deliverables/           final published media
```

| file | answers |
|---|---|
| `reports/layers/layer-N.json` | did this layer go well? |
| `logs/cost.jsonl` | cost by role |
| `logs/transcripts/<stage>/*.jsonl` | why it decided that |
| `logs/console.log` | what it looked like happening |

```bash
vfx inspect shots/barrel_roll --list-runs
vfx inspect shots/barrel_roll --run <run-id> --json
vfx inspect shots/barrel_roll --run <run-id> --layer 3
```

Transcripts strip image payloads and keep `run_bpy` scripts verbatim.
`VFXH_NO_TRANSCRIPT=1` turns recording off.

## Layout

```
src/vfx_harness/application/     user-facing use cases
src/vfx_harness/domain/          shot, contract, and work-unit language
src/vfx_harness/orchestration/   state, ledger, repair, and revalidation
src/vfx_harness/agents/          model roles, prompts, hooks, and context
src/vfx_harness/blender/         headless Blender boundary and tools
src/vfx_harness/evidence/        runtime measurements and claim authority
src/vfx_harness/evaluation/      offline and qualification evaluations
src/vfx_harness/knowledge/       verified agent cookbook and capability ledger
src/vfx_harness/observability/   logs, transcripts, cost, and provenance
docs/                            architecture, decisions, improvements, operations, research
evals/                           tracked evaluation definitions and fixtures
src/tests/                       unit, contract, architecture, and integration guarantees
shots/                           local shot inputs and outputs (ignored)
artifacts/                       generated evaluation and render evidence (ignored)
```

See [`AGENTS.md`](AGENTS.md) for coding-agent rules and
[`docs/architecture/repository.md`](docs/architecture/repository.md) for package
boundaries.
