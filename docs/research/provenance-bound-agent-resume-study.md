# Authorized Continuation: A Study of Safe Agent Resume Under Authority Drift

**Status:** research protocol, not an empirical result
**Prepared:** 2026-08-31
**Primary question:** What state identity must match before a long-horizon agent may safely
resume previous work?

## Executive decision

This is a viable research program, but the broad claim that checkpoints or conversational
memory are insufficient is no longer novel by itself. Recent work separately studies exact
execution edits, semantic isolation, recovery contracts, interruption recovery, rollback, and
commit-time authority. The defensible contribution is narrower and more useful:

> Build a controlled, cross-domain benchmark that measures resume admission under same-ID task
> mutation and authority drift, and determine the smallest provenance certificate that preserves
> valid work without admitting stale work.

The initial hypothesis also needs two corrections from the VFX Harness incidents:

1. Matching the whole authority-generation identifier is sufficient but not necessary. An
   unrelated sibling may change the global bundle while the active task and its complete causal
   slice remain identical ([HIR-0040](../improvements/HIR-0040-sibling-view-hash-is-not-a-dag-change.md)).
2. Matching static identity is not sufficient. Reconstruction must publish and read back the
   runtime's evaluated state before a successor or evaluator runs
   ([HIR-0117](../improvements/HIR-0117-artifact-replay-publishes-evaluated-state.md)).

The study should therefore test a **two-stage resume protocol**:

- **Admission:** prove that the saved state is still authorized for the task-relevant current
  authority.
- **Reconstruction:** replay it under pinned semantics, publish evaluated state, and revalidate
  current contracts before allowing a successor or external effect.

## Refined thesis

Conversation and session identifiers are transport metadata. Checkpoints and artifact hashes
prove possession of bytes. None grants authority to continue.

A resume is safe only when:

1. the active task and compiled task context are equivalent under current authority;
2. the ordered dependency closure and consumed interfaces are equivalent;
3. checkpoint and artifact bytes, settings, and causal parent chain verify;
4. the journal cut is a complete committed prefix corresponding to the checkpoint;
5. replay uses the declared runtime protocol and publishes evaluated state; and
6. deterministic current-authority read-back passes before further mutation or commit.

“Equivalent” deliberately does not mean “the monolithic generation hash is equal.” Exact hash
equality is one proof. A typed, versioned compatibility or carry-forward proof may be another.

## Research questions and falsifiable hypotheses

### RQ1 — Safety

Which identity fields are necessary to prevent unauthorized reuse after interruption?

**H1.** Session resume, checkpoint-only resume, and artifact-hash-only resume have higher unsafe
admission and silent-corruption rates than provenance-bound resume under semantic mutations.

**Falsifier:** a weaker policy has an upper confidence bound on unsafe admission no worse than the
full policy across held-out mutation classes.

### RQ2 — Minimality

Is exact global authority-generation equality necessary, or is task-relevant equivalence enough?

**H2.** Scoped provenance admits more correctly reusable work than exact-global matching without
increasing unsafe admission.

**Falsifier:** scoped equivalence admits stale work, or exact-global matching produces no material
false-refusal cost on unrelated-change controls.

### RQ3 — Reconstruction

Does a valid provenance certificate suffice without a pinned replay and state-publication
protocol?

**H3.** Static provenance alone does not prevent incorrect continuation when the runtime can expose
stale derived state; a publication/read-back barrier eliminates that class.

**Falsifier:** no replay-semantics injection changes successor-visible state once all static
identities match.

### RQ4 — Generality and efficiency

Do the safety/reuse trade-offs transfer between a deterministic provider-neutral artifact DAG and
Blender, and across model families and stochastic replicates?

**H4.** Policy ordering is stable across both environments, while absolute recovery cost varies by
environment and model.

## Formal model

Let the current task-relevant authority projection be:

```text
A(u) = (tau, kappa, alpha, delta, gamma, rho, epsilon)

tau      structured active-task digest
kappa    compiled task/context digest, including task-plan bytes
alpha    selected task-relevant authority and gate attestations
delta    canonically ordered transitive dependency/interface closure
gamma    evidence contracts, evaluator identities, and due boundaries
rho      replay/runtime/toolchain semantics
epsilon  effect or authorization epoch, where applicable
```

Let a saved resume record be:

```text
K = (A_saved, phi, pi, J[start:end], q)

phi      checkpoint/artifact content and settings identity
pi       causal parent-chain identity
J        hash-chained committed journal slice and its exact boundaries
q        replay/publication/read-back receipt
```

Admission is permitted only if a trusted checker establishes:

