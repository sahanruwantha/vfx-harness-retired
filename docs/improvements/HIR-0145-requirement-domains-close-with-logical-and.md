---
id: HIR-0145
title: Requirement domains close with logical AND
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: materialization_launders_multi_domain_requirement
mechanism: durable_requirement_domain_binding_map
adr: ADR-0006
---

# Requirement domains close with logical AND

## Observed failure

The selected global Room 1046 register declared R51 over `image + scene`: the opening
must read as the specific historic hotel rather than a generic tower. Layer 2
materialization replaced that deferred row with `kind: contract` and six count/bbox ids.
The selected materialized register retained the ids but dropped the declared domains.
All witnesses were structural or projected; no row preserved the unpaid image judgment.

HIR-0124 had already required the owner layer to declare every domain on the deferred row.
That proved the owner was capable in principle, but completion still treated any non-empty
binding list as closure. Domain coverage stopped at ownership instead of following the
requirement into its concrete evidence.

## Root cause

`requirement_bindings` was an OR-shaped transport: a row carried contract ids or a decision.
The validator checked existence and exact ids, not whether those ids jointly certified every
domain declared by global authority. Publication then replaced the deferred resolution with a
bare concrete kind, losing the evidence-domain obligation and making the laundering invisible
to later readers.

## Decision

- Deferred requirement `evidence_domains` are logical AND obligations.
- Structural domains (`scene`, `temporal`, `projected_composition`) require at least one bound
  contract whose canonical registry domain exactly matches.
- Candidate-dependent qualitative domains (`image`, `human`) require a same-domain contract
  debt or an explicit `approved_start`/`planner_start` provisional decision. The decision is a
  build-time debt marker, not a confirmed outcome.
- A mixed-domain requirement may carry contracts and a provisional decision in the same
  materialization binding row.
- Materialization cannot invent `confirmed_outcome`, use a decision for structural evidence,
  or add a padding decision after contracts already pay every domain.
- The selected concrete resolution retains `evidence_domains` and a typed
  `domain_bindings[]` map. Publication auditability does not depend on transcript or prior-view
  proximity.
- The terminal gate re-derives contract domains from `KIND_DOMAINS`; relabelling an id in the
  stored map fails closed.

## General mechanism

Materialization compiles each owned requirement against its exact base deferred row. Contract
ids are grouped by registry domain. Missing qualitative domains are assigned to one explicit
provisional decision; any other missing domain is a blocking finding that names required
domains and every bound `id=domain` witness.

The selected resolution preserves the resulting map. The requirement parser validates exact
domain coverage, unique domain rows, contract-id subsets, and one coherent provisional
statement/strength. Composed replay reads provisional rows directly, so rematerialization does
not need a prior falsification to remember that image judgment is still due.

## Rejected alternatives

- Keep domain obligations only in the global bundle: later consumers would have to reconstruct
  lineage and could not audit selected authority locally.
- Treat layer-level domain coverage as requirement closure: capability is not payment.
- Let `approved_start` close scene or projection: those domains have executable instruments.
- Infer domains from requirement prose: global planning already publishes the closed vocabulary;
  mechanical enforcement follows ids and registry kinds.
- Convert every qualitative requirement to a candidate-sensitive image contract during
  materialization: no candidate exists yet, and look-less form units use qualified Workbench
  composition judgment.

## Validation

Regression tests prove `image + scene` with only a scene count fails naming the unpaid image
domain, the same row succeeds with an explicit approved start, and publication preserves both
domain bindings. A tampered selected map that labels an image metric as projected composition
is rejected from the canonical registry. Existing materialization, rematerialization, and
provisional-composition suites remain green.

## Release and rollback

The concrete requirement resolution gains optional `evidence_domains` and
`domain_bindings`; globally authored concrete rows without this materialization lineage remain
readable. New materialization always writes the map. Rollback permits a concrete binding list
to discard global AND obligations and is unsafe.
