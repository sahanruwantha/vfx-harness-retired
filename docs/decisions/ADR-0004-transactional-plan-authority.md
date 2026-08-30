---
id: ADR-0004
title: Transactional plan authority with typed lifecycles and declared capabilities
status: proposed
date: 2026-08-21
supersedes: null
---

# Transactional plan authority with typed lifecycles and declared capabilities

## Context

Two planning runs on one brief (`20260821T115227Z-a8f966`: $19.27, terminated dirty while
reporting `passed`; `20260821T135111Z-f8e73c`: $9.86, clean after ADR-0003) exposed five
recurring mechanisms rather than isolated defects:

1. **Validation authority is not available at mutation time.** The deterministic gate holds
   the only executable definition of a valid plan; authoring and verification model passes
   learn it by failing it. Run 2's draft+verify produced 17 blocking findings against checks
   that already existed, and its verifier certified a "full claim-closure scan" clean
   immediately before the gate found eleven claim-closure defects.
2. **Machine facts are duplicated into load-bearing prose.** Fingerprints, deferred-evidence
   promises (`note_for_jit`), repair dispositions, and assumption status exist as prose that
   machines later regex-parse or silently ignore. Brief-critical obligations (exact-return
   transforms, the final two-frame lock) survive only as sentences no check can see.
