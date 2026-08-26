---
id: HIR-0021
title: A reopened unit was forbidden the exact edits its retry prescribed
status: accepted
introduced_in: unreleased
date: 2026-08-26
failure_class: convergence_guard_blind_to_qualitative_verdicts_deadlocked_reopened_unit
mechanism: audited_reopen_arms_judgment_unresolved_until_first_in_session_verdict
adr: null
---

# A reopened unit was forbidden the exact edits its retry prescribed

## Observed failure

Shot `vfx-test`, unit `2.atmosphere`, 2026-08-26. After the unit's executable rows all
passed while its canonical judgment kept failing one qualitative axis at f150 (critic
1.0–2.0 against a 5.0-sealed f72), five consecutive builder sessions were denied the
mutations they were reopened to make:

- Attempt 7 (run `20260826T094216Z-aad28b`) is the clean specimen. The audited
  `vfx units retry` at 09:42:14 carried a complete, evidence-cited prescription (feather
  the area-light beam edge with noise-modulated world-volume density, move near-camera
  haze into the bounded domain, preserve the depth gradient — proven by matcap and
  per-light isolation renders). The retry-reason channel delivered it into the session
  prompt (HIR mechanism from commit 039847a, verified verbatim). The session
  baseline-measured the beam regions within 90 seconds — then hit
  `ACTIVE UNIT CONTRACTS ALREADY PASS. Further speculative mutation is blocked; this
  unit binds no unresolved image evidence. Finish required read-only diagnostics and
  hand off` at minute 1.3, and five more times after. It handed off substantially
  unchanged; the critic failed f150 at 1.0. Cost $2.48, 51 turns, verdict regression
  2.0 → 1.0 (the prior 2.0 lived in a rolled-back failed attempt; the cuffed session
  could not re-earn it).
- The hook counter isolates the mechanism across the unit's whole post-amendment
  history: `convergence_mutation_blocked` = 3, 5, 5, 4, 6 on the five failed attempts
  (runs `a3c162`, `7c3f4c`, `e6bbe5`, `538881`, `aad28b`) versus 2 on the one passing
  attempt (`af3084`, where the throttle correctly engaged only at end-of-session).
  Every failure of this unit since its amendment occurred with the builder's hands
  cuffed; the score plateau "stuck at critic 1–2" is this deadlock's signature, not a
  craft ceiling.

## Root cause

`builder_phase_guard`'s convergence predicate consulted only executable phase flags:
`scene_contracts_passed`, `image_evidence_required`, `pixel_contracts_passed`. A unit's
blocking evidence also includes required qualitative claims decided by canonical
judgment — evidence the guard could not represent. The in-session round loop
compensated (`phase["scene_contracts_passed"] = False` on a critic-backed revision:
"the only event that reopens geometry mutation"), but no cross-session analogue
existed. A unit reopened after a failed judgment resumes from a checkpoint where every
executable row passes; the automatic scene-contract probe flips the flag at session
start; from then on the guard reads "converged" while acceptance reads "failing" — two
parallel implementations of "does this unit have open work," disagreeing terminally.
Every retry burns a full session cost into the same wall regardless of prescription
quality, and rollback-on-failure converts each cycle into score regression. This is the
ADR-0003 defect class (parallel implementations of one predicate) applied to the
completion predicate, and it violates the instruments rule in its dual form: an agent
forced to *stop* by an instrument that measures the wrong thing.

## Decision criteria

- One source of truth: the arm must derive from the same record that explains the retry
  to the model — the audited reopen event — so prompt and guard cannot disagree.
- Exactness without inference: the guard only bites when every executable row passes,
  so a unit that was terminal-failed and audibly reopened must, by elimination, have
  failed on evidence the guard cannot observe. Arming on the reopen record therefore
  has no false positives; executable-failing retries never consult it.
- Preserve the ratchet: first-build convergence, the mutate→compare cadence, and the
  hand-off-for-first-judgment flow must be byte-identical for every non-reopened
  session.
