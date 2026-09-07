---
id: HIR-0250
title: Flynn revisions require a measured candidate
status: accepted
introduced_in: unreleased
date: 2026-09-07
failure_class: candidate_rewrites_exhausted_budget_before_observation
mechanism: phase_derived_write_probe_revision_grants
adr: ADR-0012
---

# Flynn revisions require a measured candidate

## Observed failure

The live three-unit probe recorded in HIR-0249 stopped at the producer after three
distinct candidate writes without a probe. `probe_candidate` was available after
each write, but `write_candidate` remained available too. The final abstention was
an effect of the external-action reserve, not evidence that the earlier requests
lacked a probe tool. The independent unit had already passed and remained verifiable.

The preserved probe is `/tmp/vfx-live-flynn-dag-pss3bdq4`, including its summary,
provider traces and exact run-owned SQLite operations. It used 10 model calls,
34,667 prompt tokens and 1,068 completion tokens. No composed layer was accepted.

## Mechanism and ownership

VFX derives model grants from candidate/observation phase. A write permits probe or
abstention next. A returned probe permits revision, freeze or abstention; the model
cannot repeatedly probe the same observed candidate. Revision clears observation,
requiring a new probe. Initial inspection remains bounded by HIR-0248. Independent
canonical replay keeps its separate scripted grant and budget reservation, and the
existing VFX receipt writers remain the only acceptance authority.

This is a harness policy: Flynn already enforces narrowed grants before executing a
tool. No generic SDK change is needed. Increasing the budget, adding history or
requesting better model discipline would leave the unobserved rewrite mechanically
legal. The phase gate enforces read-back while preserving revision based on evidence.

## Regression

`test_flynn_candidate_revision_requires_measured_feedback` first failed on the old
policy's second model request because `write_candidate` was still granted. The test
writes a candidate without the required control, measures its failure in real Blender,
revises it, measures success and freezes it for independent canonical replay. It checks
both forbidden unobserved revision and legal measured repair, not just a happy path.
The existing fault fixture also proposes an unobserved rewrite despite the narrowed
grants and checks that Flynn refuses it without replacing the scratch candidate or
earning completion. Existing lifecycle and dependent composition cases remain in scope.

## Follow-up exposed by the first retry

The phase-only retry at `/tmp/vfx-live-flynn-dag-sp6iwgb5` used five model calls,
18,309 prompt tokens and 545 completion tokens. It did probe after writing, but
Blender rejected a capability-escaping script. The existing verifier logged the
failure without filling per-frame verdicts. Flynn transported only the failed status,
so the model never saw the concrete execution error. A revision was then permitted
with insufficient external budget for its required probe. No unit completed.

The final mechanism also forwards the existing `on_replay_failed` stage/message
through the observation, and checks the entire remaining action path before offering
a grant. Write needs four inference/tool slots and three external slots; probe needs
three and two; freeze needs two and one. Initial inspection needs five and four.
These include the separate scripted canonical invocation, not an extra paid call.
The callback starts by checking exact attempt authority before reading candidate bytes
or entering verification. No verifier implementation or SDK contract changes.

The first regression run passed 46 cases but the revocation case was intercepted by
the newly narrowed SDK grant check before its intended VFX guard. The fixture now
proposes a currently granted probe after revocation. Its inert `object()` session also
exposed the need to check replay authority at callback entry, before session lookup.
The regression still requires the same stale-attempt exception and preserved scratch
bytes. It does not accept an arbitrary exception.

## Final validation

All **58 focused tests passed** on the final unchanged production source in 205.30
seconds: lifecycle and measured repair, dependent composition/failure preservation,
confined replay, phase-budget boundaries, script publication and builder fences.
Complete-source Ruff, strict preflight and `git diff --check` passed. The earlier
failed runs above are not counted as passing. The full suite was not repeated for
this change within one production module, with no import or shared signature changes;
the preceding DAG integration commit passed all 2,914 tests.

The final live retry at `/tmp/vfx-live-flynn-dag-5_agd8cl/probe-summary.json` allowed
six external actions per unit to fit one repair while retaining the six-model-call
per-unit cap. It was additionally capped at the 13 model calls remaining from this
turn's 18-call ceiling. It used 46,716 prompt tokens and 892 completion tokens.
Together with the phase-only retry, this turn used 18 model calls, 65,025 prompt
tokens and 1,437 completion tokens. No dollar settlement is claimed.

The independent unit inspected, wrote, probed a failure, revised from the delivered
error, probed success and froze; native canonical replay/completion passed. Its
source-verified completion receipt is
`636b7dc80e61311f7dddb42d5f92ba47cf4657d7d355736e6246809e48249b76`.
The producer completed in four model calls, with source-verified receipt
`9557852d31a14266a78deb942843094496a5e6f154bd53c2897dc7722d0f7e4f`.
The consumer inspected, wrote and passed its probe. The local model-call cap then
raised before another provider request, leaving consumer=building and no composed
layer artifact. The SQLite inference reservation for that refused request does not
represent a paid model call. No resume or synthetic completion was attempted.

This proves live measured repair and two native completions. It does not prove a
completed live three-unit layer; that gate still needs a fresh bounded run. The offline
three-unit composition and preservation tests pass. The SDK is unchanged.
