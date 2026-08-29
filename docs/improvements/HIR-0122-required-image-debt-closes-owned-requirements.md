---
id: HIR-0122
title: Required image debt closes owned requirements
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: jit_requirement_closure_rejects_compiled_image_debt
mechanism: canonical_image_debt_ids_are_requirement_contracts
adr: null
---

# Required image debt closes owned requirements

## Observed failure

Room 1046 Layer 3 materialization run `20260829T224517Z-e86a4e` followed the
HIR-0047 authoring rule: required image claims bound
`hero-window-darkness-201`, `interior-door-readability-213`, and
`hero-transition-cut-check`, while the candidate-sensitive top-level
`image_contracts` array remained empty. Finalization nevertheless reported those ids
as absent contracts, left requirements R34, R46, R54, and R55 unresolved, and the
session exhausted 24 model turns after 1,017.4 seconds and $3.0092 trying to
restructure otherwise legal image debts.

## Root cause

`validate_materialization` correctly compiled required image claims into build-time
debt for image-property and optical-signal checks. Its owned-requirement validator,
however, resolved `contract_ids` only against concrete `scene_contracts` and
`image_contracts` rows. JIT materialization forbids the latter rows until a live
candidate exists, so the validator contradicted the only legal authoring form.

This was the requirement-closure counterpart of HIR-0047: claim closure already
counted a missing image row as declared debt, but owned-requirement closure did not.

## Decision criteria

- A required claim that asserts `image` and binds an `image_contract` contributes its
  normalized id to JIT owned-requirement closure.
- The id source is the canonical builder payment compiler, not a second traversal or
  copied registry.
- `image_contracts` remains empty during materialization; no placeholder row is
  published before a candidate exists.
- Optional image claims do not compile debt and cannot close a required requirement.
- Missing scene-contract ids and unknown image ids still fail closed with the existing
  pointer-addressed finding.

## General mechanism

Materialization derives `image_debt_ids` by calling
`image_contract_debt_cards` for every parsed unit. Requirement bindings resolve against
the union of concrete contract ids and those canonical required-claim debt ids. Runtime
payment remains unchanged: each debt still carries its id, frame, property, and axis,
and candidate freeze still refuses unpaid debt.

## Rejected patch-level alternatives

- Publishing dummy `image_contracts` rows would invent candidate-sensitive evidence at
  plan time and violate HIR-0047.
- Treating every image reference as a requirement contract would let optional claims
  satisfy mandatory authored intent.
- Special-casing the three Room 1046 ids would make a held-out fixture the core schema.
- Increasing the materializer turn budget would spend more model time against an
  impossible validator predicate.

## Validation

`test_materialization_requirement_binding_accepts_required_image_debt` proves that a
required, payable, signal-backed image debt closes an owned requirement while
`materialized.image_contracts` remains empty.
`test_materialization_requirement_binding_rejects_optional_image_reference` proves an
optional reference remains absent and cannot close the requirement.

Focused plan, architecture, image-debt, and atomicity suites passed 182 tests. The full
repository suite passed 593 tests in 42.16 seconds. Ruff and `.venv/bin/vfx --help`
passed.

Producing rerun `20260829T230724Z-aeb87d` staged two units, 14 scene contracts, and all
11 owned requirement bindings without any absent-image-contract or incomplete-
requirement finding. It later exhausted 24 turns after 1,172.6 seconds and $3.5603 on a
separate interaction/atomicity authoring loop, so this record does not claim that Layer
3 published. It proves that the reproduced HIR-0122 boundary no longer rejects the
legal debt representation.

Implementation commit: `4bba476` (`fix: count image debt in requirement closure`).

## Release and rollback

No persisted schema changes. Existing legal JIT candidates gain the same image-debt
semantics already used by claim closure and builder payment. Reverting would restore an
unrepresentable owned-requirement state for every requirement payable only after live
image evidence exists.

## Remaining limitations

This mechanism does not decide whether the materializer chose a sound unit DAG, whether
the builder will pay the debt, or whether interaction claims are authored efficiently.
Those remain separate typed plan, runtime payment, and agent-instrument concerns.
