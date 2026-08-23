---
id: HIR-0012
title: Publish the next unit, not the shot — a sparse global publication contract
status: proposed
introduced_in: unreleased
date: 2026-08-23
failure_class: global_planning_performs_whole_shot_preproduction
mechanism: ownership_only_registration_and_mechanically_rejectable_publication_contract
adr: ADR-0005
---

# Publish the next unit, not the shot — a sparse global publication contract

## Outcome

Global planning establishes a rough dependency-ordered layer DAG, durable shot-wide constraints,
whole-brief requirement *ownership*, and the first dependency-ready executable unit — then
publishes. Everything else is a typed debt owed by a named layer at a named boundary, designed at
that layer's JIT materialization, where its dependencies exist and can falsify it.

The sparse artifact is a **publication contract**, not a planning style: a global bundle
containing later-layer evidence design is rejected by the deterministic gate exactly as one
missing Layer-1 authority is. Overplanning stops being a behavior to discourage and becomes a
shape that cannot publish.

The operating rule extends HIR-0011's:

> Plan enough to mutate safely; build the smallest useful falsifier; revise authority from
> executable reality — and decide everything else at the boundary where it becomes real.

## Observed failure

Five consecutive planning runs on the current shot were stopped before publication:

- `20260823T005740Z-079242` calibrated later-layer image metrics through five shrinking batches
  before writing any candidate.
- `20260823T011703Z-c3306a` behaved exactly as instructed — ten reference measurements, two
  exploratory checks, one batch, zero spikes, correctly deferred stubs — and still owed the gate
  a full requirements register with whole-shot resolution typing; 65 blockers followed.
- `20260823T021919Z-8122f6` spent $12.13, 54 minutes, 133 turns, and 233,799 output tokens on a
  full register, whole-shot contract manifest, and typed later-layer promises; 16 of its 23
  blockers were promise-schema cascades.
- `20260823T041822Z-8173dc` degraded concrete promise kinds into generic evidence categories to
  satisfy a loader that demanded typing for unbuilt layers.
- `20260823T050739Z-7781dd` spent its first six minutes on whole-shot analysis, then built a
  proxy Blender scene to "prove" an approved value and a lighting adversary.

The decisive observation is that the planner was **obeying, not misbehaving**. Every run's
whole-shot phase is mandated by the current gate contract: the requirements register demands
every substantive clause resolved to typed evidence at global time; deferred-layer promises
demand exact contract kinds and moments at global time — authoring `frame_delta [239, 240]` for
layer 6 is layer-6 evidence design in compressed form; acceptance fingerprints demand all ten
approval frames measured at global time; and the verifier audits that entire surface. HIR-0011
moved world-model falsification to build time and later-layer tickets to materialization, but
the *analysis* obligation never moved.

The same pressure produces the secondary failures. When the gate forbids proxy evidence but the
contract still demands whole-shot certainty, the model manufactures certainty wherever a tool
still accepts the request (`7781dd`). And each such failure has been generalized into a new
whole-shot rule — temporal-language word lists, composition tokens, lighting screens — so
shot-specific vocabulary accretes into generic mechanism while the obligation that caused the
failure survives.

Baseline for acceptance: the five stopped runs (none published; best full attempt $12.13/54min
without reaching a gate-clean candidate) on top of the recorded $219.71/19-run ledger, with zero
accepted build units.

## Root cause

The published-plan contract conflates two different global responsibilities:

- **Coverage** — proving no brief clause can silently vanish. This genuinely requires reading
  the whole brief once, at global time, with adversarial verification. It is cheap: citation,
  owner, due boundary.
- **Design** — choosing contract kinds, moments, thresholds, techniques, and calibrations. This
  requires the dependencies the evidence will run against, which do not exist at global time,
  so global design is guessing that must later be defended, repaired, or falsified at model
  expense.

Every prior slice narrowed *execution* scope while leaving *design* scope whole-shot. The
result: correct planner behavior still costs an hour of preproduction per attempt, and the
harness keeps discovering "one more seam" because the seams are all facets of the same mandated
guessing.

## Decision criteria

The mechanism must:

- retain singular, immutable, content-addressed plan authority and pointer selection;
- retain whole-brief registration with adversarial omission detection;
- retain decision strengths, falsification routing, mutation scoping, and replay unchanged;
- make later-layer evidence design unrepresentable in a publishable global bundle;
- give every deferred requirement exactly one owner layer and due boundary;
- fail materialization closed until owned requirements gain concrete executable evidence;
- keep Layer 1 directly executable from the published bundle;
- keep the four-question deferral boundary generic — free of this shot's vocabulary;
- preserve the economic criterion: cost and latency to first authoritative scene evidence.

## The publication contract

### A published global bundle MUST contain

1. **The layer DAG** — Layer 1 `ready` with unit plans, claims, scene/image contracts, and
   acceptance fingerprints for Layer 1's judge frames only; every later layer `jit_deferred`
   with `depends_on_layers`, `required_outcomes`, `reserved_roles`, judge milestones, and
   `owned_requirements`.
