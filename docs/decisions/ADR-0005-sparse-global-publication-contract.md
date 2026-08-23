---
id: ADR-0005
title: Sparse global publication contract with ownership-only registration
status: proposed
date: 2026-08-23
supersedes: null
---

# Sparse global publication contract with ownership-only registration

## Context

Five consecutive planning runs on one shot (`20260823T005740Z-079242`, `011703Z-c3306a`,
`021919Z-8122f6`, `041822Z-8173dc`, `050739Z-7781dd`) were stopped before publication. Each stop
exposed a progressively narrower harness seam, but every run shared one shape: minutes to an hour
of whole-shot analysis — all ten approval frames measured, techniques researched, contracts and
promise types designed for layers whose dependencies do not exist — before any executable
authority emerged. The fullest attempt cost $12.13 and 133 turns without publishing. The shot has
zero accepted build units against a $219.71 pre-HIR-0011 planning ledger.

The planner was not misbehaving; it was obeying. HIR-0011 and its slices moved world-model
*falsification* to build time and later-layer *tickets* to JIT materialization, but the analysis
obligation never moved: the requirements register demands every clause resolved to typed evidence
at global time, deferred-layer promises demand exact contract kinds and moments at global time
(choosing `frame_delta [239,240]` for layer 6 is layer-6 design in compressed form), acceptance
fingerprints demand all reference frames measured at global time, and verification audits that
whole surface. Two of the five stops were promise-typing seams — direct evidence that carrying
concrete evidence design for unbuilt layers is load-bearing complexity with recurring failure
modes. A third stop caught the planner manufacturing proxy-scene evidence for an approved value,
which is what "prove everything now" pressure produces when the gate finally forbids everything
else.

Shot-specific vocabulary has also been accreting into generic mechanism — temporal-language word
lists, composition tokens, lighting screens — because each whole-shot analysis failure was
generalized into a whole-shot analysis rule rather than removed with the obligation that caused
it.

## Decision

The published global plan is governed by a **publication contract** — a mechanically checkable
definition of what a global bundle must and must not contain. Overplanning becomes a blocking
gate outcome, not advisory prose.

A published global bundle MUST contain exactly:

1. **A dependency-ordered layer DAG** — Layer 1 `ready` with its executable unit plan(s),
   contracts, and claims; every later layer `jit_deferred` with upstream dependencies, required
   upstream outcomes, reserved role namespaces, and owned requirements.
2. **An ownership-only requirements register.** The whole brief is read once; every substantive
   clause is registered with its citation and resolved to one of: a concrete Layer-1 or
   global-exception resolution, a typed decision, or a **deferred ownership** naming exactly one
   owner layer and due boundary. Registration is coverage, not design: no contract kinds,
   moments, thresholds, or evidence bindings for deferred owners.
3. **Durable shot-wide constraints** — decision records with strengths, adopted values, and
   falsification paths for everything passing the global-exception test below.
4. **Genuine blockers** — open questions that prevent the first unit from starting.

Concrete evidence design — contract kinds, moments, thresholds, calibration, technique research,
reference fingerprints beyond Layer 1's judge frames, execution tickets — belongs to each layer's
JIT materialization gate, which fails closed until every owned requirement resolves to required
executable evidence or an explicit typed decision.

A decision may be made globally only when it passes the **global-exception test**:

- it is needed before the first unit can execute; or
- it alters the global DAG (dependencies, reserved namespaces, judge milestones); or
- it is irreversible; or
- it is expensive to be *wrong about later* — not merely expensive to decide.

Otherwise the answer set (no, yes, no, no) to "needed now / better tested by its producing unit /
alters the DAG / expensive to be wrong" mandates deferral, and the gate blocks the material.

Promise semantics change accordingly: the `jit` block's typed promises
(`id`/`contract_kind`/`moments`) and their obligation-consumption plumbing are superseded by
`owned_requirements` — the requirement ids the layer owes evidence for. The register keeps the
omission net whole-shot; the design moves to where dependencies exist.

Verification narrows to its irreplaceable work: adversarial omission detection across the
ownership register, Layer-1 executability, and audit that everything concrete beyond Layer 1
names its global exception. The draft session budget defaults near 24 turns.

This decision does not authorize weakening any authority mechanism: singular immutable bundles,
pointer selection, decision strengths, falsification routing, materialization fail-closed
closure, mutation scoping, and replay all continue unchanged.

## Consequences

- Global planning becomes cheap enough to be routine: read, register, constrain, plan unit 1,
  publish. The expensive analysis happens per layer, after that layer's dependencies exist and
  can falsify it.
- Planning spend *moves* rather than disappears: more, smaller JIT sessions, each with fresh
  context overhead. The bet is that dollars spent after dependencies exist buy falsifiable
  decisions instead of guesses. The standing economic criterion — cost and latency to first
  authoritative scene evidence — stays the arbiter, and acceptance requires it to improve over
  the five-stopped-run baseline, not merely for total spend to shrink.
- The just-built promise kind/moment machinery is narrowed days after landing. That churn is
  accepted: two of five stopped runs failed inside it, which is the evidence it was the wrong
  boundary.
- Ownership can be misassigned at registration time. The correction path is the existing typed
  replan transaction at the owner's materialization gate, not silent reassignment.
- Whole-shot aesthetic coherence rides on the brief, references, durable constraints, and
  reserved interfaces rather than on one up-front plan; each materialization reads the same
  authored intent.

## Rejected alternatives

- **Keep typed kind/moment promises at global time.** Choosing kind and moments is evidence
  design; runs `8122f6` (16 of 23 blockers were promise-schema cascades) and `8173dc` (kinds
  degraded to generic categories to satisfy the loader) failed exactly there.
- **Defer registration itself to each layer.** The 239→240 final-lock clause vanished from two
  independently authored plans; whole-brief coverage with fresh eyes is the one global analysis
  with a repeated record of catching silent omission. Coverage stays global; only design defers.
- **Tighten prompts and budgets without changing the contract.** Run `7781dd` proved prose rules
  do not bind tools, and a 48-turn ceiling on mandated whole-shot work only truncates it
  mid-flight.
- **Full JIT including DAG shape.** Cross-layer dependencies, reserved namespaces, and hard
  constraints are exactly the decisions downstream sessions cannot safely change; deferring them
  reintroduces silent-corruption classes ADR-0004 closed.

## Validation and review trigger

HIR-0012 carries the acceptance conditions: publishable ownership-only register; exactly one
owner and due boundary per deferred requirement; fail-closed materialization closure; Layer 1
directly executable from the published bundle; whole-shot fingerprints, research, and calibration
absent without a named global exception; a heterogeneous fixture free of this shot's vocabulary;
a draft budget near 24 turns; and measured cost/latency to first authoritative scene evidence
improving on the five-stopped-run baseline.

Review this decision if materialization-time design produces repeated cross-layer rework that
global design would have prevented (the economic criterion regressing while ownership coverage
holds), or if omission escapes appear that the ownership register plus adversarial verification
fail to catch.
