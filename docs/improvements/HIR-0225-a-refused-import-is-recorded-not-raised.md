---
id: HIR-0225
title: A refused import is recorded, not raised
status: accepted
introduced_in: unreleased
date: 2026-09-05
failure_class: one_denied_import_short_circuited_the_collector_and_crashed_the_layer
mechanism: the_six_import_refusals_record_at_their_line_and_report_with_every_other_violation
adr: null
---

# A refused import is recorded, not raised

## Observed failure

room_1046_opening run `20260905T115410Z-e290b0`, pinned `3875027`, layer 2. `tower_shell`
**passed** — 11/11 owned scene contracts, `unit evidence: 7/7 bound checks observed · PASS`,
`best round: r1 executable mean 5.0 (no raster owed)`. Then finalization crashed the layer:

```
agents/builder/unit_finalize.py:121   flagged = annotate_journal(journal)
blender/journal_policy.py:57          violations = artifact_violations(entry, tree)
blender/artifact_execution.py:382     bindings = _artifact_bindings(tree)
blender/artifact_execution.py:185     raise ArtifactExecutionPolicyError(...)
ArtifactExecutionPolicyError: artifact import denied: itertools
```

`✗ layer 2 exited 1: crashed`, `terminal_cause: harness_defect`, `stop_stage:
infrastructure`. The trigger was two journal lines:

```
123: import itertools
124: signs = list(itertools.product([-1,1], repeat=3))
```

$3.07 of passing build discarded at write time, on a unit that had already earned its
receipt. caesar_curia lost a run to the same mechanism earlier with `collections` — two
shots, two modules, both complete and correct before the write refused them.

## Root cause

`artifact_violations` calls `_artifact_bindings(tree)` on its first line, and that function
**raised** on the first denied import instead of recording it. So the collector never ran.

Its own docstring, four lines above the call, is the argument against it:

> The walk finds them all and used to report the first. A finalizer fixing line 24 then
> paid another write-then-probe round trip to be told about line 25 … three cycles on one
> candidate, on the most expensive phase in a shot (HIR-0216).

**For imports specifically the outcome is strictly worse than the behaviour HIR-0216
replaced**: not "report the first violation" but report none and take the process down.
The asymmetry is visible within one shot — room's layer 1 annotated eleven refused
*capability* constructs in place and sealed:

```
[ 140.5s] journal: 2 line(s) carry constructs the artifact policy refuses — each is
          annotated in place with its legal form
[1751.6s] journal: 9 line(s) ... annotated in place
```

Same policy, same entry point, two exit paths. One teaches; one crashes.

Six refusals raised, not three: denied root on `Import`, `bpy` submodule, relative import,
denied root on `ImportFrom`, from-`bpy`, and wildcard.

## Decision

All six record at their line and return alongside the bindings that did resolve.
`artifact_violations` seeds its findings with them, so an import refusal reports in source
order with every capability refusal in the same walk, and `annotate_journal` writes the
legal form in place.

**Recording admits nothing.** `_artifact_bindings` is called from exactly one place, and the
execution boundary still raises on any violation:

```
denied import    REFUSED: artifact import denied: itertools
bpy submodule    REFUSED: artifact bpy submodule imports are denied; import bpy directly
relative import  REFUSED: relative artifact imports are denied
from-bpy         REFUSED: artifact from-bpy imports are denied; import bpy directly
wildcard         REFUSED: artifact wildcard imports are denied
legal artifact   ACCEPTED
```

A denied module is also not bound, so it grants no capability chain.

## Validation

`src/tests/unit/test_artifact_execution_policy.py`:

- room's exact journal — `itertools`, `os`, `bpy.ops` — reports **three** violations at
  lines `[2, 4, 5]` in source order rather than crashing on the first;
- all six refusal kinds are recorded rather than raised;
- a denied import and a capability violation report together, which is why all six convert
  rather than only the one that fired;
- **the guard**: the execution boundary still refuses every one and still accepts a legal
  artifact. That test passes with and without the mechanism, deliberately — it asserts the
  policy did not move.

The first three fail on the pre-fix code with `ArtifactExecutionPolicyError` at line 185.

## Why this is crash-safety, not only message quality

The controller correctly refuses to dispatch this shape: `route_engineering has no
receipt-backed controller adapter; the envelope remains the run's terminal stop`. So an
uncaught exception in the finalize path costs a whole layer, every time, with no automatic
recovery. That is the room_1046_opening driver's observation and it is the stronger half of
the argument: the conversion removes a class of layer-killing crash, and the better
reporting is a consequence.

The boundary's `detail` still reads *"The 'build' boundary returned without typed stop
authority"* — true about the boundary, silent about `import itertools`. That remains open
as the stage-boundary half of HIR-0224.