```text
equivalent(A_saved, A_current)
and verifies(phi, pi)
and committed_complete_prefix(J[start:end])
```

Continuation is permitted only after:

```text
reconstruct(K, pinned=A_current.rho)
and publish_evaluated_state()
and revalidate(A_current.gamma)
```

The checker, not the model, decides these predicates. The model may repair after refusal, but it
cannot waive a mismatch.

### Candidate resume envelope

```yaml
schema: vfx-harness.resume-envelope/v1
active_task:
  layer_id: string
  unit_id: string
  unit_digest: sha256
  compiled_context_sha256: sha256
  unit_plan_sha256: sha256
authority:
  global_bundle_hash: sha256       # audit + conservative policy
  relevant_projection_hash: sha256
  gate_attestation_id: string
  authored_input_hashes: object
dependency_closure:
  ordered:
    - unit_id: string
      unit_digest: sha256
      outcome_hash: sha256
      script_sha256: sha256
      interface_digests: [sha256]
artifact:
  kind: string
  locator: string
  sha256: sha256
  settings_hash: sha256
  runtime_toolchain_id: string
journal:
  locator: string
  sha256: sha256
  prefix_sha256: sha256
  checkpoint_index: integer
  first_event_id: string
  last_event_id: string
  checkpoint_sha256: sha256
origin:
  run_id: string
  attempt: integer
  created_at: rfc3339
session:
  session_id: string               # audit/optimization only
  transcript_frontier: string      # never admission authority
```

Every mismatch should produce a stable reason code. Store both global and scoped identities so the
study can measure over-binding rather than assuming it away.

## Experimental design

### Environments

#### E1 — Provider-neutral deterministic artifact DAG

Use small, typed workflows that transform JSON, CSV, and text inputs through a dependency DAG into
an exact manifest and report. Providers may propose work, but deterministic code owns identity,
mutation injection, admission, replay, and grading.

Each artifact records its producing task, authority projection, ordered parents, content hash,
contract version, and event origin. A taint oracle detects stale-origin material even when final
bytes happen to look plausible.

#### E2 — Blender executable fixtures

Begin with executable-only 2–3 unit DAGs and deterministic scene contracts:

- a same-ID geometry-to-dressing mutation patterned on
  [HIR-0059](../improvements/HIR-0059-warm-start-artifacts-must-match-unit-digest.md);
- a producer/consumer scene whose successor needs freshly evaluated transform or dependency-graph
  state, patterned on
  [HIR-0117](../improvements/HIR-0117-artifact-replay-publishes-evaluated-state.md);
- a dependency reorder or producer replacement patterned on
  [HIR-0119](../improvements/HIR-0119-replay-order-is-derived-from-the-unit-dag.md).

The primary oracle is empty-scene replay of the current accepted chain plus executable contracts,
semantic manifests, and stale-origin checks. Qualitative VFX tasks belong in a later external-
validity phase, not the first safety experiment.

### Resume policies

| ID | Policy | Admission rule | Expected profile |
|---|---|---|---|
| P0 | Clean restart | Reuse only the accepted current dependency prefix | Safety control; no interrupted work reused |
| P1 | Conversation/session resume | Same provider session or conversation | High reuse, no authority proof |
| P2 | Checkpoint-only | Snapshot exists and loads | Proves state bytes exist, not that they are current |
| P3 | Artifact-hash resume | Saved artifact bytes hash-match | Proves content, not task or causal authorization |
| P4 | Exact-global provenance | All fields match, including global generation | Conservative safety baseline; may over-reject |
| P5 | Scoped provenance-bound | Task-relevant projection and full causal record match | Candidate mechanism |

Add two diagnostics after the primary comparison:

- P5 plus provider-session continuity, separating safe state reuse from the value or harm of
  conversational continuity.
- Leave-one-field-out P5 ablations, testing the necessity of each identity family.

### Controlled mutation matrix

Every unsafe mutation needs a matched no-op or equivalence-preserving control. Otherwise clean
restart wins by construction and the study cannot measure maximal safe reuse.

