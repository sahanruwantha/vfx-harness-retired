---
id: HIR-0124
title: Deferred-owner domains must cover the owner layer
status: proposed
introduced_in: unreleased
date: 2026-08-30
failure_class: undeclared_requirement_domain_forces_materializer_rediscovery
mechanism: deferred_owner_domain_and_coverage
adr: ADR-0003
---

# Deferred-owner domains must cover the owner layer

## Observed failure

Room 1046 selected bundle `d235ad49d229a5ef865f3e7c9d10b693dbafe13de759140421a6358072ec9df9`
owns R55 ("door readable by 8.5s") on Layer 3, whose `evidence_domains` are
`scene`, `projected_composition`, and `temporal` — no `image`. R41 ("light the door
so panels and handle stay readable") sits on Layer 4, which does declare `image`.
Layer 3's materializer must rediscover whether "readable" is a framing job or a
lighting job, and the plan gate cannot check either answer.

The layer half of this fact already exists: every layer row declares
`evidence_domains`, and the gate already validates presence and vocabulary. Requirement
rows have no domain field. Binding is only transitive through layer → unit → claim.

The honest cost of the status quo is a burned rematerialization session plus
falsification churn, not a Layer 3 beauty render. HIR-0110 / HIR-0046 / HIR-0047 already
refuse a required image claim on a no-image layer at the materialization gate, before
any beauty plate. The comparison is seconds at the plan gate versus a materialization
session.

The planner's split is arguably defensible — appearance of the door has a home on
R41/L4. The defect is that the decomposition is undeclared.

## Root cause

`deferred_owner` records an owner layer and a due gate, never the evidence domains the
clause will require. `Claim.asserts` already treats domain as declared identity one
level down. Layer `evidence_domains` and `CLAIM_DOMAINS` were two copies of the same
vocabulary (ADR-0003). No gate path related a requirement to an evidence domain.

## Decision criteria

- Reuse one vocabulary. `human` stays in the set.
- Coverage is AND: the owner must already declare every domain on the row. Same
  semantics as multi-role vis (HIR-0051).
- Scope to `deferred_owner` rows. `contract` / `obligation` bind concrete ids whose
  domains are transitively checkable.
- The planning ticket enumerates the closed set before generation.
- Rejection names the row, its declared domains, the owner's domains, and the layers
  whose domains could cover it.
- Do not keyword-scan briefs. Do not duplicate remat-owned gates (write-clusters, image
  debt, payable properties) at global with weaker heuristics.
- The gate catches inconsistency, not semantic wrongness: the same planner authors both
  sides and could stamp `image` onto a framing layer. That is an explicit, auditable,
  amendable decision; remat gates remain the backstop.

## General mechanism

`EVIDENCE_DOMAINS` in `work_units.py` is the single identity. `CLAIM_DOMAINS` is that
object. Mapping validation, `load_requirements`, and the plan gate consume it.

A `deferred_owner` row must declare a non-empty unique subset of that vocabulary. The
owner layer's already-validated `evidence_domains` must cover every declared domain.
`ownership_mapping_authoring_schema` enumerates the set on the planning ticket. Expansion
writes the field onto `requirements.json`.

Judge refs on a no-image layer then mean projection or timing targets, not look
authority. Executable-only units still call no critic (HIR-0032 / HIR-0039); a required
image claim on such a layer still dies at image-signal-bootstrap. Refusing the undeclared
look owner at publication rather than remat falls out of this coverage check.

## Rejected patch-level alternatives

- Hand-editing the selected bundle to move R55. Authority changes only through planning.
- Brief keyword scanning for "readable" / "light". Domain is declared, never inferred.
- Duplicating write-cluster, image-debt, or payable-property gates at global.
- A separate HIR for judge-ref meaning on no-image layers. That corollary falls out of
  AND coverage once domains are typed.
- Parameterizing `GLOBAL_SCENE_CAPABILITIES` so image-owning layers need a light
  provider in closure. Right follow-on (witnessed capability), not this change.
- Authoring concrete `dressable` selectors at global time. `Layer.dressable` already
  exists; the hole is kickoff information flow (HIR-0054). Recorded below, not built.

## Validation

`test_image_domain_on_non_image_owner_names_covering_layer` fails without coverage and
passes when the image row moves to the image-owning layer, naming that layer in the
rejection. `test_and_coverage_forces_split_when_no_layer_declares_every_domain` refuses a
projection+temporal+image row on a no-image owner with covering `none`, and passes after
the row keeps only alignment domains. `test_plan_gate_teaches_requirement_domain_coverage_on_selected_authority`
pins the gate card on already-expanded authority. Schema and missing-field tests enumerate
the closed set, including `human`. `CLAIM_DOMAINS is EVIDENCE_DOMAINS` pins identity.

- Focused plan-authoring, plan-records, plan-improvements, ownership-adapter,
  lifecycle, and WorkUnit suites: `149 passed in 7.99s`.
- Full suite: `604 passed in 43.27s`.
- `.venv/bin/python -m ruff check src tests`: clean.
- `.venv/bin/vfx --help`: exit 0.

## Release and rollback

Strict migration: selected bundles whose `deferred_owner` rows omit `evidence_domains`
fail `load_requirements` and the plan gate. Landing this change forces a global
republication to author the field. Matching unit digests preserve through `apply_replan`
(HIR-0040 / HIR-0102). Rollback restores undeclared ownership and remat rediscovery.

## Remaining limitations

Both sides are authored by the same planner, so the gate catches inconsistency, not
wrongness. Follow-on (2): parameterize the camera closure check so an image-domain layer
needs a typed light-capable provider — witnessed capability rather than self-declaration.
Follow-on (3): compile successor reservations into the geometry owner's materialization
kickoff so ADR-0007 `dressable` grants are authored at unit level by an informed owner;
do not invent global selectors before the object inventory exists. Decide (3) when a
geometry remat reproduces the information-flow hole.
