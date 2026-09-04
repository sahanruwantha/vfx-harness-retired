---
id: HIR-0201
title: Pre-write staging gates report together, and a refusal states the transaction outcome
status: accepted
introduced_in: unreleased
date: 2026-09-04
failure_class: independent_pre_write_gates_returned_one_finding_each_so_rules_were_discovered_one_turn_at_a_time
mechanism: collected_unit_local_findings_in_one_refusal_that_names_the_unchanged_candidate
adr: null
---

# Pre-write staging gates report together, and a refusal states the transaction outcome

## Observed failure

Room run `20260904T143607Z-565c1e`, layer 1, one camera unit: **nine stage calls, eight
refusals, about 13.5 minutes**, each refusal naming a different rule.

```
547.0s  scene contract cam-lens-schedule: op "band" requires numeric lo and hi
584.0s  cross-row contradiction: cam-object-count eq 1 over camera.main vs required descendants
614.5s  point-projection interface: camera owner mutates observed selector camera.main.aim_target
653.1s  unit atomicity: camera_targets unresolved_family
673.9s  subject-framing coverage: judge frames [113, 151, 176] have no bbox_* row of a rendered subject
800.0s  point-projection interface again: same row, second cause created by the first fix
```

Hansa's layer 1 showed the same shape at a different boundary: eight refusals over
$1.4979, four of them one intent (an aim control under the camera rig) refused under four
different contracts. Meanwhile the collectable validator in the same sessions returned four
and seven findings in a single call, and the successful stage result already lists every
open layer-level finding at once.

Three times the materializer also issued `unstage_materialization_unit` purely as a probe
after a refusal, to find out whether the failed call had left anything behind.

## Root cause

Two rules that AGENTS.md already states were honoured downstream of these gates and not by
them. Materialization validation "reports every collectable finding in one write" and
HIR-0180 runs that collectable validator inside the stage call, but the unit-local
predicates run *ahead* of it and each `raise`s on its first violation, so a materializer can
only meet one per turn. And a rejection is required to name the observed state; none of
these said what the transaction had done, although HIR-0100 makes a refused stage atomic by
construction, so the model probed for an answer the harness already knew.

The cost is structural rather than incidental: five of the seven gates enforce recent
records (HIR-0125, 0147, 0103, 0096/0099, 0177/0184, 0124/0145), so every gate a new record
adds costs another turn of a bounded per-unit budget. The mechanism gets more expensive
exactly as the harness gets more correct.

## Decision criteria

- The collectable contract belongs to the whole stage call, not to the validator alone.
- Structural preconditions that later gates read (schema, parse, id uniqueness) still fail
  fast: a finding derived from an unparsed candidate is noise.
- A refusal states what the transaction did, so atomicity does not have to be probed.
- No rejection loses content: every message, rule reference and enumerated fix survives.

## General mechanism

`_validate_local_staged_units` collects instead of raising. Every unit-local gate runs, its
findings append to one list, and the call raises once:

```
staging refused before candidate write; 3 unit-local finding(s), nothing was staged and the
candidate is unchanged:
  1. required-claim coverage: ...
  2. cross-row contradiction: ...
  3. unit atomicity: ...
Every unit-local gate ran on this candidate, so the list above is complete: ...
```

Gates that previously reported only their first gap (point-projection interface, same-layer
dressing, construction route) now report every gap. `validate_unit_script_path`, which
raises internally, is captured into the same list.

## Rejected patch-level alternatives

- Reordering the gates so the expensive one comes first: changes which turn pays, not how
  many.
- Raising the per-unit turn budget: pays for rediscovery instead of removing it.
- Telling materializers in the kickoff that refusals are atomic: prose where the refusal
  itself is the place the question is asked.

## Validation

- `src/tests/unit/test_staging_claim_coverage.py::test_a_refused_stage_reports_every_gate_and_the_transaction_outcome`:
  one candidate carrying an uncovered mutated role and a contradictory row pair is refused
  once with three numbered findings (coverage, contradiction, and the atomicity consequence
  of the same edit, which fail-fast would have billed on a later turn), states that nothing
  was staged, and leaves the candidate bytes unchanged.
- The existing staging, subject-framing and staged-write suites pass with their assertions
  updated to the collected shape.

## Release and rollback

Refusal text changes shape: a numbered list under one header instead of a single sentence.
Readers matching the old per-gate prefixes must match the gate label inside the list.

## Remaining limitations

The collected list is unit-local; the collectable validator still runs after it, so a
candidate can be refused twice in sequence, once by these gates and once by that validator.
A gate whose correct repair creates the next violation of the same gate (the point-projection
row that needed a producer split and then a consumed interface) still takes two turns, which
no collection can remove.