| Fault class | Injection | Oracle label | Incident/mechanism anchor |
|---|---|---|---|
| F0 | Exact unchanged state | safe | Positive resume control |
| F1 | Same unit ID; goal, scope, contract, or completion semantics change | unsafe | HIR-0059 |
| F2a | Relevant authority is superseded | unsafe | HIR-0102, HIR-0133 |
| F2b | Only an unrelated sibling authority changes | safe | HIR-0040 |
| F3a | Upstream task, output, interface, or edge changes | unsafe | HIR-0084, HIR-0119 |
| F3b | Authored order changes among independent nodes; canonical closure does not | safe | DAG-order control |
| F4 | Same locator, substituted/tampered checkpoint or artifact bytes | unsafe | HIR-0053 |
| F5 | Journal is truncated, extended past selected checkpoint, gapped, or reordered | unsafe | HIR-0014, HIR-0054 |
| F6 | Failed partial mutation exists in state but not committed journal | unsafe | HIR-0068 |
| F7 | Evidence contract or evaluator version changes | unsafe until current revalidation | Revalidation identity |
| F8 | Static hashes match but replay omits evaluated-state publication | unsafe | HIR-0117 |
| F9 | Authorization/effect epoch expires or is revoked | unsafe | Security/durable-execution extension |

Test one factor at a time first. Add selected interactions only after the injectors and oracle pass
isolation tests: task × authority, dependency × artifact, and checkpoint × journal.

### Trial construction

For each block:

1. Prepare one reproducible initial state.
2. Run one shared pre-interruption trajectory.
3. Interrupt at a declared event boundary and seal its candidate checkpoint.
4. Clone that exact state into every policy arm.
5. Apply one typed mutation manifest.
6. Recompute current authority independently of the policy under test.
7. Run the admission decision, then the allowed recovery path.
8. Grade with the independent oracle.

Block by environment × fixture × injection × model × replicate. Randomize arm order using a
recorded experiment seed; use a Latin-square order in the confirmatory study. Hold post-
interruption turn, token, cost, and wall-time budgets equal. “Seed” means scenario seed and
stochastic replicate unless the provider exposes a genuine sampling seed.

### Trial record

```text
study_id, trial_id, block_id
environment, fixture_id, scenario_seed
model, model_version, effort, replicate
policy, budget
interruption: phase, after_event, checkpoint_id
injection: kind, target, expected_safe, equivalence_proof
pre_identity, post_identity
eligibility: admitted, reason_codes, decision_time
usage, events, terminal_result
```

Store tracked fixtures and study specs under `evals/resume_authority/`. Store generated trials
under `artifacts/evaluations/resume-authority/<study_id>/`; never mutate production shots or
accepted run state.

## Outcomes

### Safety-primary

**Unsafe admission rate**

```text
P(policy admits | oracle says saved state is not authorized)
```

**Incorrect-resume rate**

```text
trials where any state outside the current relevant closure is consumed before refusal
```

Rejection after the stale state reaches the model or a successor tool still counts as an
incorrect resume.

**Silent state corruption**

```text
pipeline reports success AND
(current-authority oracle fails OR unauthorized stale-origin state remains)
```

**Authorized completion**

```text
current task contracts pass AND provenance/admission is valid AND no stale-origin state remains
```

This must be reported alongside ordinary endpoint success. A visually or textually correct result
can still be unauthorized.

### Reuse and recovery

**False-refusal rate** is `P(policy rejects | oracle says resume is equivalent and safe)`.

**Reuse precision** is valid retained pre-interruption work divided by all retained
pre-interruption work.

**Reuse recall** is valid retained pre-interruption work divided by all still-valid reusable work.

**Recovery distance** is post-interruption authoritative mutations, tool calls, turns, and seconds
until the current oracle first passes. Report both absolute and paired difference from clean
restart.

**Wasted tokens/cost** are post-interruption usage attributable to work later invalidated,
discarded, or refused. Capture usage at phase and invalidation boundaries; a session-level total
cannot attribute this outcome precisely.

## Statistical plan

- Experimental unit: one checkpoint–mutation instance; stochastic replicates are nested within it.
- Report raw 2 × 2 admission confusion matrices and Wilson intervals before model-based summaries.
- Compare paired binary outcomes with exact McNemar tests; control multiple primary contrasts with
  Holm correction.
- Compare paired recovery, reuse, token, cost, and latency outcomes with paired bootstrap intervals.
- In the multi-model confirmatory phase, fit hierarchical logistic models with policy, mutation
  class, environment, and prespecified interactions; use random intercepts for task and model.
- Treat budget exhaustion as censored where appropriate rather than assigning an invented distance.
- Use the pilot only to estimate nuisance rates and variance, then freeze the confirmatory sample
  size and exclusion rules in a preregistration.

Do not pool safe controls with unsafe mutations into one “accuracy” number. A policy that rejects
everything can have perfect unsafe-admission performance while providing no useful resume.

## Phased execution

### Stage 0 — Systematic review and claim freeze

Search ACM Digital Library, IEEE Xplore, arXiv, OpenReview, and Semantic Scholar using three query
families:

