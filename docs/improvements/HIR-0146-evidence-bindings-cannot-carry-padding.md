---
id: HIR-0146
title: Evidence bindings cannot carry padding
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: compatible_witness_hides_incompatible_padding
mechanism: exact_binding_domain_certifiability
adr: ADR-0006
---

# Evidence bindings cannot carry padding

## Observed failure

Layer 2 of Room 1046 bound hotel-identity requirements to permissive mesh counts and
bbox bands. HIR-0145 made the missing image conjunct visible, but two more OR-shaped
surfaces remained: a required claim passed when any one of several bindings matched its
declared domain, and a requirement silently ignored contract ids whose domains it did
not declare. A valid witness could therefore carry unrelated counts through publication.

The permissive count ranges are not themselves invalid. `mesh_vertex_count >= 1` is an
honest existence witness. The defect is allowing that witness to certify, or be retained
as if it certified, a different typed proposition.

## Root cause

Claim validation asked whether the set of bound domains contained the declared domain.
Requirement compilation grouped only ids matching declared domains and discarded the
rest from the durable map. Both checks proved that a compatible witness existed while
failing to account for every witness the author claimed as payment.

## Decision

- Every executable binding on a required structural claim must have the claim's exact
  canonical `KIND_DOMAINS` domain.
- Cross-domain observations belong in `composition_context`, or in another atomic claim
  with its own domain; they are not certification padding.
- Every contract id in an owned requirement binding must belong to one of that
  requirement's declared AND domains.
- Every concrete resolution id must be assigned to exactly one retained contract-domain
  row. An id cannot survive publication outside the auditable domain map.
- Rejections name each `id=canonical_domain`, the asserted or declared domains, and the
  legal split/remove action.

## General mechanism

Materialization resolves every required structural claim binding through the canonical
metric registry and rejects any incompatible member, even when another member is valid.
Requirement compilation performs the same total-accounting check before grouping ids.
The durable requirements parser verifies that the union of contract-domain binding ids
equals the resolution's id set, preventing later or hand-authored views from restoring
unassigned padding.

## Rejected alternatives

- Ban wide vertex-count ranges: those are valid existence contracts and width alone
  cannot reveal the proposition being certified.
- Infer hotel identity from contract or requirement prose: certifiability is typed, not
  keyword-scanned.
- Keep incompatible ids but ignore them during closure: that recreates an unauditable
  second class of evidence.
- Let one correct metric bless the whole claim: atomic claim bindings are conjunctive
  assertions of payment, not a bag of optional hints.

## Validation

Regression tests cover a temporal claim containing one valid temporal metric plus a
scene count, a requirement containing an out-of-domain scene count alongside valid image
debt, and a selected resolution with unassigned ids. Existing proxy-evidence and plan
record suites remain green.

## Release and rollback

No schema version changes. Existing materializations that used cross-domain claim
evidence must split the claim or move observational ids to `composition_context`.
Rollback would again allow evidence that pays no declared proposition and is unsafe.

