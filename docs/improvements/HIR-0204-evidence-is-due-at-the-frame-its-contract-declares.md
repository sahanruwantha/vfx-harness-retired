---
id: HIR-0204
title: Layer replay demands each evidence row at the frame its contract declares
status: accepted
introduced_in: unreleased
date: 2026-09-04
failure_class: a_frame_pinned_contract_was_demanded_at_every_frame_its_claim_judged
mechanism: replay_claim_requirements_carry_per_evidence_declared_frames_from_selected_authority
adr: null
---

# Layer replay demands each evidence row at the frame its contract declares

## Observed failure

Room run on `b87035c`, layer finalization. Eight scene contracts, each pinned to one
frame, bound by claims judging three frames. The replay evaluator demanded every id at
every frame the claim judged:

```
frame 113  demanded the 176-pinned rows   -> status "missing", failed []
frame 176  demanded the 113-pinned rows   -> status "missing", failed []
frame 151  demanded all eight             -> status "missing", failed []
```

Each row passed at its own frame and could not exist at the others. The evaluation then
raised `ValueError: layer evaluation canonical[0].verdict cannot pass failed or missing
deterministic evidence`, with an empty `failed` list, so the layer could not finalize and
the shot could make no progress at all.

## Root cause

`LayerReplayClaimRequirement.executable_evidence_ids(frame)` returned the union of every
evidence id of every executable claim whose `judge_frames` contained that frame. The
claim's judge list was treated as each binding's schedule. AGENTS.md states the opposite
and has since HIR-0130: runtime schedules every bound static row at its declared `frame`
or `frames`, falling back to the active judge only when the row is unframed.

The requirement record simply had nowhere to put the answer. It carried `evidence_ids`
and `judge_frames` and no per-id schedule, so the compiler could not have expressed
"this id belongs to frame 176" even though the selected scene contracts say so. The live
builder verdict already reads declared frames (`agents/builder/verdicts._due_here`); the
replay requirement was compiled from the claim alone and reconstructed a schedule by
union. One rule, two implementations, and the durable one was the lossy one.

## Decision criteria

- A row's schedule comes from the row, not from the claim that binds it.
- The requirement record carries the schedule explicitly; a reader never re-derives it.
- An unframed row keeps the existing fallback: due at every frame its claim judges.
- Declared frames are validated against the ids they schedule, so an id outside
  `evidence_ids` or a repeated id is refused at mint rather than silently ignored.

## General mechanism

1. `LayerReplayClaimRequirement` gains `evidence_frames`: an ordered mapping from
   evidence id to the frames that row is due at. Ids outside `evidence_ids` and repeated
   ids are rejected at construction and on `from_dict`; the field round-trips through
   `as_dict`.
2. `executable_evidence_ids(frame)` returns an id when the claim judges that frame and
   either the id declares no frames or declares this one.
3. `evidence/scene_checks.scene_contract_declared_frames` reads `frame`/`frames` from the
   selected scene contracts, and layer-composition finalization fills `evidence_frames`
   from it when minting each requirement. A shot with no selected scene contracts yields
   an empty map, which is the same as declaring nothing.

## Rejected patch-level alternatives

- Letting the evaluator treat a missing frame-pinned row as satisfied: that is
  detect-and-continue, and it would hide a genuinely absent row.
- Widening every contract's `frames` at authoring time so the union is correct: pushes a
  harness defect into every plan and makes each row claim readings it never takes.
- Filtering by frame inside the evaluator: the same re-derivation, one boundary later.

## Validation

- `src/tests/unit/test_evidence_due_at_its_declared_frame.py`: the exact shipped shape —
  two frame-pinned rows on one claim judging both frames are each due only at their own;
  an unframed row stays due at every judged frame; a row declaring several frames is due
  at each; an id outside `evidence_ids` and a repeated id are refused; the field survives
  `as_dict`/`from_dict`.
- `src/tests/unit/test_builder_stop_boundary.py` passes unchanged, including the
  composition fixture that selects no scene contracts.

## Release and rollback

Additive optional field with an empty default, so an existing receipt reads exactly as it
did. Rollback restores the union and the stall.

## Remaining limitations

The declared-frame map is read from the selected scene contracts only. An image contract
carries its frame in the same field and is covered, but a future evidence family that
schedules itself some other way would need its own reader. The live verdict and the
replay requirement now agree on the rule while still holding two implementations of it;
collapsing them onto one predicate is not done here.