- Bounded life: the arm is not a standing permission. The first in-session verdict
  supersedes the cross-session record, and the round discipline resumes ownership.
- No new authority: the arm permits mutation only. Sealing still requires canonical
  judgment; nothing self-certifies.

## General mechanism

Three edits, one phase key:

- `agents/guardrails.py` — the convergence deny requires
  `not phase.get("judgment_unresolved")`.
- `agents/builder.py` (`build_unit`) — the single unit-state read that renders the
  `REOPENED BY OPERATOR` prompt block also sets `phase["judgment_unresolved"] = True`.
  One record, two consumers. The record must be the unit's *live* lifecycle fact:
  `_live_reopen_reason` scans newest-first and stops at a later seal (`passed`) or
  amendment re-entry (`pending`), which consume the reopen — otherwise a retired retry
  reason would steer, and un-cuff, a rebuilt unit (stale-context injection, the same
  defect class as a superseded approach's role names surviving into a rematerialized
  unit).
- `agents/builder.py` (round transition) — beside the existing critic-revision reset,
  `phase["judgment_unresolved"] = False`: the reopened session's free mutation window
  mirrors a first build's exactly — open until the first judgment, owned by the round
  discipline after it.

## Rejected patch-level alternatives

- Prompt wording telling the session to work around the guard: hooks enforce what
  prompts request; a deny is mechanical and prompt text cannot override it.
- Removing or loosening the guard (mutation allowed after N denials, larger budgets):
  reopens the unmeasured beauty-churn the guard exists to stop; arbitrary thresholds
  are detect-and-continue.
- Treating declared look capabilities as permanently unresolved image evidence: opens
  indefinite mutation for every look unit even after genuine convergence — broadens the
  wrong predicate.
- Hand-editing unit state or checkpoints so an executable row fails and mutation
  reopens: falsifies authority to smuggle permission.

## Validation

- Pinned regression, `tests/integration/test_harness.py`:
  `an operator-reopened failed judgment keeps live mutation legal` — reproduced failing
  (✗) with the guard edit reverted, passes with it; `the first in-session verdict
  retires the reopen arm` — the deny is preserved once the arm clears. Liveness of the
  record is pinned three ways: a live reopen reason is surfaced; a seal retires it; an
  amendment re-entry retires it.
- Negative control from the shot: run `20260826T095918Z-82a02b` (started 09:59:18,
  before the mechanism reached disk at 10:06–10:08) shows the `REOPENED BY OPERATOR`
  block and convergence denials coexisting in one transcript — exactly the state the
  pinned test proves impossible under the fixed code, confirming the defect, the seam,
  and that the fix was not aboard that process.
- `.venv/bin/ruff check src tests`: clean. `.venv/bin/python -m pytest -q`: 295 passed.
  `.venv/bin/python -m tests.integration.test_harness`: ALL PASS (0 failed).
  `.venv/bin/vfx --help`: ok.
- Live confirmation pending: the next audited retry of `2.atmosphere` must show
  prescription edits landing after the automatic probe (`run_bpy` accepted while
  executable rows pass) and `convergence_mutation_blocked` ≈ 0 before the first
  verdict. Per the verification rule, only a run through the fixed path is a verdict on
  the fix.

## Release and rollback

No schema, migration, or flag. The venv imports the working tree, so the next `vfx`
invocation executes the mechanism. Rollback is reverting the three edits; all
non-reopened flows are unchanged by construction.

## Remaining limitations

- The arm records *that* a failed judgment is unresolved, not *which* claim — the full
  unification (guard consuming the acceptance closure directly, one computed predicate)
  is ADR-scale and should ride the planned unit-scope/coordination-budget ADR.
- After the first in-session verdict the one-edit-per-revision cadence governs; a
  many-edit prescription must spread across revisions or a further (now legal) retry.
- Proven on one fixture. A held-out fixture — reopened unit, executable rows passing,
  qualitative claim failing — belongs in the eval suite alongside the instrument work
  (solve-to-target, best-candidate ratchet) queued behind this fix.
