---
id: HIR-0044
title: Look without image contracts is not a critic handoff
status: accepted
introduced_in: unreleased
date: 2026-08-27
failure_class: empty_image_gate_locked_look_iteration
mechanism: look_unsettled_keeps_run_bpy_open
adr: null
---

# Look without image contracts is not a critic handoff

## Observed failure

Run `20260827T042342Z-a21ce1` `atmosphere` declares `look_capabilities:
["atmosphere"]` and binds only scene counts/links. After 3/3 executable rows the
live tools said the unit "binds authoritative image evidence" and blocked
`run_bpy` until `compare_frame`. That comparison reported **0/0** image
contracts pass, then **CRITIC HANDOFF READY**. Further look mutation (including
putting back shaft occluders) was denied. Canonical still scored f150 look 2.0.

HIR-0042 named this remaining limitation.

## Root cause

`image_evidence_required_for` is true when the unit declares look capabilities,
even with an empty image-binding set. `_pixel_contract_gate(evidence_ids=set())`
returns `(True, [])`. The compare-frame handler treated that 0/0 pass as critic
handoff. The live mutation guard keys off `scene_contracts_passed`, so existence
rows closed appearance iteration. Classification: evidence-gate / tool-policy
error. Counts still cannot certify look (HIR-0014); the defect is locking
mutation as if they had.

## Decision criteria

- `look_unsettled_for`: look feedback groups and no bound image-contract ids.
- That flag lives on the live phase / comparison state.
- Scene-pass followup does not claim bound image evidence and does not lock
  `run_bpy`.
- 0/0 image rows are not `CRITIC HANDOFF READY`.
- `builder_phase_guard` skips the `run_bpy` deny while `look_unsettled`.
- Executable-only units still lock on scene pass. Look units **with** image
  bindings still lock until those rows pass.

## General mechanism

`look_unsettled` is compiled from the unit's declared capabilities and exact
image-binding ids. Canonical sealing still uses `image_evidence_required_for`
(look units still go to the critic). Live mutation stays open until the builder
hands off or the session budget stops.

## Rejected patch-level alternatives

- Prompt "keep mutating after 3/3": the PreToolUse hook denied `run_bpy`.
- Make `image_evidence_required_for` false for look-without-bindings: that
  would also skip the compare-frame gate path and revive sealing on geometry.
- Treat 0/0 as a failing image gate: there are no image contracts to fail;
  inventing a brightness floor is the generic gate HIR already removed for
  executable-only units.

## Validation

- `tests/unit/test_look_capabilities.py`:
  `test_look_without_image_contracts_stays_unsettled`.
- `tests/unit/test_proxy_evidence.py`: look-without-bindings is required for
  sealing **and** unsettled for live mutation.
- `tests/integration/test_harness.py`: `look_unsettled` keeps `run_bpy` legal;
  followup strings do not claim image evidence or critic handoff on 0/0.

## Release and rollback

No schema migration. Rollback is 0/0 → CRITIC HANDOFF and a scene-pass lock
on look units.

## Remaining limitations

This does not add an optical contract for shafts or particle projected size.
It stops the harness from pretending those facts were already closed. HIR-0045
refuses a critic look vote on a unit judge frame that no required claim covers.
HIR-0046 refuses a look-owning unit whose only evidence is scene counts.