- `(agent OR workflow) AND (resume OR checkpoint OR restore OR rollback OR interruption OR handoff)`;
- `(state drift OR semantic isolation OR authority OR provenance OR cache invalidation OR workflow versioning)`;
- `(exactly once OR write-ahead log OR consistent snapshot OR durable execution) AND recovery`.

Include empirical or formal work where action state persists across an interruption and
correctness depends on replay, versioning, authority, or change. Exclude pure conversational-memory
retrieval unless it controls persistent action state. Record queries, dates, deduplication,
backward/forward snowballing, and an extraction matrix before making a novelty claim.

### Stage 1 — Model-free mechanism test

Implement the identity checker, typed injectors, reason codes, and independent oracles. Run exact
matches, each one-field mismatch, unrelated-generation controls, and field-drop ablations without
an LLM. Every injected case must receive its prespecified oracle label.

### Stage 2 — Small provider-neutral pilot

A credible initial pilot is one task family × six scenario blocks × five primary arms × three
stochastic replicates × one model: 90 post-interruption continuations. A 30-arm single-replicate
run is a feasibility smoke test, not inferential evidence.

### Stage 3 — Blender validation

Use two executable-only fixtures × four critical scenarios × three arms (clean, strongest weak
baseline, scoped provenance) × three replicates: 72 continuations. First run an 18-continuation
smoke test covering exact match, same-ID drift, and journal/replay failure.

### Stage 4 — Preregistered confirmation

Freeze hypotheses, primary outcomes, policy implementations, mutation manifests, task split,
sample size, exclusions, and analysis. Then expand across several model families and held-out task
templates. Add qualitative VFX only after executable safety results are stable.

### Stage 5 — Artifact and paper

Release fixtures, mutation manifests, reference oracles, policy adapters, raw event logs, analysis
code, and a reproduction script. Separate the benchmark artifact from any VFX Harness production
change so reviewers can run the provider-neutral core without Blender.

## Related-work position as of 2026-08-31

This is a scoping map, not yet a completed systematic review.

