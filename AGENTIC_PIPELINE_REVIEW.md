# Agentic Pipeline Architecture Review

## Overall assessment

The architecture has strong instincts but is not yet a reliable long-running harness.

Its strongest ideas are the warm Blender session, independent critics, deterministic rebuilds, layer-scoped ownership, final full-chain acceptance, render metrics, and explicit checkpoints. Those are worth preserving.

The central weakness is that important invariants are still expressed as prompts and comments rather than enforced as transactions, schemas, artifact dependencies, and tests. The historical evidence shows the consequence:

- `server_to_hansa`: 5/8 layers marked passed, but only 1/5 final acceptance moments passed.
- Archived `barrel_roll`: 4/6 layers passed, but only 3/4 acceptance moments passed.
- The newest eight-layer redesign has only completed layer 1, so it has not yet demonstrated improved end-to-end success.
- There is no test suite despite the README claiming one.

This is consistent with current harness-engineering lessons: repository-local structured artifacts and independent evaluators help, but documentation alone does not enforce invariants; boundaries need mechanical validation. [Anthropic recommends structured handoffs and clean context resets for long work](https://www.anthropic.com/engineering/harness-design-long-running-apps), while [OpenAI emphasizes mechanically enforced architecture and structural tests](https://openai.com/index/harness-engineering/).

## Latest-run evidence

The latest completed build I found is the August 13 layer-1 `barrel_roll` run in [`shots/_archive/layer1_prefix_2026-08-13/shot.json`](shots/_archive/layer1_prefix_2026-08-13/shot.json). Its detailed SDK trace is outside the shot folder at `/home/sahan/.claude/projects/.../e1865419-6b4d-43d1-a929-322ee9738358.jsonl`.

That single layout layer used:

- 89 unique model requests.
- 42 `run_bpy` calls containing about 44 KB of generated Python.
- 18 standalone renders and 15 compare renders.
- 113,604 output tokens.
- 12,964,866 prompt-cache read tokens and 213,511 cache-creation tokens.
- Roughly 34 minutes for the builder session, plus canonical criticism.
- 114 seconds for the first recorded critic round alone.

Prompt caching is working extremely well monetarily. Do not interpret the 13 million cached tokens as 13 million full-price input tokens. But prompt caching does not remove tokens from the model's attention or context window—it reduces processing cost, not context bloat. Anthropic explicitly distinguishes caching from context editing on this point. [Manage tool context](https://platform.claude.com/docs/en/agents-and-tools/tool-use/manage-tool-context)

The run also wrote the build script twice—about 9.5 KB, then 11.8 KB—despite the prompt instructing it to write once. That is direct evidence that prompt instructions are not sufficient enforcement.

## If I had to operate this pipeline myself

I would redesign these first, in this order:

1. Make every run an atomic, validated artifact transaction.
2. Make Blender mutations recoverable and make the raw journal the authoritative build artifact.
3. Replace the agentic critic loop with one direct structured multimodal request.
4. Make layer dependencies fail closed and hash-invalidate downstream work.
5. Establish end-to-end evals and durable telemetry before changing models or budgets.

I would not begin by shrinking prompts, downgrading models, removing verification, or reducing judge frames. Those would optimize the wrong layer.

## A — Quality improvements

### A1. One typed plan artifact, validated and atomically published

**Current behavior:** The planner writes `plan.md`, `layers.json`, `acceptance.json`, and `critic_axes.json` independently. The runner only verifies that `plan.md` exists; it does not prove that the current session created it or that the companions correspond to it. See [`pipeline/plan_agent.py`](pipeline/plan_agent.py:101).

The current shot illustrates the provenance problem: `plan.md` and `brief.md` are dated August 14, while the companions range from August 12–13.

**Problem:** A crash or incomplete model response can leave a mixed-generation plan. An old `plan.md` already on disk can make a failed session look successful. Model-written duplicate representations can silently drift.

**Proposed change:** Have the model produce one schema-constrained `PlanSpec` in a run-specific staging directory. Deterministic code should:

- Validate IDs, frames, refs, ownership coverage, judge/read agreement, script prefixes, transition coverage, and unanswered questions.
- Generate `plan.md`, `layers.json`, `acceptance.json`, and `critic_axes.json`.
- Record hashes of the brief, refs, model, prompt version, recipes and planner evidence.
- Atomically promote the complete bundle only after validation.

Structured outputs are intended for exactly this boundary. [Claude structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)

**Quality impact:** Large improvement. It eliminates mixed-state plans and machine-readable/prose drift.

**Cost/token impact:** Likely cheaper because the model emits one representation instead of four, while deterministic code generates the rest.

**Risk:** An overly rigid schema could remove useful nuance. Keep long-form rationale and ticket prose as schema fields.

**Validation:** Corrupt or interrupt planning at every write boundary; no partial bundle may become current. Run a corpus validator over all archived plans. Blind-grade rendered plans generated by old versus typed pipelines.

### A2. Transactional `run_bpy`, durable write-ahead journal, exact artifact persistence

**Current behavior:** Successful `run_bpy` payloads are kept in an in-memory `_JOURNAL`; failures are not journalled. See [`pipeline/blender/worker.py`](pipeline/blender/worker.py:367) and [`pipeline/blender/worker.py`](pipeline/blender/worker.py:400).

A failing script may mutate Blender before raising. Those partial effects remain live but are absent from the journal. The builder later asks a model to reconstruct a clean script from the successful-call transcript.

**Problem:** The live best scene can contain unjournalled partial mutations. Model-authored finalization then introduces a second drift point. This is a plausible root cause of live/canonical disagreement.

**Proposed change:**

- Treat every mutation as a transaction: checkpoint or undo boundary before execution; roll back completely on failure.
- Append each successful call immediately to a disk WAL with sequence number and post-state fingerprint.
- Automatically snapshot on elapsed-time/mutation thresholds, not only after reaching a critic round.
- Make the concatenated WAL the authoritative raw build script.
- Permit model refactoring only as an optional derivative; accept it only if scene manifests and rendered outputs are equivalent to the raw script.

**Quality impact:** Major improvement in reproducibility and recovery. It removes "re-author from memory" from the critical path.

**Cost/token impact:** Removes the expensive final model rewrite and repeated full-file writes. Transaction checkpoints add local I/O.

**Risk:** Blender undo may not cover every operation. If so, restore a lightweight `.blend` checkpoint or execute mutations in a disposable worker.

**Validation:** Inject exceptions after the first, middle and last mutation in scripts. The post-failure scene must equal the pre-call scene. Kill Blender after every successful call and verify WAL+snapshot recovery. Require pixel/manifest equivalence between live and replayed state.

### A3. Fail-closed artifact DAG with downstream invalidation

**Current behavior:**

- A prior layer whose ledger status is not passed is logged, then chained anyway in [`pipeline/build_agent.py`](pipeline/build_agent.py:105).
- Acceptance skips missing scripts and judges the partial chain in [`pipeline/accept_agent.py`](pipeline/accept_agent.py:32).
- Final rendering globs numeric Python files rather than consuming an accepted manifest in [`pipeline/render_shot.py`](pipeline/render_shot.py:25).
- Layer scripts depend on mutable Blender object names.

**Problem:** Unaccepted, orphaned, stale, or incompatible work can enter the deliverable. Editing an early layer does not mechanically invalidate later layers.

**Proposed change:** Each published layer should include:

- `input_hash`: prior manifest + brief/plan hash + asset hashes + Blender/helper versions.
- `output_hash`: raw script + scene manifest + canonical renders.
- Explicit `provides`/`requires` semantic handles.
- Passed canonical verdict and evaluator version.

The orchestrator must refuse to run downstream work if a dependency is failed, missing, stale, or hash-mismatched. Add an explicit unsafe override rather than silently continuing.

**Quality impact:** Major improvement. Compounding errors become visible at the layer where they originate.

**Cost/token impact:** Mostly deterministic hashing and validation; saves expensive work on invalid chains.

**Risk:** Aggressive invalidation may rebuild more than necessary. Later, narrow invalidation using declared dependencies.

**Validation:** Mutate each early script and assert exactly the dependent layers become stale. Delete or add an orphan script and confirm acceptance/render refuse to proceed.

### A4. Calibrate verification and use adaptive adjudication

**Current behavior:** A single critic returns integer 0–5 scores. Low-axis layers use a different threshold, and canonical failure can be accepted as "reproduced" when within up to one full score point of the live best. See [`pipeline/build_agent.py`](pipeline/build_agent.py:512) and [`pipeline/build_agent.py`](pipeline/build_agent.py:944).

**Problem:** The thresholds are based on a very small sample. A one-point tolerance on one scored axis is too wide to be called judge noise. Re-criticizing equivalent live/canonical renders adds stochastic double jeopardy.

**Proposed change:**

- Compare live-best and canonical images deterministically first. If equivalent, reuse the quality verdict.
- Judge canonical independently only when it differs materially or at additional judge frames.
- Add a second independent critic only near the decision boundary or when critic and metrics disagree.
- Calibrate thresholds per axis against human pairwise labels.
- Record evaluator prompt/version and uncertainty.

**Quality impact:** Better pass/fail consistency and fewer accidental passes or failures.

**Cost/token impact:** More cost on ambiguous cases, fewer calls on equivalent renders.

**Risk:** Image-equivalence metrics can miss semantically meaningful differences. Combine pixel/SSIM-style comparisons with scene-manifest comparisons.

**Validation:** Build a frozen set of at least 100 candidate/reference pairs with blinded human labels. Measure false-pass rate, false-fail rate, inter-run agreement and boundary flip rate.

### A5. End-to-end acceptance must trigger repair, not merely record failure

**Current behavior:** Acceptance records failures and marks contradictory layer verdicts as superseded, but there is no automatic repair path. See [`pipeline/accept_agent.py`](pipeline/accept_agent.py:125).

**Problem:** Local layers can finish while the shot remains unusable. The historical 1/5 and 3/4 final acceptance results demonstrate this.

**Proposed change:** Route every failed acceptance axis/frame back to the earliest owning layer capable of fixing it. Mark that layer and all dependents stale, reopen it with:

- Failed moment and axis.
- Objective deltas.
- Last accepted manifest.
- Protected properties that must not regress.
- Previously rejected approaches.

After repair, replay downstream layers and rerun the full acceptance suite. Add a bounded stopping rule: repeated same-axis failure after one approach replacement becomes `needs_supervisor`, not another tuning loop.

**Quality impact:** Directly raises final task success.

**Cost/token impact:** More compute, necessarily. This is capability spend, not waste.

**Risk:** Repairing an early layer can destabilize much of the chain.

**Validation:** Primary metric is final-shot acceptance and blind human preference, not layer pass rate. Also track repair yield and regressions introduced per repair.

### A6. Structured context resets inside long layers

**Current behavior:** A layer can run hundreds of turns with automatic compaction and a generated `CLAUDE.md`. The newest actual layer used 89 model calls; earlier layer G reached 121 turns. The persistent contract is a good idea, but the generated context is never cleared despite `clear_layer_context` being imported.

**Problem:** Compaction alone preserves too much trajectory debris and can retain anchoring on a poor technique. Anthropic's newer harness work reports that clean resets with structured handoffs can outperform compaction alone on very long tasks. [Harness design for long-running applications](https://www.anthropic.com/engineering/harness-design-long-running-apps)

**Proposed change:** At semantic boundaries or a measured context threshold:

1. Snapshot Blender and WAL.
2. Write a typed `layer_state.json` containing current manifest, object handles, best render, converged values, failed experiments, protected axes and remaining work.
3. Start a fresh builder session from that state.
4. Use the SDK's available `PreCompact` hook to ensure the state is current before compaction.
5. Clear generated `CLAUDE.md` in `finally`.

**Quality impact:** Better focus and less anchoring on stale attempts.

**Cost/token impact:** Resets add a new-session prefix and handoff read, but should reduce repeated reasoning later.

**Risk:** Incomplete handoffs can be worse than compaction.

**Validation:** On layers exceeding 40 calls, A/B compaction-only versus reset+handoff. Compare completion, regressions, duplicate tool calls and final human scores.

### A7. Build the eval suite before further architectural expansion

**Current behavior:** No `tests/` directory exists; `.venv` does not have pytest installed. Recipe verification misclassifies a Snap launch failure as "24 stale APIs."

**Problem:** Recent changes—axis ownership, multi-frame judging, resume, metrics, ablation and reconciliation—are sophisticated but largely untested. Architectural complexity is growing faster than evidence.

**Proposed change:** Create four test levels:

- Pure unit tests: plan validators, ledger merging, thresholds, path sandbox, script mapping.
- Fault-injection integration tests with a fake Blender worker and fake SDK.
- Real Blender smoke tests for transaction/replay/recipe execution.
- Frozen end-to-end shot evals with human-rated outputs.

**Quality impact:** Essential for preventing compounding harness bugs.

**Cost/token impact:** Engineering and CI compute increase; expensive regressions and failed production runs decrease.

**Risk:** Overfitting to two existing shots. Add diverse minimalist, photoreal, typography, motion and atmosphere fixtures.

**Validation:** Mutation-test critical validators and fault paths. A test suite that cannot catch the current `_encode` defect or false "stale recipe" classification is insufficient.

## B — Free efficiency

### B1. Replace the agentic critic with one direct multimodal structured request

**Current behavior:** Every critic is a fresh Agent SDK session with filesystem tools, up to eight turns, and retries. It must call `Read` for the candidate and reference; the harness infers that it saw them from tool-call metadata. See [`pipeline/build_agent.py`](pipeline/build_agent.py:532).

**Problem:** Criticism is not an agentic task. Tool selection, filesystem resolution, "did it read both?" logic and multiple round trips add latency and failure modes. The current code can count an attempted `Read` as seen even when its result failed.

**Proposed change:** Call the model API directly with candidate, reference and optional motion strip as image blocks, plus the rubric and JSON schema. Keep the same critic model and reasoning effort initially.

**Quality impact:** Same or better—the critic is guaranteed to receive exactly the intended images.

**Cost/token impact:** Substantially lower latency and fewer tool/schema tokens. The 114-second latest critic round should fall sharply.

**Risk:** Agent SDK-specific vision preprocessing may differ.

**Validation:** Paired replay on the frozen critic corpus. Require non-inferior human agreement and lower missing-image/error rate.

### B2. Clear stale tool results while preserving structured conclusions

**Current behavior:** Dozens of old renders, comparison images, tool outputs and superseded code calls remain in the same conversation. Prompt caching makes them cheap to resend but does not remove them from context.

**Problem:** Context accumulates without corresponding capability. The latest layer averaged roughly 146k cache-read tokens per model request.

**Proposed change:** Keep active:

- Immutable layer contract.
- Current scene manifest.
- Latest and best render.
- Current metrics/critic result.
- Protected axes.
- Rejected approaches.
- Remaining work.

Clear older images, raw scene dumps, superseded `run_bpy` results and obsolete critic prose after their conclusions are written to `layer_state.json`. This follows current first-party context-editing guidance. [Manage tool context](https://platform.claude.com/docs/en/agents-and-tools/tool-use/manage-tool-context)

**Quality impact:** Expected improvement in focus, provided conclusions are persisted first.

**Cost/token impact:** Large reduction in attention load and latency; caching savings remain for the stable prefix.

**Risk:** Clearing evidence before extracting a durable conclusion.

**Validation:** Track duplicate calls, forgotten constraints, context size and task success. Reject the optimization if forgotten-fact errors rise.

### B3. Deterministic precomputation, caching and safe parallelism

**Current behavior:** The planner spends model turns calling `measure_ref` one image at a time; image metrics are recomputed repeatedly. Acceptance renders and critiques moments sequentially.

**Problem:** Deterministic work occupies expensive agent turns and wall time.

**Proposed change:**

- Compute every reference fingerprint concurrently before planning.
- Cache vectors by content hash, metric version and resolution.
- Pass `ref_metrics.json` to both planning and building.
- Render acceptance frames serially in Blender, then evaluate independent images concurrently with a bounded pool.
- Continue parallel multi-frame canonical critics; that existing optimization is correct.
- Parallelize independent asset generation, not dependent layers.

**Quality impact:** Unchanged; identical functions and models operate on identical inputs.

**Cost/token impact:** Fewer model/tool turns and lower latency.

**Risk:** Unbounded API concurrency or stale metric caches.

**Validation:** Hash-based cache correctness tests and serial-versus-parallel result equality.

### B4. Move recipe distillation out of the critical path

**Current behavior:** After a pass or API error, an Opus model can modify the global cookbook and mark recipes `verified: true` in [`pipeline/build_agent.py`](pipeline/build_agent.py:369).

**Problem:** This spends a frontier-model call after every successful layer and lets one trajectory mutate global knowledge. "A passing shot used it" is not the same as "the extracted recipe is correct and general."

**Proposed change:** Append candidate learnings to a structured queue. Batch-distill offline after several runs. Require executable verification and ideally human approval before promotion to `verified`.

**Quality impact:** Improves global knowledge quality and prevents one bad extraction from poisoning future builds.

**Cost/token impact:** Removes a per-layer Opus call; batching deduplicates repeated gotchas.

**Risk:** Useful lessons become available later.

**Validation:** Measure recipe promotion precision, subsequent usage and success uplift. Never promote a candidate that fails execution verification.

### B5. Fix deterministic defects and remove duplicated instructions

**Current behavior:**

- `render_frames` calls undefined `_encode` in [`pipeline/blender/tools.py`](pipeline/blender/tools.py:280).
- Generated layer context is never cleared.
- The recipe verifier calls an environment launch failure "API stale."
- `render_shot.py` assigns `t0` twice.
- The README describes a removed `src/scene` architecture.
- Builder policy is duplicated across the system prompt, kickoff, plan excerpt and `CLAUDE.md`.

**Problem:** These defects waste turns and undermine observability. Duplicated rules compete for attention and complicate prompt caching/versioning.

**Proposed change:** Fix the code defects; add tests; reduce repeated instructions to:

- Stable system: role, tool semantics, invariants.
- Typed layer contract: target, ownership, frames, protected state.
- Retrieved technique: only when needed.
- Durable state: current progress and rejected approaches.

Keep compact summary instructions in `CLAUDE.md`, but generate them from the same contract rather than restating prose independently.

**Quality impact:** Neutral to positive.

**Cost/token impact:** Modest recurring token reduction; significant reduction in failure-recovery turns.

**Risk:** Removing a genuinely load-bearing reminder.

**Validation:** Prompt ablations one section at a time, as recommended in Anthropic's harness study—not a wholesale rewrite. [Harness design](https://www.anthropic.com/engineering/harness-design-long-running-apps)

## C — Quality/cost tradeoffs: do not adopt by default

| Possible saving | Why I would not do it now | Required validation |
|---|---|---|
| Downgrade the Opus builder or lower effort | Building is the hardest reasoning/craft step. There is no evidence a cheaper model is equivalent. | Paired end-to-end runs across at least 20 diverse layers; non-inferior final acceptance and blind human ratings. |
| Remove the second planning pass | Two-pass planning may be expensive, but the verifier catches unsupported timing and evidence gaps. | Plan-defect recall, final-shot success and human plan review must remain non-inferior. |
| Reduce judge frames or motion strips | This recreates the exact transition blind spots already observed. | Temporal defect recall on a labeled transition corpus. |
| Lower render resolution/quality for final judgment | Fine detail, typography and halation are resolution-sensitive. | Per-axis agreement versus full-resolution judgments. |
| Cap builder turns or budget more aggressively | The current problem is wasted trajectory structure, not proven excess reasoning capability. | Compare truncation and success curves after context/WAL improvements. |
| Parallelize dependent layers or use many subagents | Shared scene state and mutable object contracts make this unsafe; handoffs would multiply drift. | Only after the artifact DAG proves true independence. |
| Aggressively prune context without a typed handoff | Can remove failed approaches, names or measured values and cause repeated work. | Forgotten-fact and duplicate-work metrics must not worsen. |

## Proposed target architecture

```text
brief + refs
    │
    ▼
Deterministic preflight
  hashes · ref metrics · ambiguity checks · prior-artifact inventory
    │
    ▼
Planner / verifier
    │ one schema-constrained PlanSpec
    ▼
Plan validator ──fail──► supervisor
    │ atomic publish
    ▼
Versioned artifact DAG
    │
    ├── independent assets, bounded parallelism
    │
    ▼
Layer orchestrator
  load verified prior manifest
    │
    ▼
Builder phase
  transactional run_bpy
  durable WAL + periodic snapshot
  typed layer_state.json
    │
    ├── context reset at semantic/size boundary
    ▼
Direct multimodal critic + deterministic metrics
    │
    ├── pass
    ├── surgical revision
    └── plateau → one approach review
    ▼
Raw WAL artifact
  optional refactor only if equivalent
    ▼
Canonical multi-frame verification
    │ atomic layer publish + hashes
    ▼
Full-chain acceptance
  all moments + temporal checks
    │
    ├── pass → final render
    └── fail → owning layer repair + downstream invalidation
```

Every box should emit append-only trace events. `shot.json` becomes a derived summary, not the only record.

## What should not be changed

Keep these unless an eval disproves them:

- Opus/high effort for the builder.
- Separate builder and critic identities.
- Warm Blender sessions.
- Deterministic replay from an empty scene.
- Best-scoring scene snapshot restoration.
- Layer-scoped axis ownership.
- Multi-frame canonical verification and motion strips.
- Full-chain final acceptance as the real success criterion.
- Objective image metrics as additional evidence.
- Filesystem sandboxing and API guardrail hooks.
- Recipe retrieval bodies on demand.
- Prompt caching; the latest trace shows it is highly effective.
- Sequential execution of dependent layers.
- Two-pass planning for difficult shots until a controlled ablation proves it unnecessary.

Tool search is not a priority: the builder has roughly a dozen relevant tools, below the range where current guidance usually recommends dynamic tool discovery. Directly removing non-agentic critic tools is higher leverage.

## Biggest unnecessary token/cost sources

1. Long builder histories retaining obsolete renders and tool results: about 13 million cache-read tokens for one layout layer.
2. 89 model calls, 42 mutation calls and 33 renders/comparisons for a one-axis layout layer.
3. Agentic critics that need filesystem/tool loops for a fixed two-image classification.
4. Model re-authoring of 10–23 KB scripts already represented exactly by the run journal.
5. Two full model-written plan representations plus three separate machine companions.
6. Per-layer Opus recipe distillation.
7. Repeated model-driven reference measurement instead of deterministic precomputation.
8. Repeated instruction copies across system prompt, kickoff, plan and `CLAUDE.md`.

The two-pass planner may be expensive, but I would not yet call it waste. Its deterministic subwork and duplicated outputs are waste; the adversarial review itself may be load-bearing.

## Biggest current quality risks

1. Failed `run_bpy` calls can leave unjournalled partial mutations in the live scene.
2. Planning outputs are not atomic, freshness-checked or tied to a common provenance hash.
3. Unpassed prior layers are chained anyway.
4. Acceptance can silently judge a partial chain when scripts are missing.
5. Final rendering can execute orphan numeric scripts not in the accepted ledger.
6. Local layer pass rates remain weak predictors of final-shot success.
7. Critic scoring is not calibrated against human judgments and image-read verification is fragile.
8. Canonical reproduction tolerance is too loose for one-axis layers.
9. Recovery begins only after reaching the first critic checkpoint; long initial builds remain vulnerable.
10. No automated tests cover the rapidly expanding harness.
11. The metric blocker policy is globally configured but has not been demonstrated to have acceptably low false-rejection rates across shot styles.
12. Detailed telemetry lives in hidden SDK session logs rather than the shot's auditable run artifacts.

## Prioritized implementation plan

1. **Baseline first:** Freeze current renders, ledgers, traces and human labels for both shots. Build the trace summarizer before changing behavior.
2. **Correctness foundation:** Add `run_id`, attempt IDs, atomic writes, locks, input/output hashes, typed `PlanSpec`, and plan validation.
3. **Recovery foundation:** Durable WAL, transactional `run_bpy`, periodic snapshots, and exact raw-journal publishing.
4. **Fail-closed DAG:** Refuse missing/unpassed/stale priors; make render and acceptance consume only the published manifest.
5. **Verification redesign:** Direct image critic, deterministic live/canonical equivalence, adaptive second judge near thresholds.
6. **Context redesign:** `layer_state.json`, context editing, `PreCompact` checkpointing and semantic resets.
7. **Repair loop:** Acceptance failure routing, downstream invalidation and bounded approach replacement.
8. **Free efficiency:** Reference metric cache, safe concurrency, offline recipe distillation, prompt deduplication.
9. **Only then model-route:** Test cheaper models or lower effort on bounded roles, never on intuition.

## Evals to run before and after major changes

| Change | Primary quality eval | Reliability eval | Efficiency telemetry |
|---|---|---|---|
| Typed atomic planning | Human plan-defect recall; downstream final success | Kill planner at every write; schema mutation tests | Planner calls, output tokens, duplicate facts |
| Transactional Blender/WAL | Live-versus-replay render and manifest equivalence | Exception/kill injection after every mutation | Recovery time, lost-work calls, finalize tokens |
| Direct critic | Agreement with blinded human pairwise labels | Missing/corrupt image and retry tests | Calls, latency, tokens per verdict |
| Context editing/reset | Final human score and task completion | Forgotten-constraint and duplicate-work rate | Active tokens, calls, cache reads, wall time |
| Artifact DAG | Final acceptance and regression count | Missing/orphan/stale script tests | Avoided invalid downstream runs |
| Adaptive judging | False-pass and false-fail rates | Repeated-run score variance | Critic calls per decision |
| Acceptance repair | Final-shot pass rate and blind preference | Bounded-loop/plateau termination | Cost per repaired moment |
| Model routing | Non-inferiority on final success | Error and retry rates | Cost and latency only after quality passes |

For model or cost A/B tests, use paired runs on identical inputs, models/settings except the tested variable, and blind human raters. The primary decision statistic should be final task success with a non-inferiority bound—not mean tokens, layer pass rate, or critic score.

## Assessment of the “Cursor for 3D” interaction model

The `fstandhartinger/Cursor-for-3D` repository does not contain an agent implementation to adopt. Its fork-specific history consists of README edits over Blender, and the README explicitly describes the project as a joke. The underlying product metaphor is still worth evaluating: treat Blender as a persistent, inspectable workspace in which an agent makes scoped, reversible edits rather than repeatedly reconstructing and reauthoring the scene.

That metaphor does **not** justify a wholesale redesign. The current pipeline already has its most important foundation—a warm, persistent Blender worker—and a full “Cursor for 3D” architecture would introduce substantial complexity without demonstrated quality gains. Only the following narrow elements are currently justified.

### Adopt now: transactional scene edits

**Current behavior:** `run_bpy` executes arbitrary Python directly against the live warm scene. The successful call is journalled, but an exception can leave partial mutations that are neither accepted nor replayable.

**Problem:** Subsequent calls may build on corrupted or ambiguous state. This is a direct source of compounding errors and makes recovery nondeterministic.

**Proposed change:** Wrap every mutating operation in `savepoint → execute → validate → commit`, restoring the savepoint on any exception, invariant failure or worker interruption. Retain arbitrary `bpy`; the transaction is an orchestration boundary, not a restriction on the builder.

**Quality impact:** Reliability and task success should improve because failed attempts cannot contaminate later work. Creative capability remains unchanged.

**Cost/token impact:** Small additional checkpoint latency and storage. It may reduce expensive downstream recovery and repeated model work.

**Risk:** Per-call `.blend` saves may be too slow or may interact poorly with some Blender state. A cheaper undo mechanism may also prove incomplete in background mode.

**Validation:** Inject exceptions and process termination after representative mutations, then compare object/material/animation manifests and rendered hashes against the pre-call savepoint. A failed operation must leave zero persistent semantic changes.

### Adopt now: deterministic semantic diffs

**Current behavior:** Tool results report stdout, timing and coarse scene-count deltas. The model often needs inspection or rendering to determine exactly what changed.

**Problem:** This causes repeated state reconstruction, obscures unexpected cross-layer changes and makes failures difficult to localize.

**Proposed change:** Compute a compact before/after diff for every mutating call. Initially cover stable object identity, creation/deletion, transforms, collection membership, materials, cameras, lights, frame ranges, keyframe counts and render settings. Report expected and unexpected changes separately.

**Quality impact:** The builder and supervisor receive more precise feedback and can detect unintended edits before they compound. This complements rather than replaces visual verification.

**Cost/token impact:** Adds deterministic CPU work but should reduce inspection calls and verbose scene dumps. Compact diffs are cheaper than repeatedly resending full state.

**Risk:** An incomplete diff could create false confidence, particularly around drivers, constraints, modifiers, node groups and linked data.

**Validation:** Maintain a corpus of representative scene mutations and compare reported diffs with Blender-native state inspection and blinded manual review. Unknown or unsupported state must be marked as such rather than silently treated as unchanged.

### Adopt now: persistent issue and attempt ledger

**Current behavior:** The builder and critic retain problems, attempted fixes and progress primarily in conversational history and free-form verdicts.

**Problem:** The agent can retry equivalent changes, lose track of the active defect or continue rendering after progress has plateaued. The latest layout run's 89 model requests and 33 render/comparison calls make this a plausible source of waste and trajectory drift.

**Proposed change:** Persist each diagnosed issue with severity, owning layer, affected objects/frames, evidence, attempted approaches, metric history, status and explicit resolution criteria. Require each iteration to close an issue, materially improve it, introduce new evidence or trigger a bounded approach change.

**Quality impact:** Planning and criticism become cumulative instead of repeatedly reconstructed. Plateau detection should encourage genuine approach changes rather than parameter thrashing.

**Cost/token impact:** Small structured-state overhead; likely fewer redundant model, inspection and render calls.

**Risk:** Poorly formulated issues or noisy metrics can prematurely constrain creative exploration.

**Validation:** Compare duplicate-attempt rate, unresolved-major-issue rate, iterations per resolved issue, final human preference and final acceptance. Do not declare success from call reduction alone.

### Test before adopting: scoped context retrieval

**Current behavior:** Long builder histories preserve obsolete renders, tool results and earlier reasoning. The latest trace contains roughly 13 million cache-read tokens.

**Problem:** The model repeatedly processes information that is no longer relevant, while important state remains mixed with stale attempts.

**Proposed change:** Construct each active context from global shot invariants, the current layer, directly dependent scene elements, the active issue, recent accepted diffs and relevant references. Preserve an explicit tool for broadening context, and expand automatically after unexpected changes or repeated failures.

**Quality impact:** It may improve focus and should maintain quality if dependency retrieval and fallback expansion are reliable. Missing a non-obvious dependency could reduce quality, so this remains an evaluated hypothesis.

**Cost/token impact:** Potentially large reductions in active and cache-read tokens, plus lower latency.

**Risk:** Hidden dependencies may be omitted. Aggressive scoping could cause inconsistent cross-layer decisions or forgotten constraints.

**Validation:** Paired evaluation on identical tasks, including scenes with cross-layer dependencies. Require non-inferiority in task success and blind human visual score; also track forgotten constraints, context-expansion requests, duplicate work, cache reads and wall time.

### Defer: complete scene IR or semantic index

A fully faithful intermediate representation of Blender is not presently justified. Blender scenes contain implicit and difficult-to-index relationships through constraints, drivers, modifiers, node groups, linked data and dependency-graph behavior. A partial index could become stale while appearing authoritative. Start with a small derived manifest and semantic diffs, expanding only when traces demonstrate a recurring information gap.

### Defer: mandatory typed patch language

Typed helpers are valuable for common, deterministic operations, but a closed patch language would reduce the builder's ability to solve novel VFX tasks. The appropriate design is typed helpers for routine work plus a transactional arbitrary-`bpy` escape hatch. Do not replace general Python unless a broad capability eval demonstrates equivalence.

### Defer: interactive editor UI and default variant branching

An interactive Cursor-like interface may improve human usability but does not inherently improve this autonomous, headless harness. It is not a current reliability priority. Likewise, branching multiple scene variants can improve difficult artistic decisions but multiplies builds, renders and critic calls. Trigger it only for high-uncertainty decisions or verified plateaus, and validate the gain with blind preference tests.

### Decision and rollout gate

Adopt the narrow pattern in this order:

1. Transactional `run_bpy`.
2. Deterministic before/after semantic diffs.
3. Persistent issue and attempt ledger.
4. Evaluated context scoping with automatic fallback expansion.
5. Preserve the existing arbitrary-`bpy` path and high-capability builder throughout.

Before expanding further, run paired evaluations on routine scenes and scenes requiring novel Blender Python. Final task success and blind human visual scores must be non-inferior. Failed calls must produce zero residual mutations; duplicate attempts, repeated inspections and recovery time should decline. Model calls and tokens are secondary evidence, not the acceptance criterion.

The concise verdict is: your pipeline needs stronger deterministic orchestration around its capable models, not weaker models or tighter budgets. The biggest opportunity is to turn the journal, plan and ledger from advisory files into atomic, validated, versioned contracts. Once that is done, context pruning and direct critics can remove substantial waste without sacrificing capability.
