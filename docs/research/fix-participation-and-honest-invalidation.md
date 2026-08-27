# A run that never executed the fix cannot judge it

**Status:** open — cause established from the HIR-0014..0019 history; mechanism direction
proposed; promote to an HIR after the closure-width measurement at the end of this note.

## The circle

A failure surfaces in step 2. The root cause is in step 1. The fix lands in step 1's
mechanism. The pipeline resumes at step 2 — consuming step-1 products sealed **before** the
fix. The failure recurs. Nothing records that the fix never participated in the run, so the
recurrence reads as "the fix didn't work": another investigation, another fix, another paid
run. Every lap costs real money and, worse, misdirects the next change, because a recurring
symptom cannot be cheaply classified into the three cases that demand different actions:

1. the fix never ran (stale upstream products) — re-run the producer, change nothing;
2. the fix ran and failed — the fix is wrong, reopen the record;
3. a new failure that resembles the old one — new capture.

While those are indistinguishable, improvements are invisible: the harness may already be
correct and still look broken, or still be broken and look patched. The improvement
lifecycle's promise — "the preserved failing case now passes for the intended reason" — is
quietly voided on production paths, because production can resume around the mechanism under
validation.

## Evidence the circle is real

- HIR-0018: two builds plus four repair rounds (~55 min and 162 tool calls in the first
  build alone) re-ran downstream against contract rows authored defectively upstream — one
  of them structurally unsatisfiable by the node class the contract intended. No repair
  could ever have moved it.
- HIR-0019: three units sealed lookdev on surfaces that were never on screen; everything
  downstream of layer 1's falsified spine was noise, and the verdict-producing renders were
  the back of a blockout disc.
- HIR-0017: "the materialize→gate→publish path had never once passed end-to-end — every unit
  plan a build ever consumed had arrived through the leak." Weeks of downstream results were
  read as signal about a path that had never actually run.
- Pending now: the unit-1 artifact accepted under afec73-era instruments fails current
  revalidation and awaits rebuild; until then, any consumer of it validates against a product
  the current harness disowns.

## Root cause, two halves

**Resume paths can consume pre-fix products.** `orchestration/revalidation.py` embeds
producer identity in the input manifest as a hand-enumerated list of seven source files. A
fix landing anywhere else — `evidence/metrics.py`, `evaluation/plan_gate.py`,
`blender/tools.py`, prompt modules — does not perturb the cache key, so outcomes sealed
under the broken mechanism stay eligible for fast-path reuse. This is HIR-0014's shape
(silence reads as consent) inside the invalidation mechanism itself.

**No run states which mechanism identity it executed.** Reports do not say "this run ran
under harness state X," so whether a failure recurred under the old or the new code is
reconstructed from timestamps and memory — a guess, in a harness whose guide now forbids
deciding by guesses.

## Candidate mechanism

1. **Derived producer identity.** Replace the hand-list with a derived closure: the source
   modules transitively imported by the producing/validating entry points (builder, replay,
   evidence, gate), computed from the import graph and digested into the manifest. A change
   inside the closure invalidates what it could have produced; a change outside does not.
   One derivation, no hand-enumeration — the same move as the layer-DAG identity. Open:
   non-import dependencies (prompt text, `knowledge/` recipes, configuration) need declared
   or derived inclusion.
2. **Tiered identity, because pixels and verdicts age differently.** Two closures: one over
   what produces pixels (scripts, session, worker, builder), one over what produces verdicts
   (evidence, gate). An instrument fix invalidates verdicts — sealed renders are re-judged,
   which is cheap — while a producer fix invalidates pixels, which forces rebuild. Most of
   the honest-invalidation cost objection dissolves here if the split is real; measuring
   that is the point of the probe below.
3. **Participation provenance.** Every run report and every sealed outcome records the
   closure digests it ran under. "Did the fix participate" becomes a recorded fact: after a
   fix, the next run either shows the producer re-ran under the new identity and the failure
   is gone (the improvement is *observed working*, in production, immediately) or shows the
   failure persisting under the new identity (the fix is wrong — also a true signal,
   immediately). The circle depends on this fact being unrecorded.
4. **Refusal at resume.** Eligibility already fails closed on manifest drift; with honest
   producer identity the refusal becomes complete: running step 2 against step-1 products
   the current mechanism disowns stops being representable. "Run step 1 again" is enforced,
   not remembered.

## What would reject this direction

Closure width. If the derived closure covers most of the package, honest identity collapses
into whole-package invalidation and every commit forces full rebuilds — unaffordable in a
week that produced six HIRs. The cheap offline probe: compute both closures on the current
repo, then replay the last ~30 commits against them and count which sealed outcomes each
commit would have invalidated, at which tier. If the verdict tier absorbs most fixes (the
HIR record suggests it: 0014, 0015, 0018, 0019 are instrument fixes), honest invalidation is
affordable and this promotes to an HIR. If everything lands in the pixel tier, the design
needs finer producer boundaries before mechanization, and this note stays open with that
measurement attached.
