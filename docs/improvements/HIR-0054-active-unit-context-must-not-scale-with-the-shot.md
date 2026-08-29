---
id: HIR-0054
title: Active-unit context and journals must not scale with the shot
status: proposed
introduced_in: unreleased
date: 2026-08-27
failure_class: unbounded_active_unit_context
mechanism: compiled_context_read_boundary_and_unit_journal_slice
adr: null
---

# Active-unit context and journals must not scale with the shot

## Observed failure

In run `20260827T155157Z-95b80e`, fresh builder sessions read the full brief and a roughly
16K-character scene-contract catalog before querying their already-compiled unit scope. The JIT
unit planner read the full roughly 66KB layer catalog and could read a known legacy layer script
under `build/` even after broad discovery was denied. Finalizer journals grew from 13KB to 62KB
because reset and dependency replay were included with active-unit mutations.

Layer 2 rematerialization runs `20260827T185220Z-e68a73` and
`20260827T185916Z-9ff1bc` then reproduced a stronger version: each read the complete roughly
40KB requirements register, 19KB upstream outcome, 11KB layer catalog, and 6KB global plan,
called `evidence_vocabulary`, and emitted no further event for 90 seconds (the first remained
silent beyond five minutes). Both safe transactions were interrupted without publication.

## Root cause

The compiled unit card was advice, not the read boundary. Planner read exceptions admitted whole
directories, so exact known paths bypassed discovery policy. Blender's journal had an end index but
no start index, making inherited setup indistinguishable from the active delta.

## Decision criteria

Context, cost, and uncertainty must scale with the active unit; dependencies remain readable only
through declared exact interfaces; resume fidelity and selected-checkpoint truncation must remain
unchanged.

## General mechanism

Declared live work units are denied direct reads of the full brief and raw plan catalogs; their
compiled kickoff card and `unit_scope` tool are the active context. JIT planner reads are exact
selected authority members plus the selected unit and transitive predecessor plans/scripts, never
the entire `build/` or JIT view roots. After view `2ae8017f…` published, that still admitted the
whole selected 65KB `layers.json`, full brief, global plan, scene-contract catalog, and critic axes
to plan one unit. The unit-plan phase now also has no raw `Read` surface: it receives the exact
active unit scope card plus compact passed-predecessor interfaces, bounded
prior-outcome/amendment/gap feedback, layer judges/axes, and reference names. A predecessor
interface carries only exported semantic roles, dressed surfaces, controls, provided/look
capabilities, and sealed claim/contract ids; it excludes the predecessor's evaluator fields,
propositions, judge configuration, debt cards, and helpers. This prevents each later unit from
paying again for every transitive dependency's implementation closure. Journal export accepts a start index captured
after reset/prior replay and an end index from the selected checkpoint.

The compact builder kickoff renders every exact evaluator field for the active unit's bound scene
contracts. Boundedness comes from selecting only that unit's ids, not from removing graph, socket,
node-role, data-path, sample, or threshold fields and forcing a failed implementation before the
model queries `unit_scope`.

Layer materialization now receives its exact global layer row, only its owned requirement rows,
active structured decisions, compact dependency status plus explicitly required evidence, and
upstream semantic interfaces/dressable grants in the kickoff. `Read` is absent from the
materialization tool surface; the full brief, global/register catalogs, decision ledger, outcome
reports, historical views, and prior materializations are not model context. Validation still
resolves the complete authoritative documents outside the model boundary.

## Rejected patch-level alternatives

Asking the model to call `unit_scope` first leaves the larger files callable. Summarizing transcripts
does not stop sibling authority leakage. Pruning inherited calls in the finalizer asks a model to
rediscover a boundary the harness already knows exactly.

## Validation

`test_journal_start_excludes_inherited_dependency_replay` pins the journal slice. Read-boundary and
producing-run evidence are required before acceptance.
`test_materialization_kickoff_compiles_layer_bounded_authority` proves an unrelated requirement and
large outcome payload cannot enter the card while the owned requirement, exact required outcome,
and owner-granted dressable interface remain present.
`test_unit_plan_kickoff_uses_compiled_cards_not_raw_catalogs` proves exact active contracts and
predecessor interface ids remain while raw layer and scene-contract catalog instructions are absent.
`test_predecessor_interface_excludes_dependency_implementation_closure` proves consumers retain
exported selectors and sealed ids while dependency propositions, evaluator internals, judge refs,
image-debt details, and helpers do not enter their prompt.

## Release and rollback

No data migration. Revert the guard and journal start together if a declared unit is shown to need
an authority field absent from its compiled card; that is also a compiler defect to fix.

## Remaining limitations

The active unit card still contains every exact evaluator field it owns and can therefore be
large when one unit itself owns many contracts; that is unit complexity rather than transitive
shot growth. The embedded unit plan excerpt can still be large up to its existing 160-line cap. The generic
materialization system prompt/example and complete evidence vocabulary remain fixed-size context;
their size should be measured separately from shot-scaling authority. Typed successor
publish interfaces (HIR-0084) now compile digest-bound role/control/contract-id exports
into the predecessor card; producer scripts remain outside that card.
