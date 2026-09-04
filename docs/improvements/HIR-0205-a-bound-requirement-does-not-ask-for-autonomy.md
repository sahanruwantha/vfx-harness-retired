---
id: HIR-0205
title: A bound requirement asks whether the row passed, not whether it may veto alone
status: accepted
introduced_in: unreleased
date: 2026-09-04
failure_class: one_boolean_answered_two_different_authority_questions
mechanism: named_predicates_for_bound_satisfaction_and_unbound_veto_shared_by_every_consumer
adr: null
---

# A bound requirement asks whether the row passed, not whether it may veto alone

## Observed failure

hansa run `20260904T143358Z-238376`, layer 2, unit `hero_facade`, on `b87035c`. The unit
built, its critic sealed, and `probe_candidate` reported every evidence row passing. The
canonical evaluator produced all three bound image contracts and all three passed. From
that run's boundary audit, verbatim:

```
{"id": "hero-look-f201-mean", "source": "image_contract", "origin": "builder",
 "metric": "region_mean", "target": "15..90", "value": 54.409,
 "pass": true, "authoritative": false, "owner_layer": "2"}
```

The unit-outcome receipt then raised `canonical evaluator did not produce passing
required evidence` naming those exact ids. The message was false about what happened, and
the run died at exit 1 with an unclassified boundary packet after $12.48 of spend.

Tallying every evidence row in that audit by `(source, authoritative, pass)`:

| rows | source             | authoritative | pass  |
|-----:|--------------------|---------------|-------|
|   61 | interface_contract | true          | true  |
|    4 | image_contract     | false         | true  |
|   21 | (none)             | false         | false |

Every scene row is autonomous; no image row is. Layers whose units bind only scene
contracts sealed cleanly, which is why the defect survived to a third shot.

## Root cause

One boolean field carried two different questions.

`evidence/checks.py` mints `authoritative` to answer: *may this row block acceptance on
its own, with nobody having bound it?* A builder-authored image check must not, and the
code says so — "Builder checks remain useful evidence, but do not silently become an
independent acceptance oracle by marking their own homework." That rule is correct and is
AGENTS.md's HIR-0060: valid builder payments are bound checks, never autonomous
acceptance authority.

Three consumers that close over a unit's **required claim bindings** read that same field
to answer a different question: *was the exact id this claim names produced, and did it
pass?* Boundness there is established by the claim itself, so autonomy is not the property
in question. Because materialization mints required `image_contract` ids as build-time
debts that only a builder payment can discharge (HIR-0047, HIR-0048, HIR-0053), the flag
those consumers read can never be true on that path. Three rules that are each right
individually are jointly unsatisfiable: no unit with a required image binding could
publish an outcome, in any shot.

The earliest owning decision is not the predicate at `checks.py:641` and not the guard in
the receipt. It is expressing "may veto unbound" and "is a trustworthy reading of a bound
id" in one field, so a consumer asking the second question silently receives the answer to
the first. This is the same shape as HIR-0198: a condition no rule states, added where the
two meanings happened to coincide, and validated only in the region where they do.

The live builder verdict does not read the flag at all — it closes over id presence and
`pass`. So the live path and the durable path disagreed about what satisfies a binding,
which is why the failure landed at finalize, after the whole build and critic round was
paid for.

## Decision criteria

- Two questions get two names. A consumer states which one it means.
- The rule lives in one leaf module both the evidence and orchestration layers call; no
  consumer restates it.
- The anti-self-certification property is preserved exactly where it applies: a row
  nothing bound still needs autonomy to block.
- What satisfies a binding live must satisfy it durably. Disagreement between those two
  consumers is itself a defect, and is asserted as an invariant.
- A refusal names which required ids were produced-but-failing and which were absent.

## General mechanism

1. `domain/evidence_authority.py` (dependency-free leaf) holds the one source-to-family
   map and five named predicates: `evidence_family`, `is_typed_measurement`,
   `settles_bound_requirement` (typed and passing), `blocks_without_a_binding` (the
   autonomy flag), and `is_recorded_evidence` (typed **or** autonomous). Autonomy stays
   sufficient everywhere it was sufficient before and is nowhere necessary for a bound id,
   so no row that decided anything before stops deciding it — including a row that carries
   the flag but no `source`.
2. `orchestration/unit_evaluation_receipts._passing_evidence` observes a required binding
   through `settles_bound_requirement` and drops its private copy of the family map. Its
   refusal now names the unit, separates "produced by the canonical evaluator but not
   passing" from "never produced", and states the legal next action.
3. `evidence/claim_evidence.reconcile_observation` decides a cited id inside the claim's
   binding closure with `is_recorded_evidence` and one outside it with
   `blocks_without_a_binding`. A critic can no longer override a passing bound image
   measurement, and a failing one now supports the critic instead of reading as a
   contract gap.
4. `orchestration/revalidation._authoritative_projection` seals every recorded reading,
   so a look unit's record contains the image evidence it was sealed on. Existing sealed
   outcomes verify against their own stored bytes and digest, so nothing migrates.

Scene evidence is unaffected: every scene row already carries both `source:
interface_contract` and `authoritative: true`, so the two predicates coincide there. The
behavioural delta is exactly the image rows, exactly at the defect.

## Rejected patch-level alternatives

- Minting builder image rows `authoritative: true`: deletes the anti-self-certification
  rule to fix a consumer that was asking the wrong question.
- Requiring materialization to author a planner-side adversary for every minted image id:
  moves real work upstream to satisfy a flag, and the harness already selects the
  adversary at payment time (HIR-0053).
- Making required image claims advisory: contradicts HIR-0046, which forbids a look-owning
  unit sealing on scene counts.
- Special-casing `source == "image_contract"` inside `_passing_evidence`: leaves the two
  meanings fused and lets the next consumer make the same mistake.

## Validation

- `src/tests/unit/test_bound_image_evidence_settles_its_claim.py`, built on the
  `hero_facade` shape — a look-owning unit with a required image claim and no
  planner-authored image check: the passing builder row settles its binding; a failing one
  refuses and the message says it was produced; an absent one refuses and says it was
  never produced; the live verdict and the receipt agree on the same unit and rows; a
  critic is contradicted by the passing bound row and supported by the failing one; an
  unbound builder row still has no autonomous authority; the sealed record keeps the image
  row. An autonomous row carrying no `source` still decides its bound claim, which the
  pre-existing critic contract test caught the moment the predicate was too narrow. Six of
  the nine fail without the mechanism; the three that pass either way are the guards that
  the preserved properties stayed preserved.
- Every non-look unit in the reporting runs sealed with authoritative scene rows only, so
  a fixture built on a geometry or control unit passes with and without the fix. That is
  why the shape of this fixture is part of the mechanism.

## Release and rollback

No schema, capsule, or digest change: unit and layer capsule content is untouched, so
`DIGEST_SCHEMA` does not move and durable units sealed before this change stay valid and
resumable. Rollback restores the flag read and the unpublishable required image claim.

## Remaining limitations

The builder's live read-back (`blender/tools/reports.py`, `blender/tools/mutate.py`) still
filters on the autonomy flag for its scene-interface summary. That view is deliberately
scene-scoped and image debt is tracked separately (HIR-0048), so it is left alone; it does
mean a builder's contract summary never lists its image rows. The sealed revalidation
record keeps the field name `authoritative` while now holding every typed measurement;
renaming a durable field is a migration this change does not take. `UnitEvaluationConflict`
still escapes the builder boundary as an unhandled exception rather than a typed stop, so a
future conflict of this family will again die as an unclassified boundary instead of a
dispatchable envelope; that is a separate defect and is not fixed here.
