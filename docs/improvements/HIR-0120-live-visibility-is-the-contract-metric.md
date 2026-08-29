---
id: HIR-0120
title: Live visibility is the contract metric
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: parallel_visibility_metrics_and_implicit_threshold
mechanism: canonical_surface_visibility_sampler
adr: ADR-0003
---

# Live visibility is the contract metric

## Observed failure

Room 1046 Layer 2 run `20260829T204459Z-4e2b95` exposed two incompatible meanings
for `visible_fraction` in one builder session. The authoritative
`hero-window-visible-f38` contract measured `0.428571 >= 0.3` and passed, while
`check_scene(kind='visibility', object='facade_hero_window', frame=38)` measured
`0.259`, labeled it `ISSUES`, and implicitly judged it against a hard-coded `0.5`.
The same divergence appeared at the other protected frames.

The window-bay builder spent 160 turns and $9.10, including repeated visibility
diagnostics while calibrating a layout whose authoritative protected contracts were
already green. The following `facade_pier` builder reproduced the split, moved piers to
improve the diagnostic's unrelated bbox-grid score, regressed sealed hero visibility,
and reverted those experiments. It spent another 42 turns and $2.00 on a unit whose
only owned contract (`pier-count`) passed after the first authored mutation. This was
not harmless advisory noise: a tool advertised as judgment-free taught the model that
accepted evidence was defective and induced scene mutations.

## Root cause

The scene-contract evaluator sampled evaluated mesh vertices and polygon centers,
discarded off-screen points, ray-cast only the on-screen surface samples, and applied
the threshold declared on the exact contract row. `check_scene(visibility)` instead
sampled a 3x3x3 grid through the object's world bounding box, counted off-screen/missed
rays in a different denominator, and declared any value below `0.5` an issue.

The two implementations shared a public metric name without sharing an algorithm,
sampling policy, denominator, or decision threshold. That directly violated ADR-0003:
one metric id must select one implementation and sampling policy. It also made an
unbound diagnostic a second acceptance authority.

## Decision criteria

- Live diagnostics and executable contracts call one Blender-side visibility sampler.
- The denominator is on-screen evaluated mesh surface samples; off-screen samples are
  reported separately and cannot dilute the scalar.
- Multi-role contracts retain HIR-0051 logical-AND semantics by applying the same
  sampler per named role and taking the minimum.
- `check_scene(visibility)` reports observation, not a universal acceptance threshold.
  Fully off-screen and fully occluded subjects remain explicit issues.
- Exact PASS/FAIL comes only from `contract_result`, using the bound contract's target.
- Sampling is registry policy. Callers cannot select a second policy with `samples=`.
- Shared-role live checks remain single-subject as required by HIR-0041; aggregate
  contract evaluation remains available through `contract_result`.

## General mechanism

`blender.checks.surface_visible_fraction` owns the evaluated-surface sampling,
frustum test, ray cast, and scalar. The generated canonical scene-contract probe calls
that helper for every selected role. `check_visibility` calls the same helper for its
single resolved subject and adds only diagnostic state: no on-screen samples, fully
occluded, or observed.

The live report names `canonical visible_fraction`, exposes visible, occluded,
on-screen, and off-screen sample counts, and explicitly routes threshold judgment to
`contract_result(id=...)`. The tool schema no longer advertises `samples`; a manually
supplied value fails with the legal action to omit it.

## Rejected alternatives

- Change the diagnostic threshold from `0.5` to `0.3`: another shot or contract may
  require a different bound, and the scalar would still come from the wrong sampler.
- Widen tolerances until both implementations agree: conflicting metric identity
  remains and real occlusion becomes harder to detect.
- Teach the prompt to trust `contract_result` over `check_scene`: the misleading tool
  remains callable and mechanical disagreement is not a prompting problem.
- Remove visibility diagnostics entirely: fully occluded or off-screen unbound subjects
  are still useful deterministic observations when no contract is bound.
- Make a shared role silently aggregate in `check_scene`: that would reverse HIR-0041's
  exact single-subject contract and make crop/subject identity ambiguous.

## Validation

- `test_live_and_contract_visibility_call_one_canonical_sampler` pins both consumers to
  `surface_visible_fraction` and pins the ray cast inside that one helper.
- `test_visibility_report_is_canonical_observation_not_a_second_threshold` pins the
  observation-only report and rejects custom sampling policy.
- The evidence-vocabulary regression compiles the generated Blender probe and proves it
  calls the shared helper while preserving per-role detail.
- Focused visibility, semantic-role, evidence-vocabulary, and builder-instrument suites:
  59 passed. Ruff passed on every changed file.
- The full integration harness reported `ALL PASS (0 failed)`; the full unit suite passed
  590 tests; `.venv/bin/vfx --help` passed.
- Producing run `20260829T220650Z-fb25a5` loaded the new live path. Its cornice visibility
  observation reported canonical surface counts and no invented threshold. The unit's
  candidate and empty-scene replay retained protected
  `hero-window-visible-f38 = 0.428571 >= 0.3`, and `facade_cornice` sealed at 5.0.

Implementation commit: `e4e9b2c` (`fix: unify live and contract visibility metrics`).

## Release and rollback

No persisted schema changes. The removed `samples` knob was an unversioned diagnostic
option whose use would recreate a second metric identity; callers must migrate by
omitting it. Rollback restores contradictory evidence and is unsafe.

## Remaining limitations

`check_scene` bbox and framing remain deliberately single-subject, and
`inspect_scene(objects)` still lacks evaluated world-bbox minima/maxima. Room 1046
builders repeatedly attempted read-only `run_bpy` probes for those bounds; that is a
separate missing-instrument defect and is not broadened into this visibility mechanism.
