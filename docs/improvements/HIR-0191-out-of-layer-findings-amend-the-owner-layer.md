---
id: HIR-0191
title: A finding whose fault owners are sealed upstream amends the owner's layer view
status: accepted
introduced_in: unreleased
date: 2026-09-04
failure_class: downstream_finding_stopped_the_run_because_its_amendment_targeted_the_wrong_layer
mechanism: owner_layer_amendment_target_with_controller_dispatch_and_evidence_batching
adr: ADR-0010
---

# A finding whose fault owners are sealed upstream amends the owner's layer view

## Observed failure

Room run `20260903T234629Z-21c1b4` (layer 2 of `artifacts/room_1046_opening`, on main
`33310de`): `building_shell` swept mass position, height, and footprint under the sealed
camera path and proved that no rigid mass satisfies the frame-1 cap and frame-113 floor
together. It abstained with `cannot_express_in_scope` naming `camera_rig`, layer 1's sealed
camera unit, as the fault owner (finding `hf-7488c9e81ae1914b88fd`, `fault_owner_units:
["camera_rig"]`). The unit planner reached the same conclusion on its own: the camera is
the fault owner and layer 2 cannot reopen a sealed layer. The framing check in the same run
read 12% of the hotel corner on screen at f150 for a mass that had passed every other row.

