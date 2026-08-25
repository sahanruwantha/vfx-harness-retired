---
id: ADR-0007
title: Dressing authority — appearance assignment across layer ownership
status: proposed
date: 2026-08-25
supersedes: null
---

# Dressing authority — appearance assignment across layer ownership

## Context

Run `20260825T133513Z-af3084`: all four lookdev units sealed at 5.0 canonical under
measured exposure anchors, and the composed critic scored the layer 1.0–1.83 across six
axes. The judged frames are dominated by layer 1's blockout masses, and the authority
model gave no unit a legal way to put materials on them: roles are reserved by their
owning layer, mutation scope is ownership-bound, contract selectors and claim subjects
close through `mutates.roles`. The gunmetal/cyan/amber language existed in the scene
graph — on layer 2's own small staged subjects — while every visible surface stayed a
naked proxy.

Lookdev's ontology IS assigning appearance to geometry it does not own. The model could
not express the one mutation class the department exists to perform, so "all contracts
pass" and "the frame reads" were permanently separable.

## Decision

Appearance assignment becomes a typed, owner-granted authority:

- A layer's materialized row may declare `dressable`: role selectors whose objects
  expose their material slots to later layers. Declaring nothing keeps every role
  undressable. The grant is the owner's row, never the dresser's request.
- A unit's `mutates` may declare `dresses`: selectors it assigns materials onto.
  Validation refuses any selector no OTHER layer declares dressable (exact-string
  match — the owner names the surface, the dresser names the same surface), refuses
  overlap with the unit's own `roles` (owned roles need no dressing), and requires a
  required claim covering every dressed selector exactly as for mutation roles.
- Contract selectors and claim subjects close through `roles ∪ dresses`, so
  assignment-fraction contracts and appearance claims about dressed surfaces are
  expressible and answerable.
- Enforcement of the assignment-only boundary is layered: the creation-based scope
  audit is unaffected (dressing creates no objects); geometric tampering with dressed
  objects remains caught by the OWNER's sealed contracts (bbox, visibility, schedule
  rows re-evaluated in canonical replay). Worker-level write-path enforcement
  (material-slot writes only) is a recorded follow-up, not part of this decision.
- `DIGEST_SCHEMA` 3→4 (the WorkUnit shape changed); the golden-digest test pins the
  pairing.

## Consequences

- Layer 1 declares its blockout tiers dressable via a view amendment; its sealed units
  are preserved through the replan closure (unit definitions unchanged).
- Layer 2's materialization gains a dressing unit scope, making the composed look
  producible: the frame-filling masses can carry the primary material language the
  critic judges.
- Dressing is assignment authority only. A dresser that moves, deletes, or remeshes a
  dressed object breaks the owner's contracts and fails canonically — the protection
  model is unchanged.
- Rejected alternatives: duplicating look geometry inside the lookdev layer (fights
  the real geometry layers later); re-sequencing lookdev after the beat layers
  (destroys the lookdev-first DAG the generation is built on); silently permitting
  cross-layer writes (unowned mutation, the class every closure exists to prevent).

## Validation

- Suite 294: dressing-closure blocks an ungranted selector and clears a granted one
  (both directions); claim closure covers dressed selectors; MutationScope parse
  refuses dressing-owned-roles overlap and mode-`none` dressing; golden digest pins
  schema 4.
- The real shot's layer-1 amendment and layer-2 rematerialization are this record's
  acceptance run; status stays proposed until that build's composed verdict.
