---
id: HIR-0252
title: Declared global client blockers must reach the gate
status: accepted
introduced_in: unreleased
date: 2026-09-07
failure_class: declared_client_blocker_ignored_by_gate
mechanism: deterministic_ownership_mapping_blocker_findings
adr: ADR-0012
---

# Declared global client blockers must reach the gate

## Observed failure

During the native global-planner cutover, the offline production-stage contract test
added one explicit client blocker to both the product still and motion ownership
mappings. Both stages returned 0, reported a clean terminal gate, and selected plan
authority. The expected result was exit 3 with a typed stop and no selected authority.
That focused run produced 2 failures and 28 passes. This was a controlled fixture
reproduction, not a live production shot or a claim about model quality.

The baseline implementation is commit `5d2ef81`. The regression remains in
`src/tests/contract/test_global_planner_cutover.py` as
`test_dirty_driver_stage_preserves_typed_stop_without_selecting_authority`.

## Root cause and decision

The deterministic compiler retained `blockers` in `plans/ownership_mapping.json` and
rendered them into the global plan. The gate never read that declared blocker list.
A clear-looking document and a clean gate therefore disagreed about whether work could
begin. The fix belongs to VFX's acceptance evidence, not SDK execution or prompt wording.

The contract gate now reads the compiler's preserved mapping artifact when present,
requires its current schema and explicit valid blocker list, and emits a plan-wide
blocking finding for every declared blocker. Unreadable or malformed records fail
closed. Consumer projections resolve their verified immutable global bundle before reading the
mapping; legitimate projection links do not become either authority or a reason to
weaken the direct-workspace reader. Older synthetic test mapping headers were migrated
to the current schema instead of adding a compatibility parser.

The native and terminal gates share this evaluator, so a diagnostic gate cannot
be fixed while the authority publisher still accepts the same blocked candidate.

## Rejected alternatives

Telling the model to remember its blockers would preserve the mechanical defect.
Rejecting every blocker at tool validation would prevent the harness from retaining the
blocked draft and explaining the owning transaction. Inferring blockers from prose
would create another inconsistent interpretation of a field already available as data.

## Validation and limits

Both previously failing production-stage cases now return 3, preserve the typed stop,
leave terminalization to the live driver, and select no plan authority. The complete
native planner/session test group passed 74 tests after the fix. Full-suite validation
is recorded with the production cutover in `docs/research/flynn-full-cutover.md`.

The gate enforces declared blockers; it does not prove that a model recognized every
ambiguity or that a proposed answer has human approval. No paid inference or full-shot
visual acceptance was performed. No compatibility fallback or feature flag was added.
Rollback is a code revert, not editing generated evidence to make a blocked plan pass.
