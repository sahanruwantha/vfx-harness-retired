---
id: HIR-0180
title: Stage calls refuse collectable findings addressed inside their own write
status: accepted
introduced_in: unreleased
date: 2026-09-03
failure_class: materialization_finalize_teaching_rounds
mechanism: stage_call_runs_terminal_validator_on_the_proposed_candidate
adr: null
---

# Stage calls refuse collectable findings addressed inside their own write

## Observed failure

Run `20260903T053305Z-83f8e1` on `artifacts/room_1046_opening` (main `0c2c593`): the
layer-2 materialization staged five units, 25 scene contracts, and 21 requirement bindings
through `stage_materialization_unit` without a refusal, then `finalize_materialization`
returned seven findings, most of them decidable per unit at the moment it was staged:
look-owning units whose judge frames had no required image-domain claim (HIR-0046), a
claim binding a contract whose roles lie outside the unit's mutation authority
(HIR-0159), a requirement binding paying an image witness against a `scene` domain,
judgment-debt subjects with no reachable provider, and judge frames without
`visible_fraction` rows. The session answered by unstaging all five units and restaging
from scratch, then patching; the layer attested after 21 minutes and roughly 40 turns of
a 66-turn budget. Earlier sessions of the same lineage paid the same class of round for
the derivative contradiction (HIR-0175) and the deferred subject bbox rows (HIR-0177
mechanisms 5 and 6), each ported to the stage call one rule at a time.

## Root cause

Validation gap at the staging transaction. `patch_materialization` has always run the
collectable validator on the proposed candidate and returned the remaining findings, but
`stage_materialization_unit` ran only its local predicates. Every rule finalize applies to
a unit, its contracts, and its bindings was therefore learned one finalize round late,
and each port of a single rule to the stage call (HIR-0177) left the rest behind.

## Decision criteria

- Rejections teach at the earliest owning boundary; a write that cannot survive finalize
  must not enter scratch.
- One validator: the stage call must not grow a second rule set that drifts from
  `validate_materialization`.
- Incremental staging stays legal: findings that need units not yet staged are reported,
  not refused.

## General mechanism

1. `MaterializationInspection` carries the selected-authority inputs the terminal
   validator needs (global root, bundle hash, base layers/scene-checks/requirements,
   resolutions). The staging transaction runs `inspect_materialization` on the candidate
   before and after the mutation, inside the serialized write.
2. `staged_write_findings(before, after, …)` refuses every finding the write introduced
   that is addressed under the staged unit (`/layer/stages/<index>`), under a supplied
   contract or binding row, or that names a supplied contract or requirement id as a whole
   token; every other finding is layer-level and is returned as
   `StagedMaterializationUnit.remaining_findings`.
3. The `stage_materialization_unit` tool passes the inspection from the same authority it
   resolves for patches and lists the open layer-level findings under the STAGED result,
   so the session sees what finalize will still refuse while it plans the next unit.
4. `LOOK_REQUIRES_IMAGE_DOMAIN_RULE` no longer names the retired `human_decision`
   evidence kind.

## Rejected patch-level alternatives

- Porting the remaining rules to the stage call one by one (a second, drifting validator).
- Teaching the materializer to call `materialization_status` or finalize early (prompt
  wording for a missing boundary).
- Refusing every new finding at the stage call (would forbid legal incremental staging).

## Validation

- `src/tests/unit/test_staged_write_findings.py`: classification refuses only findings
  addressed inside the write or naming a supplied id as a whole token; a look-owning unit
  without an image claim is refused by the stage call with the finalize rule and no
  candidate write, and the clean unit stages with only layer-level findings remaining.

## Release and rollback

Unreleased; the stage tool's result text gains an open-findings list. Rolling back
restores finalize-time discovery.

## Remaining limitations

- Layer-level findings (judge-frame visibility, judgment-debt activation over the whole
  layer) are reported at each stage call but can only be refused at finalize.
- A finding at a bare list pointer is attributed by the ids it names; a validator note
  that omits the id is treated as layer-level.