3. **Findings, assumptions, and deferred obligations lack typed lifecycles.** A genuine
   scope violation (a control governing a role outside its unit's mutation scope) is
   indistinguishable at warn severity from fourteen migration chores; an unanswered global
   question does not alter a `clean` outcome.
4. **Agent roles lack tested capability contracts.** A verifier instructed to edit had
   `Edit` disabled; a drafter without `Edit` re-emitted an ~850-line artifact to change one
   field; a repair-owned gate tool required discovery through search.
5. **Planning lacks a run-isolated transactional publication boundary.** Shot-root
   authority is mutated live as agent workspace: the authoritative plan was absent
   mid-run, and run 2 overwrote run 1's repair snapshots.

## Decision

Authority is singular, transactional, and typed; state has a lifecycle; capability is
declared and tested; and every discipline imposed on the shot applies to the harness's own
artifacts.

This ADR records the target authority model. Recording it does not publish that model or make
the current shot-root artifacts compliant. The decision remains `proposed` until the migration
is implemented and the acceptance evidence below passes; only then may it become `accepted`.
[HIR-0008](../improvements/HIR-0008-transactional-plan-publication-foundation.md) records the
accepted first implementation slice and its deliberately incomplete acceptance coverage.

- **Immutable plan bundles, atomic pointer publication.** One plan generation is one
  immutable bundle (`global.md`, `layers.json`, `acceptance.json`, `checks.json`,
  `scene_checks.json`, `requirements.json`, `obligations.json`, `assumptions.json`)
  identified by producing run and content hash. Publication atomically replaces a single
  pointer (`plans/current.json`) carrying bundle id, content hash, and gate outcome.
  Readers resolve the whole set through the pointer: always exactly one complete
  generation, never a mix. Staging lives under the producing run; superseded bundles remain
  immutable run artifacts. Legacy shot-root plan reads are removed, not shadowed.
- **Three validation tiers, one shared validator set.** Per-write validation checks shape
  and local constraints. Staging validation reports relational findings continuously while
  permitting incompleteness. Commit validation blocks on every relational invariant. The
  independent publication gate re-runs the same validators from a separate process, adds
  whole-set checks (grounding, citations, coverage), and alone computes `clean`,
  `clean_with_assumptions`, or `clean_with_deferred`.
- **Typed meta-records with distinct treatments.** Invariant violations (mechanically
  decidable, always wrong — e.g. control-to-role closure) block unconditionally and cannot
  be lifecycled. Obligations carry an owner and a due gate expressed against dependency
  outcomes — never shot-specific stage names or frame numbers — and block the stage they
  come due at. Assumptions propagate into every outcome until explicitly resolved;
  resolution is a new record in durable state outside the immutable bundle, keyed to
  assumption id + bundle hash, folded into the next generation.
- **Requirements register with content-addressed citations.** The planner extracts
  normative brief requirements into `requirements.json`, each citing the brief content hash
  plus line/span, so citations cannot silently drift after edits. The gate mechanically
  enforces register → (contract | obligation | decision) closure. Register completeness is
  the adversarial verifier's charter.
- **Structurally narrowed verifier output.** Verify operates as a second authoring
  transaction and emits only: missing requirement candidates, disputed register entries,
  qualitative risks, proposed edits (through the same transactional operations), and
  `no_additional_findings`. Its output type contains no aggregate cleanliness verdict;
  only the publication gate declares gate-covered categories clean.
- **Role capability manifests.** Each role declares its workflow verbs; the harness
  materializes exactly those tools, preloaded and named in context; a contract test asserts
  every prompt-required verb has a live tool.
- **Migration windows fail closed.** Every compatibility mode declares a deterministic
  expiry condition; after expiry, legacy forms are rejected. Prose fingerprints and
  unmapped `control_roles` are the first two sunsets.

### Amendment: structural cleanliness and executable falsification

Plan commit validation certifies **structurally executable authority**, not the truth or
reachability of scene state that does not exist yet. A `clean` plan proves bundle identity,
requirement closure, ownership, dependency order, mutation scope, evidence bindings, due gates,
and decision authority. It does not claim that a future projected bbox, transform, timing,
lighting response, or interaction already passes.

Scene-dependent values carry an explicit strength: `hard_constraint`, `approved_start`,
`planner_start`, or `confirmed_outcome`. Legacy decisions with no strength are read as hard
constraints so migration cannot silently grant automation more authority. Approved/planner
starts name the producing unit and exact runtime contracts that can falsify them. Only accepted
executable evidence pinned to a checkpoint may create a confirmed outcome.

An approved/planner start that resolves a requirement deferred to one layer remains
mandatory composed-layer judgment debt. Its exact statement is judged independently on
cumulative canonical replay at the layer's declared reference moments. Look-less form uses
Workbench solid through the active camera; failure records replanning evidence and grants no
synthetic cross-unit repair scope (HIR-0137).

When passing requires a decision, dependency, ownership, mutation-scope, contract, or sealed-
outcome change outside the active unit, the unit records `hypothesis_falsified` with immutable
bundle/unit/candidate identities and stops. Transactional replanning consumes that record,
preserves unaffected checkpoints, and invalidates the dependency closure. Ordinary misses that
remain repairable inside declared authority do not become plan findings.

Plan spikes are optional mechanism evidence. A ticket claiming `spiked` must cite immutable exact
script/output/runtime bytes and any contract rows it claims to have executed. Composition ownership
alone never makes a spike mandatory, and a passing spike cannot confirm a future cumulative scene.

[HIR-0011](../improvements/HIR-0011-build-time-plan-falsification.md) records the implementation,
migration, and economic acceptance evidence for this amendment.

## Consequences

- Authoring converges in-session against live validation; the outer gate is normally clean
  and cheap, and rounds stop being the unit of learning.
- Contradiction between verifier and gate becomes a type error, not a runtime discovery.
- Every plan consumer must migrate to pointer resolution in one cut (strict migration).
- Bundle storage grows per generation (text-scale; retain all initially).
- Build and accept stages later consume obligations coming due — plan and build share one
  obligation ledger, so this schema is a cross-stage contract, not a plan-stage detail.

## Rejected alternatives

- **Patching individual findings** (earlier gate call, prompt warnings): treats symptoms;
  each of the five mechanisms keeps generating new instances.
- **Sequential replacement of shot-root files**: readers observe mixed generations;
  observed in production (authority absent mid-run, snapshots overwritten).
- **Schema-only authoring validation**: shape checks cannot express claim-axis coherence,
  frame coverage, role/control closure, or dependency scope; the authoring operation must
  invoke the domain validators transactionally.
- **Verifier prompt-tuning against overconfidence**: a model pass re-deriving executable
  rules from prose will re-certify wrongly; removing the verdict from its output type is
  enforceable, tone is not.
- **Indefinite compatibility modes**: demonstrated to convert hard invariants into
  permanent warnings.

## Validation and review trigger

Acceptance tests — written first, as failing fixtures, before the bundle/transaction
migration begins — run on two heterogeneous fixtures with injected failures, each required
to surface at authoring time rather than in a paid repair round:

1. A control governing an undeclared role is rejected at the authoring write.
2. A new prose fingerprint cannot be committed.
3. A layer cannot start while an obligation due at it is unresolved (exact-return,
   final-lock class).
4. An unanswered global question yields `clean_with_assumptions` or blocks at its declared
   due gate.
5. Every prompt-required verb passes the role capability contract test.
6. Interrupted or concurrent planning never removes or mixes published authority.
7. Run snapshots are immutable and never overwrite another run's.
8. Verifier output asserting a gate-covered category clean is unrepresentable.
9. An expired migration window rejects legacy-form records.
10. Every normative brief requirement resolves to a contract, obligation, or decision;
    register omissions are found by the adversarial pass, whose findings are validated
    against the register rather than self-certified.

Review this decision if bundle promotion latency becomes material, if a heterogeneous
fixture shows obligations cannot be expressed against dependency outcomes without
shot-specific policy, or if the requirements register proves unauditable in practice.
