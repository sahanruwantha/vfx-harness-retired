---
id: HIR-0029
title: Extra-frame contracts bind through ids, not invented judges
status: accepted
introduced_in: unreleased
date: 2026-08-26
failure_class: unit_authority_was_not_compiled_for_the_builder
mechanism: compiled_frame_authority_and_teaching_judge_set_rejections
adr: ADR-0006
---

# Extra-frame contracts bind through ids, not invented judges

## Observed failure

Shot `vfx-test`, `l1-remat6`, run `20260826T155803Z-b5aa3f`. After HIR-0028 the
session did not copy A2. The scratch candidate authored a crossing spine,
real path-clearance bounds, and interior `visible_fraction` rows at f72/f150
owned by layer 1. Then it burned the remaining turns on two guesses:

1. `Read plans/outcomes` (EISDIR), then `index.json` / `2.json`. Layer 1 is a
   dependency root; leftover `01.json` / `02.json` are not this design's
   authority.
2. Extra-frame vis on `composition_context.frames` and claim `moments`. The
   validator passed twice, `gate_preview` returned blocking findings, and the
   session walked f72/f150 onto the judge lists until `max_turns_exhausted`.
   Layer judge is structural `{1, 240}` and cannot change in materialization.
   Unit judge, claim moments, and `composition_context.frames` are subsets of
   that list. Scene contracts may still measure other frames; bind those ids
   through `composition_context.contract_ids` while leaving `frames` on the
   unit judge.

HIR-0027 held: exhaustion did not publish. The write-hook said `outside the
judge set: [72, 150]` and named no legal next action.

## Root cause

Frame-subset rules lived in `EvaluationPolicy.parse` and `ledger.py`. Kickoff
said `Sealed upstream outcomes (readable): plans/outcomes/` — a directory, not
an enumerated card. The session had to rediscover that extra-frame contracts
bind by id, and that a dependency root has no sealed outcomes to read.
Classification: missing compiled context. Prompt text in a remat trigger is
not a card (HIR-0025's class).

## Decision criteria

- Materialization kickoff compiles this layer's judge frames from the global
  row. Unit judge, claim moments, and `composition_context.frames` stay inside
  that list.
- Extra-frame scene contracts bind through `composition_context.contract_ids`
  (or a required claim whose moments stay in the unit judge). Adding those
  frames to layer/unit judge, `composition_context.frames`, or claim moments
  is refused.
- Judge-set rejections name that legal binding.
- A dependency root compiles `Sealed upstream outcomes: none` and forbids a
  directory Read. A dependent layer lists named `plans/outcomes/{id}.json`
  files for `depends_on_layers` only.
- Two-sided contracts (`path_clearance_min` primary `roles`) bind on the unit
  that mutates those roles; `compare_roles` and observation-only
  `visible_fraction` may name other plan-declared namespaces.

## General mechanism

- `compile_frame_authority` / `EXTRA_FRAME_BINDING_RULE` in
  `domain/work_units.py`. Kickoff, parse, ledger, and claim-closure share it.
- Claim-closure counts `composition_context.contract_ids` as bound producers,
  the same way `validate_materialization` already did. Binding those ids as
  claim `evidence` whose contract frame sits outside claim moments still
  fails closed and names the id path.
- `_sealed_outcomes_block` enumerates named files from `depends_on_layers`
  and `required_outcomes`.
- `_TWO_SIDED_CONTRACT_BINDING` compiles the gate's role-selector-closure
  distinction into the same kickoff.

## Rejected patch-level alternatives

- Tell remat7 in the operator trigger not to put 72/150 on
  `composition_context.frames`: remat6 already had that requirement in prose
  and still invented judge frames because the kickoff invited a directory
  Read and the rejection did not name the id-binding.
- Add f72/f150 to the layer judge list: structural; the validator refuses.
- Loosen max-turns so the session can walk the loop: HIR-0027.
- Enable Glob so `plans/outcomes/` lists: leftover discarded files are not
  authority.

## Validation

- `tests/unit/test_work_units.py`:
  `test_compile_frame_authority_preserves_declared_judge_order`,
  `test_composition_context_frames_outside_judge_name_extra_frame_binding`.
- `tests/unit/test_plan_records.py`:
  `test_claim_moments_outside_judge_name_extra_frame_binding`,
  `test_unit_judge_outside_layer_names_extra_frame_binding`.
- `tests/unit/test_planner_outcomes.py`:
  `test_materialization_kickoff_carries_row_and_readable_paths` (dependency
  root; no directory Read),
  `test_materialization_kickoff_compiles_frame_authority_and_named_outcomes`
  (dependent layer lists only the named upstream file).
- `tests/architecture/test_staged_architecture.py`:
  `test_claim_closure_counts_composition_context_ids_as_bound`,
  `test_extra_frame_evidence_binding_names_id_path`.

Do not judge this HIR by treating remat6 as success. Remat7 (`1ae2d7`) is the
first path that received the compiled card. It bound extra-frame vis through
`composition_context.contract_ids`, published view `eb02080d…` (crossing spine
f24 y=140 / f40 y=172, interior vis at f72/f150 owned by layer 1), then the
unit plan retracted because claim-closure ignored that binding. Claim-closure
now counts the same ids the materialization validator already counted as
producers. Retry `1a1451` published `plans/units/cam_spine.md` through a clean
gate.

## Release and rollback

No schema migration. Rollback is restoring the directory invitation and the
short judge-set errors. That would make extra-frame vis another translation
job.
