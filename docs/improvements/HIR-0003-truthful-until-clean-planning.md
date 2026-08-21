---
id: HIR-0003
title: Fail closed and aggregate structural defects in until-clean planning
status: accepted
introduced_in: unreleased
date: 2026-08-21
failure_class: dirty_plan_reported_as_passed
mechanism: explicit_plan_outcome_and_exhaustive_dependency_feedback
adr: null
---

# Fail closed and aggregate structural defects in until-clean planning

## Observed failure

Run `20260821T115227Z-a8f966` invoked `vfx plan /home/sahan/Desktop/vfx-test
--until-clean`. After three repair rounds the final deterministic gate still reported two
blocking findings: Layer 5's `ignition_pulse` named the Layer 4 unit
`exposure_hierarchy_lighting` in its layer-local `depends_on`. The command nevertheless exited
0, while `runs/latest.json`, `status.json`, and `reports/summary.json` all recorded `passed`.

The same run began with four instances of the same cross-layer dependency defect. Typed loading
raised on only the first invalid layer, so each paid repair round saw and fixed one edge. The run
spent all three rounds without ever receiving the complete defect set. Five model sessions cost
$19.2715 and the command ran for 4,945 seconds (82m25s).

## Root cause

`generate_plan_until_clean()` reduced its terminal authority to a `Path`, discarding whether the
loop ended `clean`, `stalled`, or `budget`. `main()` therefore left the run-artifact invocation
normally, which unconditionally published a passing terminal record.

Separately, the plan gate delegated DAG validation to `load_layers()`. That loader correctly
fails closed but raises at the first invalid layer, which is suitable for runtime consumption and
unsuitable for a repair brief intended to fix all independent structural defects at once.

## Decision criteria

- A dirty plan remains available as diagnostic evidence but never looks accepted.
- Direct planning and planning inherited by the full driver use the existing deterministic-check
  exit code 3.
- Repair feedback reports independent repetitions of one structural defect in one free gate pass.
- Typed runtime loading, layer-local dependency semantics, and build ordering remain unchanged.

## General mechanism

- Return an explicit `PlanLoopResult` containing path, terminal outcome, and blocking count.
- After writing the plan and provenance, raise a detail-carrying exit 3 when the outcome is not
  clean. The run-artifact boundary records `failed`; an inherited full run receives the same
  non-zero stage result.
- Add an exhaustive raw dependency pre-pass to the deterministic gate. It reports every stage
  whose `depends_on` names no unit in that same layer, then defers typed loading and claim closure
  until those edges are repaired.

## Rejected patch-level alternatives

- Treat exit 0 plus a console warning as sufficient: structured readers already selected the run
  as passed, violating fail-closed authority.
- Increase the default repair budget: repeated first-error discovery scales cost with layer count
  and still permits exhaustion.
- Teach the prompt not to emit cross-layer edges: prompt discipline cannot replace an executable
  contract and would not fix truthful terminal status.
- Make the runtime loader collect every schema error: runtime consumers benefit from its simple
  fail-fast contract; exhaustive repair feedback belongs to the evaluation boundary.

## Validation

- `tests/unit/test_planner_outcomes.py` drives the real planner `main()` with a bounded dirty
  outcome. It exits 3, preserves `plans/global.md`, stamps provenance, and publishes failed
  run status.
- `tests/unit/test_run_artifacts.py` verifies the detail-carrying exit produces failed
  `status.json` and `reports/summary.json` with exit code 3.
- `tests/contract/test_planner_artifact_paths.py` supplies three cross-layer dependencies in
  three different layers and receives all three targeted findings in one gate pass.
- The live failing shot now reports both remaining edges in one invocation:
  `.venv/bin/vfx evals plan /home/sahan/Desktop/vfx-test` exits 3 and names
  `ignition_pulse → exposure_hierarchy_lighting` and
  `volumetrics_particulate → final_lock`.
- `.venv/bin/ruff check src tests` passes.
- `.venv/bin/python -m pytest -q` passes: 70 tests after the related plan-evidence improvements.
- `.venv/bin/vfx --help` exits 0.

## Release and rollback

Target: next minor release. Compatibility change: automation that previously treated a
budget-exhausted dirty plan as exit 0 must handle exit 3. Rollback is the bounded revert of the
planner outcome and dependency pre-pass; no shot schema migration is required.

## Remaining limitations

- Other malformed schema fields may still fail fast at the first typed-loader exception.
- Warn-level research provenance findings do not block a plan and are unchanged.
- Repair quality remains model-dependent after the gate supplies a complete deterministic brief.