2. **The ownership register.** Every substantive brief clause is cited and resolved to exactly
   one of: a concrete resolution (contract/obligation/decision) when it passes the
   global-exception test, or a **deferred ownership** — a resolution kind naming one owner
   layer whose materialization boundary is the due gate. Ownership is coverage, not design.
3. **Durable constraints** — typed decision records with strengths, adopted values, and
   falsification paths, for global-exception material only.
4. **Genuine blockers** — open client questions that prevent the first unit from starting.

### A published global bundle MUST NOT contain (blocking findings)

- scene or image contracts owned by deferred layers (already enforced);
- reference fingerprints for frames no published unit judges;
- calibration evidence, technique research citations, or recipe selections attached to
  deferred-layer material;
- promise-level evidence design: contract kinds, moments, or thresholds inside a `jit` block.

### The global-exception test

A decision may be resolved concretely at global time only when at least one holds:

1. the first unit cannot execute without it;
2. it alters the global DAG — dependencies, reserved role namespaces, judge milestones;
3. it is irreversible;
4. it is expensive to be **wrong about later** — not merely expensive to decide now.

Otherwise the answers (no, yes, no, no) to "needed now / better tested by its producing unit /
alters the DAG / expensive to be wrong" mandate deferral, and material carrying the decision
blocks publication.

### Promise semantics

`jit.promises` (`id`, `contract_kind`, `moments`, `requirement_ids`) and the obligation
consumption plumbing (`jit_contract` evidence, before-layer coupling, kind/moment matching at
materialization) are superseded by `jit.owned_requirements`: the requirement ids the layer owes.
The materialization gate closes the debt at requirement granularity — every owned requirement
must resolve to at least one **required executable contract bound to a producing claim** in the
materialized DAG, or to an explicit typed decision recorded at materialization — and fails
closed otherwise. Kind, moments, and thresholds are chosen there, against real upstream
outcomes, and validated by the same gate that already refuses structural drift, namespace
escapes, and missing producing claims.

### Verification charter

The verify pass narrows to what only an adversary can do:

- omission detection: every substantive brief paragraph, list item, and table row overlaps a
  register citation, and each owner assignment is defensible;
- Layer-1 executability: the first unit's claims, contracts, scopes, and dependencies close;
- exception audit: everything concrete beyond Layer 1 names which global exception admits it.

No whole-shot look auditing, no reachability certification, no later-layer review. The draft
session budget defaults near 24 turns; verification is bounded below that.

## Boundary stress test — A2

The camera-spine decision is the deliberate hard case, because it *looks* like the epitome of
whole-shot planning: sixteen human-approved keyframes spanning f1–f240, embedded as a
`values.contract`, keyed verbatim by the first unit, with later layers forbidden to re-key.

The test keeps it global on three independent grounds: the first unit cannot execute without it
(q1); it is a whole-DAG dependency every later layer builds against (q2 in the exception sense);
and it is a human approval that is expensive to be wrong about later — which is precisely why it
carries a falsification path to unit 1's f36 evidence rather than a proxy proof (q4). Likewise
A5's 24-module count (first-unit material, q1) and A6's X=0 alignment (cross-DAG, q2) stay
global.

The same test defers what the stopped runs kept pulling forward: bloom/glare tuning belongs to
the finish layer's materialization; lighting design and the `7781dd` "unlit adversary" belong to
the lighting owner; reactor scale and fracture behavior belong to their layers. Each is better
tested by its producing unit, alters no DAG edge, and is cheap to revise before its layer
materializes — (no, yes, no, no) — so global planning must not decide it, and under this
contract cannot publish it.

## Economics

Planning spend moves; this HIR does not claim it shrinks. A sparse global pass is followed by
one materialization gate per layer, each a smaller session with fresh-context overhead. The bet
is that dollars spent after dependencies exist buy falsifiable decisions instead of defended
guesses. The standing criterion therefore remains the arbiter: **cost and latency from `vfx
plan` invocation to first authoritative scene evidence**, measured against the five-stopped-run
baseline in which that evidence was never reached and the fullest attempt spent $12.13 and 54
minutes without publishing.

## Rejected alternatives

- **Keep typed kind/moment promises at global time.** Two of five stopped runs failed inside
  promise typing; choosing kind and moments is design, and design without dependencies is
  guessing with a schema.
- **Defer registration per layer.** The 239→240 clause vanished from two independently authored
  plans; whole-brief coverage at global time is the one analysis with a repeated record of
  catching silent omission. Coverage stays; design moves.
- **Prompt guidance plus tighter budgets, contract unchanged.** `7781dd` proved prose does not
  bind tools; a 48-turn ceiling on mandated whole-shot work truncates it rather than removing
  it.
- **Full JIT including the DAG.** Dependencies, reserved namespaces, judge milestones, and hard
  constraints are exactly what downstream sessions cannot safely change (ADR-0004); deferring
  them reopens silent-corruption classes.
- **Advisory sparseness.** A style the gate does not reject regresses the first time a draft is
  thorough; the contract must make overplanning unpublishable, not discouraged.

