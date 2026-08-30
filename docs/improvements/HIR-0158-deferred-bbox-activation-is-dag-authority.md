---
id: HIR-0158
title: Deferred bbox activation is selected DAG authority
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: dag_occupancy_asked_as_client_question
mechanism: compiled_earliest_geometry_successor
adr: ADR-0005
---

# Deferred bbox activation is selected DAG authority

## Observed failure

Layer 1 materialization run `20260830T142640Z-10b425` asked the supervisor which
later layer is the earliest geometry owner for persistent `bbox_*` `activates_at`.
It assumed layer `2` and finalized. Build run `20260830T144410Z-758622` then exited
5 on that unanswered question. The stop taught
`python -m vfx_harness.orchestration.escalate` instead of the public `vfx` CLI.

The selected sparse DAG already named every later layer, its `depends_on` closure,
and whether it may provide geometry. Occupancy was not a brief ambiguity.

## Root cause

Materialization kickoff told the session to use "the earliest geometry layer" as a
placeholder. It did not compile successor layer ids from the selected bundle. The
model used `ask_supervisor` for a fact the harness already had. Build then treated
that recorded question as a human gate.

## Decision criteria

- Deferred bbox `activates_at` is selected DAG authority, not a client taste call.
- Kickoff compiles successors and `earliest_geometry_layer` from sparse provides and
  dependency closure. Authored order is only the topo tie-break.
- Publication refuses a deferred camera-owned bbox whose `activates_at` is not that
  compiled id.
- `ask_supervisor` remains for genuine brief/still contradictions.
- Operator stops name the public `vfx escalate` command.
- No shot id, reserved namespace, or fixed layer count belongs in core code.

## General mechanism

`compile_deferred_subject_activation` lists later layers whose dependency closure
contains the owner. The first successor that may provide geometry is
`earliest_geometry_layer`. Camera-layer materialization kickoff compiles that card.
`validate_materialization` refuses mismatched deferred `bbox_*` rows.
`ask_supervisor` names the compiled field. Unanswered-question exits teach
`vfx escalate`.

## Rejected patch-level alternatives

- Answer Q1 by hand or `--force`: that leaves the next camera layer asking again.
- Infer the form layer from brief keywords or reserved-role names: role names are not
  capabilities (HIR-0098).
- Stage shot-root clips or enlarge the materialization prompt: occupancy is already in
  `layers.json`.

## Validation

A three-layer DAG (camera, form, lookdev) compiles `earliest_geometry_layer` as the
form id. A deferred bbox aimed at a later lookdev id is a gap. Kickoff for a camera
row contains that compiled id and forbids `ask_supervisor` occupancy questions.
`DEFERRED_SUBJECT_BBOX_KINDS` matches the scene-check bbox registry.

## Release and rollback

No schema migration. Existing unanswered occupancy questions remain human-gated until
answered or the layer is rematerialized against this kickoff. Rollback would restore
placeholder `activates_at` and internal-module answer instructions.

## Remaining limitations

Non-camera later layers are geometry-capable in the sparse capability vocabulary;
"earliest" is the first such successor, not a reserved-role match to the bbox selector.
An already-published camera view that asked Q1 is unchanged until rematerialization.