| Work | What it already establishes | Remaining empirical angle for this study |
|---|---|---|
| [Exact Checking for Execution Edits](https://arxiv.org/abs/2608.22928) | Formal exact checking for checkpoint/fork/restore/merge under workflow edits | Cross-domain empirical policies, journal/state-publication failures, cost and maximal reuse |
| [BEGIN AI TRANSACTION / SemIso](https://arxiv.org/abs/2608.05412) | Semantic isolation over versioned models, prompts, tools, indexes, and policies | Controlled resume admission after same-ID task/closure mutation |
| [Resume Means Resume](https://arxiv.org/abs/2608.03836) | Machine-checked recovery contract and cross-framework fault matrix | Authority drift and task-relevant semantic equivalence |
| [DART](https://arxiv.org/abs/2605.23311) | Dependency/effect-aware recoverability for structured tool agents | Changed task/authority rather than recovery inside a fixed task |
| [ContinuityBench](https://openreview.net/pdf?id=3N3BzvoLbG) | Agent performance after interruption with different handoff context | Authorization of persisted work after semantic mutation |
| [AgentRewind](https://arxiv.org/abs/2608.14380) | Aligned context and workspace rewind for fixed task instructions | Whether a saved workspace remains authorized when the task changes |
| [InterruptBench](https://arxiv.org/abs/2604.00892) | Agents adapting to revised or retracted user intent | Checkpoint admission where prior progress becomes invalid |
| [ACRFence](https://arxiv.org/abs/2603.20625) | Semantic rollback attacks including authority resurrection | Non-adversarial correctness benchmark plus reuse/efficiency trade-off |
| [Temporary Authority, Permanent Effects](https://arxiv.org/abs/2607.10487) | Commit-time authorization and authorized completion | Resume-state provenance before commit across artifact environments |
| [OSWorld](https://proceedings.neurips.cc/paper_files/paper/2024/hash/5d413e48f84dc61244b6be550f1cd8f5-Abstract-Datasets_and_Benchmarks_Track.html) | Reproducibly initialized computer-use tasks | Persisted-state authorization after task mutation |
| [TheAgentCompany](https://proceedings.neurips.cc/paper_files/paper/2025/hash/0d744742f6fac4d1134c019b7cef3c8a-Abstract-Datasets_and_Benchmarks_Track.html) | Workplace task completion in a simulated environment | Resume correctness under changed authority |
| [Nextflow cache and resume](https://docs.seqera.io/nextflow/cache-and-resume/) | Task hashes bind inputs, script, environment, and session state | Agent-specific semantic authority, journal, and evaluated-state concerns |
| [Temporal workflow versioning](https://github.com/temporalio/documentation/blob/main/docs/develop/dotnet/workflows/versioning.mdx) | Versioning is required because code changes can break deterministic replay | Content-level active-task mutation and empirical admission policies |

Claims to avoid:

- “The first interruption or resume benchmark.”
- “The first proof that checkpoints are insufficient.”
- “The first semantic recovery system.”
- “Exact global authority generation must always match.”

A defensible provisional claim is:

> We introduce a controlled benchmark for authorized continuation under task-relevant authority
> drift, unifying same-ID semantic mutation, causal artifact identity, committed journal cuts, and
> runtime state publication while measuring both unsafe reuse and false refusal.

Use “introduce” only after the systematic review confirms that this exact combination and protocol
are not already evaluated.

## Repository evidence behind each identity field

- Active task digest: [HIR-0059](../improvements/HIR-0059-warm-start-artifacts-must-match-unit-digest.md)
  and `orchestration/unit_state.py`.
- Relevant rather than monolithic authority: [HIR-0040](../improvements/HIR-0040-sibling-view-hash-is-not-a-dag-change.md),
  [HIR-0102](../improvements/HIR-0102-remat-replan-base-is-durable-unit-state.md), and
  [HIR-0133](../improvements/HIR-0133-direct-materialization-reconciles-durable-state.md).
- Ordered dependency and interface closure: [HIR-0084](../improvements/HIR-0084-successor-interfaces-are-typed-references-bound-to-producer-digests.md)
  and [HIR-0119](../improvements/HIR-0119-replay-order-is-derived-from-the-unit-dag.md).
- Artifact plus causal parent identity: [HIR-0053](../improvements/HIR-0053-runtime-image-payment-needs-provenance.md)
  and ADR-0008.
- Exact journal start/end and checkpoint fidelity: [HIR-0014](../improvements/HIR-0014-instruments-that-cannot-lie.md)
  and [HIR-0054](../improvements/HIR-0054-active-unit-context-must-not-scale-with-the-shot.md).
- Checkpoint/journal state equivalence: [HIR-0068](../improvements/HIR-0068-authored-blender-calls-must-rollback-on-error.md).
- Replay publication semantics: [HIR-0117](../improvements/HIR-0117-artifact-replay-publishes-evaluated-state.md).

These are incident anchors, not independent experimental evidence. Several are single-system
production failures. Their role is to motivate the mutation taxonomy; the benchmark must supply
the controlled evidence.

## Implementation route in this repository

Future benchmark code should live at:

```text
src/vfx_harness/evaluation/resume_authority/
  schema.py
  identity.py
  policies.py
  injectors.py
  runner.py
  grade.py
  report.py

evals/resume_authority/                       # tracked fixtures and study specs
artifacts/evaluations/resume-authority/<id>/  # generated trials
```

Add a public `vfx evals resume-authority` command only after the model-free checker and oracle are
tested. Reuse current plan authority, unit-state, topological ordering, interface digests, replay,
acceptance, run-artifact, transcript, and cost-log boundaries.

Do not use the current public conversation-resume path as the reference implementation. Its resume
record is materially weaker than accepted checkpoint/revalidation provenance, and the current
builder lifecycle clears the stored slot before the later resume lookup. A controlled evaluation
adapter should implement each policy explicitly so an implementation defect is not confused with
the scientific treatment.

## Venue fit

- **ICSE/FSE:** strongest fit for an empirical study of runtime correctness, fault injection, and a
  reproducible artifact.
- **MLSys:** strong if the contribution includes a practical provenance admission layer, runtime
  integration, overhead analysis, and cross-system generality.
- **NeurIPS/ICLR benchmarks or workshops:** plausible only with a substantially larger public task
  corpus, several systems/models, strong benchmark maintenance, and a completed novelty review.
- **Workshop-first pilot:** appropriate if the initial contribution is the taxonomy, oracle, and
  two-environment feasibility result.

## Immediate go/no-go gates

Proceed to model spending only if all are true:

1. The systematic review extraction matrix is complete enough to freeze the claim.
2. Every mutation is isolated and receives the expected label from a policy-independent oracle.
3. Safe equivalence controls exist for global authority and independent-DAG changes.
4. The scoped certificate's fields can be recomputed from current authority rather than trusted
   from the saved record.
5. The Blender replay oracle includes the evaluated-state publication barrier.
6. Usage can be attributed at phase boundaries.
7. The held-out confirmatory task split and analysis plan are frozen before results are inspected.

If those gates hold, the next engineering step is Stage 1: the provider-neutral identity,
injection, and oracle microbenchmark—not a large multi-model run.
