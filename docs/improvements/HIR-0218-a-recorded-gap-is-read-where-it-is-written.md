---
id: HIR-0218
title: A recorded vocabulary gap is read where it is written
status: accepted
introduced_in: unreleased
date: 2026-09-05
failure_class: the_validator_read_recorded_gaps_relative_to_the_plan_bundle_instead_of_the_shot
mechanism: one_shared_path_function_and_a_shot_folder_the_validator_takes_by_name
adr: ADR-0004
supersedes_scope_of: HIR-0202
---

# A recorded vocabulary gap is read where it is written

## Observed failure

room_1046_opening layer 2, archived run `20260905T004004Z-c238a9`. Decoding the
`requirement_bindings` payload of every staging call naming R18, in order:

```
seq 28  escalate_vocabulary_gap(R18)                              <- VG-001 recorded
seq 32  stage R18 [NO contract_ids | decision=approved_start]  -> REFUSED "declares only
                                                                  structural domains ['scene'] …
                                                                  call escalate_vocabulary_gap
                                                                  for this requirement first"
seq 35  stage R18 [NO contract_ids | decision=approved_start]  -> REFUSED, same text
seq 41  stage R18 [contract_ids=['VG-001'] | decision=…]       -> REFUSED "names absent
                                                                  contracts: VG-001"
seq 43  escalate_vocabulary_gap(R18)                              <- VG-002
seq 61  escalate_vocabulary_gap(R18)                              <- VG-003
```

The materializer performed the instructed action, then submitted the prescribed shape —
an `approved_start` decision carrying the authored statement, no contract — **twice**, and
was refused both times. Only after that did it improvise VG-001 into `contract_ids`, which
is correctly refused because a gap id is not a contract id. It escalated three times
against a refusal that could not change. The session never published.

VG-003's own `note` field reads: *"Retry with claim text set verbatim to the requirement
statement, in case gap-to-requirement linkage matches on exact statement text."* That is a
session correctly diagnosing a linkage failure it had no instrument to see.

Reproduced independently in room_1046_opening's **current** run at `09daa94` on a fresh
bundle: `VG-001` recorded for R33 (`evidence_domains ["projected_composition","temporal"]`,
structural-only, no decision in its resolution), refusal A fired twice afterwards at
953.4s and 1013.6s, and the materializer abandoned the decision route and bound six
contracts instead. VG-001 is orphaned durable audit state that closed nothing.

## Root cause

The writer and the reader disagree about where the record lives.

```
writer  agents/plan_tools/gate.py:109                  shot_folder / "state" / "plan-escalations"
reader  jit_materialization/validate_requirements.py:76  Path(root) / "state" / "plan-escalations" / …
        jit_materialization/validate.py:95              root = Path(global_root)
        agents/plan_tools/materialize_mcp.py:394        global_root=authority.bundle_root
```

`global_root` is the selected **plan bundle** — content-addressed immutable authority.
Proven at `09daa94` against the archived shot:

```
recorded_vocabulary_gap_ids(<bundle root>)  -> {}
recorded_vocabulary_gap_ids(<shot folder>)  -> {'R18': ('VG-001','VG-002','VG-003')}
bundle contains a state/ dir?  False
bundle top level: acceptance.json assumptions.json bundle.json checks.json critic_axes.json
                  global.md layers.json obligations.json plan.provenance.json plans
                  requirements.json scene_checks.json
```

A bundle has no `state/` directory and never will, so the read returns `{}` in **every
shot and every run**. The `if gap_ids:` branch that widens `decision_domains` to cover
structural domains — the whole of HIR-0202's mechanism — has never executed in production.

Two things kept it invisible:

1. **`except OSError: return {}`.** The reader's own docstring called this "a malformed or
   missing file means no gap, never a crash in a validator". It also made *a wrong
   directory* indistinguishable from an empty file. AGENTS.md forbids exactly this: never
   use a broad exception to turn invalid authority into apparent success.
2. **Every fixture passed one directory as both roots.** `test_vocabulary_gap_closure.py`
   calls `_record_gap(root, …)` and then `validate_materialization(root, …)` where `root`
   is a single `tmp_path`. In production those are different directories three levels
   apart. So HIR-0202 shipped with tests that passed, asserting a behaviour production
   never had — the test encoded the bug's own assumption.

The earliest owning decision is HIR-0202's: it added a reader for durable shot state and
handed it the argument that was already in scope, a plan-bundle root, rather than naming
the folder it needed. The boundary that should have prevented it is the parameter list —
`root` was ambiguous enough to accept the wrong directory silently.

## Decision

- One function owns the location: `domain/vocabulary_gaps.vocabulary_gaps_path(shot_folder)`,
  called by the escalation writer and the validator's reader alike.
- `validate_materialization` and `inspect_materialization` take `shot_folder` as a
  **required** keyword, distinct from `global_root`, and `MaterializationInspection`
  carries it as a field. Required rather than defaulted: a default would let a call site
  omit it and silently reproduce the defect.
- The reader takes the shot folder by name, so a caller cannot hand it a bundle.
- Tolerance of unreadable rows is **kept unchanged and deliberately out of scope**. The
  file is an append-only journal, a torn trailing line is a real possibility, and a
  validator that died on one would strand a shot in state an operator may not hand-edit
  (AGENTS.md forbids hand-editing `state/`). That tolerance was never the defect; reading
  the wrong directory was, and the two were indistinguishable only because both produced
  an empty result. Changing it would move a suite assertion without evidence that a
  malformed row has ever occurred.

## Validation

`src/tests/unit/test_vocabulary_gap_closure.py::test_a_recorded_gap_is_read_from_the_shot_folder_not_the_plan_bundle`
builds the production shape — bundle at `<shot>/runs/r1/checkpoints/plans/bundles/h0`,
shot at `<shot>` — and asserts the **outcome the refusal promises**, in three steps:

1. no gap recorded → the decision is refused naming the escalation as the path;
2. a gap recorded **in the bundle**, where the old reader looked → still refused;
3. a gap recorded **in the shot**, where the tool writes → the decision closes.

Step 2 is the discriminator, and it separates the implementations by behaviour rather than
by signature. Running both readers over one fixture with a gap in each location:

| gap location | pre-fix reader (root=bundle) | post-fix reader (shot folder) |
|---|---|---|
| bundle only | `{'R-form': ('VG-BUNDLE',)}` — closes | `{}` — inert |
| shot as well | `{'R-form': ('VG-BUNDLE',)}` — still blind | `{'R-form': ('VG-SHOT',)}` — closes |

A test of the reader alone would have passed on both sides of the bug, which is why the
assertion is on the staging outcome. The point was raised by the room_1046_opening driver
and it is the reason this test is shaped this way.

## Rejected alternatives

- **Derive the shot folder from `resolutions_path`.** It already points at
  `<shot>/state/plan-resolutions.jsonl`, so `.parent.parent` would work today. It couples
  two unrelated arguments and breaks silently the first time a caller passes a copy.
- **Default `shot_folder` to `global_root`.** Zero call-site churn and it reproduces the
  defect for any caller that forgets, which is the caller that had it.
- **Let the escalation tool write into the bundle.** Bundles are immutable content-addressed
  plan authority; durable shot state does not belong in one (ADR-0004).

## Reconsider when

A caller legitimately needs to validate a materialization against a shot it is not part of
— a cross-shot preview. `shot_folder` would then need to be the authority the gaps belong
to rather than the shot being validated, and the two would separate again.