## Proposed implementation slices

### Slice 1 — Record the decision

Land ADR-0005 and this HIR; state in both public summaries that `clean` now certifies the
sparse publication contract.

### Slice 2 — Ownership register

Add the deferred-ownership resolution kind to `requirements.json` (owner layer + due boundary),
loader validation (exactly one owner; owner must be a declared deferred layer), and gate closure
(every substantive clause covered; ownership needs no kinds or moments). Registration remains
blocking for coverage gaps.

### Slice 3 — Sparse `jit` block and materialization closure

Replace `jit.promises` with `jit.owned_requirements` in the layer loader; move requirement-level
debt closure into `validate_materialization`: every owned requirement resolves to required
executable evidence bound to a producing claim, or an explicit typed decision, else the
materialization fails closed. Retire the `jit_contract` evidence plumbing and promise
kind/moment matching it superseded.

### Slice 4 — Publication-contract negatives

Blocking gate findings for later-layer fingerprints, calibration evidence, technique/recipe
citations, and any evidence design inside a `jit` block; acceptance fingerprints restricted to
published judge frames, with later milestones arriving through the materialized consumer view
(extend the overlay artifact set to `acceptance.json`).

### Slice 5 — Verification charter and budgets

Narrow the verifier prompt to omission detection, Layer-1 executability, and the exception
audit; default the draft budget near 24 turns with the existing override; bound verification
below the draft.

### Slice 6 — Fixtures and the shot

A heterogeneous fixture (no iris/reactor/fracture vocabulary) proving: ownership-only
publication, fail-closed materialization of an owned requirement, and the negative findings. Then
the current shot: fresh sparse plan, replan, build unit 1, and record the economics against the
baseline.

## Acceptance conditions

- A complete ownership register can publish without later-layer contract kinds or moments.
- Every later-layer requirement has exactly one owner and due boundary.
- Materialization fails closed until its requirements gain concrete executable evidence.
- Layer 1 remains directly executable from the published global bundle.
- Whole-shot fingerprints, technique research, and check calibration are absent unless required
  by a named global exception.
- A heterogeneous fixture proves the mechanism without iris/reactor/fracture vocabulary.
- The draft budget defaults near 24 turns.
- Measured cost and latency to first authoritative scene evidence improve over the
  five-stopped-run baseline.

## Validation plan

Focused tests per slice (register kind, loader symmetry, materialization closure, negative
findings, budget default), the heterogeneous fixture, and repository verification from the root:

```bash
.venv/bin/ruff check src tests
.venv/bin/python -m pytest -q
.venv/bin/vfx --help
git diff --check
```

Do not mark this HIR accepted from unit tests alone: acceptance requires a published sparse
bundle for the current shot and its first authoritative Layer-1 scene evidence, with the
economics recorded here.

## Implementation progress — 2026-08-23

Slices 2–5 are implemented pending the runtime acceptance above:

- `requirements.json` supports `deferred_owner` with one owner layer and matching
  `before_layer` due gate; deferred `jit` rows carry only `owned_requirements`.
- The global gate accepts ownership as complete coverage while rejecting contracts and
  acceptance fingerprints for deferred layers. Temporal vocabulary checks stop at ownership
  instead of demanding future evidence design.
- JIT materialization closes every owned requirement with required producing contracts or a
  typed decision, and publishes cumulative `requirements.json` and `acceptance.json` overlays.
- Global kickoff no longer embeds all future stills. `measure_ref` is mechanically restricted
  to ready-unit references, while materialization receives its own layer references.
- Draft and verification defaults are 24 and 12 turns respectively; the verifier charter is
  omission coverage, Layer-1 executability, and global-exception auditing.
- The generic finish/polish fixture covers ownership-only publication, missing-debt failure,
  typed-decision closure, deferred fingerprint rejection, and rejection of superseded promises.

Repository verification passed with 165 tests before the required live sparse-plan acceptance;
the live economics and first authoritative scene evidence remain the acceptance boundary.

## Release and rollback

Implement behind the plan/build policy version, as with HIR-0011: existing immutable bundles
retain their recorded policy and remain readable; new publications record that the sparse
contract classified them. Rollback restores the previous contract for new runs without mutating
published bundles, durable state, or the register schema of already-selected authority.

## Remaining limitations

- Ownership can be misassigned at registration; the correction is a typed replan at the owner's
  materialization gate, which costs a session. The omission verifier reviews assignments but
  cannot prove intent.
- Per-layer materialization sessions add fresh-context overhead that the economic criterion must
  catch if it dominates.
- Later-milestone acceptance fingerprints depend on extending the materialized consumer view to
  `acceptance.json`; until that slice lands, whole-shot acceptance remains partially global.
- The accreted shot-vocabulary heuristics (temporal word lists, composition tokens, lighting
  screens) are demoted in effect by ownership-only coverage but still exist in gate code; their
  dedicated audit is deliberately out of scope here.
- Cross-layer aesthetic coherence relies on authored inputs and durable constraints rather than
  a single up-front design; the brief and references remain the only whole-shot look authority.
