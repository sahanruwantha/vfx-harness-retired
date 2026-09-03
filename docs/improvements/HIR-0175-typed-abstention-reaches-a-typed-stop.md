---
id: HIR-0175
title: Contradictory derivative bounds are refused at authoring, and a falsification finding reaches its typed stop
status: accepted
introduced_in: unreleased
date: 2026-09-03
failure_class: unsatisfiable_authority_reaches_the_builder
mechanism: authoring_time_bound_consistency_and_capsule_bound_stop_identity
adr: null
---

# Contradictory derivative bounds are refused at authoring, and a falsification finding reaches its typed stop

## Observed failure

Fresh run `20260902T190446Z-88aeb3` on `artifacts/room_1046_opening` (main `06f577c`) after
HIR-0174. Layer 1 materialized four `curve_derivative_max` rows on role `camera.rig`,
property `location`: `cam-path-smooth` (`max`, `hi 0.06`, frames 1..213),
`speed-ramp-early` (`max`, `hi 0.03`, 1..113), `speed-ramp-late` (`min`, `lo 0.12`,
113..175), `speed-ramp-end-decel` (`max`, `hi 0.04`, 200..213). Materialization
validation, the finalization gate, and the plan gate all passed them. The camera_path
builder proved that the floor demanded inside 113..175 is the same peak the global cap
measures, recorded `cannot_express_in_scope` (23 turns, $1.06) and the unit was marked
`hypothesis_falsified`, exactly as designed.

The typed stop then never published. `compile_hypothesis_falsification_stop` compared the
finding's `plan_hash` with the SHA-256 of the whole selected `layers.json` and refused:
`hypothesis falsification belongs to a superseded selected layer view; current=0356…,
finding=6b57…`. The builder raised `BuildAuthorityDefect`, the stage returned without typed
stop authority, and the driver stopped the run as an unclassified `harness_defect` routed to
engineering instead of the amendment-requiring plan defect.

The same plan had also authored judge frame 0 for `refs/frame_0s.jpg` for the second time
(the first was OBS-2 on 2026-09-02); the gate repaired it in one turn each time.

## Root cause

1. `validate_row_set` refuses a `keyframe_schedule` that already exceeds a same-role
   derivative cap (HIR-0030) but had no rule for two derivative rows contradicting each
   other: a floor (`min`/`band` with `lo`) inside a cap (`max`/`band` with `hi < lo`) on the
   same roles and property over a containing window is unsatisfiable by construction.
2. Durable unit state and findings carry the selected layer capsule digest as
   `plan_hash` since HIR-0171 ("the complete selected `layers.json` hash is not a unit
   acceptance identity"), but the stop compiler still hashed the file. No real
   falsification had run through that boundary since the change.
3. The global kickoff stated the frame count and fps without the 1-based convention, and
   the judge-frame rejection named only "positive integer".
4. The derived falsification projection lived at `state/work-units/hypothesis-falsifications/`,
   inside the strict live work-unit namespace whose enumerator refuses every unrecognised
   member (HIR-0171). The first rematerialization after the finding failed its terminal
   gate with `unknown or unsafe work-unit state member: hypothesis-falsifications` and
   burned its session retrying finalize.

## Decision criteria

- Refuse unsatisfiable authority before builder spend (HIR-0030's class).
- One identity per authority: the stop boundary compares the same capsule digest unit
  state records; no second projection of `layers.json`.
- A recurring guess is a missing compiled fact, not a prompt tweak: the convention is
  stated once with the numbers the planner needs.

## General mechanism

1. `derivative_bound_contradictions(rows)` (`evidence/scene_checks/validate.py`) pairs
   every floor row with every cap row on the same sorted roles and property whose window
   contains the floor's window and whose `hi` is below the floor's `lo`; `validate_row_set`
   appends the finding, so staging, materialization validation, and the plan gate all refuse
   it naming both rows, both windows, and the legal repairs. Partial overlap, different
   roles or property, or `lo <= hi` are not contradictions.
2. `compile_hypothesis_falsification_stop` compares `finding.plan_hash` with
   `authority_capsule_resolution.selected_layer_capsule_digest(shot, layer, selected)`;
   the fixture pins that `plan_hash` is not the file hash and a replaced capsule still
   refuses.
4. The projection directory is `state/hypothesis-falsifications/`, beside the strict
   namespace; durable state remains the sole authority and the projection is
   regenerable through `reconcile_falsification_projection`. A shot carrying the old
   directory moves it once; nothing reads the old location.
5. The materialization kickoff compiles `Judgment property authority` from the sparse
   layer row: the closed vocabulary and, for a camera-providing layer, that only
   `camera_framing` is legal; every layer-1 materialization had paid one or two
   rejection turns to learn that rule.
3. The global planner kickoff states `Frames are 1-based: frame 1 is t=0.0s and
   frame(t) = round(t*fps)+1, so every judge frame lies in 1..N`, and `JudgePoint.parse`
   teaches the same convention with the found value.

## Rejected patch-level alternatives

- Telling the materializer to "check speed windows" (prompt wording for a mechanical
  contradiction).
- Making the builder's abstention cheaper (it did the right thing; the authority was wrong).
- Accepting either digest at the stop boundary (two identities for one authority).
- Parsing reference file names for times (shot vocabulary in core).

## Validation

- `src/tests/unit/test_derivative_bound_contradictions.py`: the run's rows are refused with
  both windows named; partial overlap, other roles/property, satisfiable bounds and band
  rows on both sides behave as specified; `validate_row_set` carries the message.
- `src/tests/unit/test_builder_stop_boundary.py`: the fixture's `plan_hash` is a capsule
  digest distinct from the file hash; a replaced capsule is refused.
- `src/tests/unit/test_judge_frame_convention.py`.
- Live: rematerializing layer 1 of the same shot after this change must refuse the
  contradictory rows at staging, and the next falsification (if any) must publish the
  typed `authority_defect` envelope.

## Release and rollback

Unreleased; no schema change. Rolling back restores the file-hash comparison (every
falsification stop fails as harness_defect) and lets contradictory bounds reach builders.

## Remaining limitations

- Only `curve_derivative_max` pairs are checked; other kinds with window semantics
  (`radial_distance_trend`, `transform_return_delta`) have no cross-row consistency rule.
- A floor whose window only partially overlaps a cap is accepted even when the
  non-overlapping part is a single frame; the builder's abstention still covers that case.