The stop the builder compiled named `publish_validated_amendment` on **layer 2's** view. The
controller (HIR-0186) refused it as `out_of_layer_owner`, correctly: rematerializing layer 2
cannot change the camera, and a replacement of layer 2 alone re-authors the same finding.
The run therefore ended for an operator, who would have typed `vfx plan --layer 1
--rematerialize --evidence <finding>` by hand: the same mechanical step ADR-0010 removed
for same-layer findings, and one whose evidence the rematerialization never read (the
`--evidence` locators were recorded for audit only; the layer-1 materializer saw the
operator's trigger sentence and nothing of what layer 2 had measured).

## Root cause

Two decisions, both earlier than the refusal that surfaced them.

1. **The stop compiler bound the amendment to the stopped layer, not to the layer whose
   authority must change.** `compile_hypothesis_falsification_stop` set
   `target.layer_id = finding.layer` unconditionally. But `record_hypothesis_falsification`
   already separates ownership by construction: same-layer seeds stay in `affected`, and
   `fault_owner_units` are exactly the units *outside* the stopped layer (an earlier-layer
   camera provider, HIR-0127). A non-empty `fault_owner_units` therefore always means "the
   authority that must change is another layer's", and a target on the stopped layer was a
   proposal the controller could only refuse. The typed proposal was less precise than the
   record it was built from.
2. **Rematerialization evidence was audit-only.** `vfx plan --rematerialize` required
   `--evidence` but `_rematerialize_layer` discarded it, so the replacement design could not
   see the measured floor it had to answer. HIR-0175 §5 already recorded a rematerialization
   re-authoring the same misunderstanding; without the evidence in the kickoff, the owner
   layer's replacement would do the same and the controller would converge on the repeated
   fingerprint after paying for it.

The question that motivated the change ("mark the upstream fault as to-do, finish layer 2,
then ask layer 2 whether the fix is okay") is answered by the existing preservation
mechanism, not by a new one: rematerializing layer 1 runs the authority-state transaction,
which preserves every layer-2 unit whose predecessor edges and source closure are unchanged
and supersedes the rest (HIR-0102, HIR-0171); canonical replay then re-evaluates what
survives. Deferring the owner's fix would spend on layer-2 work that transition supersedes.

## Decision criteria

- A typed proposal names the authority that must actually change; the controller dispatches
  proposals, it does not reinterpret them.
- The stopped layer's identity stays in the record (unit fields, cause owner scope, the
  evidence document's `layer_id`); only the amendment target moves to the owner.
- Owner resolution fails closed: owners must resolve to exactly one earlier layer, and an
  unknown or ambiguous unit id is a compile error, never a guess.
- One replacement of the owner's view answers every open finding that names it.
- An owner outside the run's requested layer range is refused naming the layer, exactly as
  the gate boundary does (HIR-0190); a builder finding is not stronger authority than the
  operator's `--from`.

## General mechanism

1. `agents/builder/stops._amendment_layer(finding, layers)` returns the stopped layer when
   the finding names no fault owners, otherwise the single earlier layer that contains every
   fault owner; several layers, an unknown id, an id present in several layers, a same-layer
   owner, or a non-upstream owner raise. `compile_hypothesis_falsification_stop` binds the
   target, postcondition, and stop identity to that layer, records both layers in the
   evidence document (`authority.layer_id` stopped, `authority.amendment_layer_id` owner),
   adds `fault-layer:<owner>` to the cause's owner scope, and names the owner in
   `next_action`.
2. `application/run_controller.RunController._ownership_refusal` accepts an owner-layer
   target only when the finding names fault owners, every owner is a unit of that layer, and
   the layer is upstream of the stopped one; a same-layer target with owners, or an owner
   target whose units do not contain the owners, is refused `out_of_layer_owner`. The
   controller now carries the run's `layer_range`; an owner outside it is refused
   `out_of_range_owner` with the layer to include.
3. `_owner_layer_evidence` batches every hypothesis-falsification record on the same bundle
   whose fault owners are units of the owner layer, so the dispatched rematerialization
   cites all of them (`--evidence` per locator, the stop's own finding first).
4. `agents/planner/rematerialization_evidence.replacement_evidence_block` renders each cited
   finding (record, stopped unit, contracts, classified observations with bounded reasons,
   required authority, fault owners, affected units) into the kickoff's replacement reason;
   other locators are named by path; the block is bounded and locators cannot escape the
   shot root. `_rematerialize_layer` appends it to the operator's trigger.
5. After the dispatch the driver re-derives the receipt-backed prefix as before: the owner
   layer is unpassed, so it rebuilds, and layer 2's units are preserved or superseded by the
   authority-state transaction rather than asked.

## Rejected patch-level alternatives

- Letting the controller retarget a stopped-layer proposal to the owner: dispatching a
  transaction the envelope did not name is the inference HIR-0164 forbids.
- Deferring the owner's amendment until the stopped layer closes and then "asking" layer 2:
  spends on work the transition supersedes, and replaces preservation evidence with a model
  opinion.
- Passing the finding's `reason` through the trigger string only: unbounded, unstructured,
  and silent about the other findings naming the same owner.
- Rematerializing the stopped layer anyway: re-authors the same finding; the controller's
  convergence would stop it, after paying for it.

## Validation

- `src/tests/unit/test_builder_stop_boundary.py`: an out-of-layer fault owner moves the
  target, postcondition, and identity to the owner's layer while the stopped unit stays in
  the identity and cause; owners spanning layers, unknown owners, ambiguous ids, and
  non-upstream owners fail closed.
- `src/tests/unit/test_run_controller.py`: the owner-layer stop dispatches `--layer 1` with
  every open finding naming the owner as evidence; an owner outside the run's range is refused
  naming the layer and dispatches once the range includes it; an owner target whose units do
  not contain the owners is refused.
- `src/tests/unit/test_rematerialization_evidence.py`: findings render compactly and once,
  other locators are named, the block is bounded, and locators cannot escape the shot root.
- Existing builder-stop, controller, and driver suites pass unchanged.

## Release and rollback

Lands as ADR-0010 step 4's second half. Rollback is `--single-pass`; the stop compiler's
owner target is a pure function of the finding and the selected DAG and produces the same
envelopes as before for findings without fault owners.

## Remaining limitations

Owners in more than one earlier layer still stop the run for an operator: one amendment
replaces one layer view, and the order in which two owners should change is a plan decision.
The materialization gate's own findings (HIR-0187) reach the owner through the gate
boundary, not through this path. The rematerialization reads the cited findings but does
not yet receive the stopped layer's accepted geometry as a measured constraint; the camera
replacement designs against the finding's observations and the deferred framing rows, and
canonical replay decides whether layer 2 survives it.
