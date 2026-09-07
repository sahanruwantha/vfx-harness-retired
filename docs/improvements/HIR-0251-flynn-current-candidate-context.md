---
id: HIR-0251
title: Flynn repairs need their current candidate source
status: accepted
introduced_in: unreleased
date: 2026-09-07
failure_class: stateless_request_lost_the_candidate_needed_for_repair
mechanism: required_current_source_and_digest_under_existing_context_cap
adr: ADR-0012
---

# Flynn repairs need their current candidate source

## Observation

The fresh live DAG probe after HIR-0250 stopped at the independent unit after four
model calls. Blender refused `import bvfx`; the replay error reached the model, but
the model abstained because it could not see the candidate it needed to repair.
The adapter deliberately has no accumulated conversation history. VFX supplied the
current candidate digest and measured feedback, but omitted the actual current source.

The failed probe is `/tmp/vfx-live-flynn-dag-v9soavlq`, with its summary, provider
traces and SQLite operation records under that fixture root.
It used 14,125 prompt tokens and 328 completion tokens. No unit or layer completed.
The failure was missing repair context, not a reason to relax the import policy.

## Mechanism and ownership

After a candidate is written, VFX reopens the exact guarded scratch file and supplies
its current UTF-8 source and SHA-256 alongside the execution phase and selected
feedback. The existing required-context check includes that source under the same
12,000-character cap and runs before inference reservation. It never silently
truncates the source. An oversized candidate leaves diagnostic scratch bytes and
refuses the next inference; it earns no completion authority.

This context belongs to the active VFX unit. It contains no predecessor scripts,
previous candidate versions or accumulated tool history. Flynn remains responsible
for enforcing budgets and transporting the prepared request, not choosing which
VFX artifact is relevant. No SDK or shared signature changes are needed.

## Regression

The real failed-then-repaired candidate test now checks that every post-write model
request includes exactly the current source and its digest, including the replacement
after revision. Before the fix it failed on the first post-write request because the
candidate source was absent. Existing count-failure and script-error variants both
exercise this path. A separate oversized-source case checks refusal before a second
model invocation or reservation. The three-unit fixture continues to verify bounded
context and absence of sibling plans.

## Final validation

All **59 focused regression tests passed** on the final unchanged source in 219.91
seconds. Complete-source Ruff, strict preflight and `git diff --check` passed. The
full suite was not rerun for this single-module context change with no import or
shared signature changes; the preceding dependency integration passed 2,914 tests.

The fresh live retry at `/tmp/vfx-live-flynn-dag-ie3p4t10/probe-summary.json`
completed all three units and native composed layer finalization. It used **14 model
calls**, **52,555 prompt tokens** and **902 completion tokens**, within six calls per
unit. The independent unit needed one measured repair (six calls); producer and
consumer each needed four. Across both attempts this turn: **18 calls**, **66,680
prompt tokens**, **1,230 completion tokens**. No dollar settlement is claimed.

Native empty-scene composed replay measured all three bound scene contracts at both
fixture judge points and passed. An independent process reopened selected authority
and verified current layer publication with receipt digest
`ee232ba18f6bee00eaa5aad50c103c55690a35ae4267a452505967d7d9289caa`.
All three durable unit states are passed. The consumer's generated script reads the
accepted producer via `bpy.data.objects.get("producer")` and derives its location from
that object's location; it does not merely hardcode the expected result.

This closes the live executable dependent-layer gate with seeded planning authority.
It does not prove live planning, visual judgment, amendment-driven preservation,
full-shot acceptance or heterogeneous generalization. The CLI still uses the existing
engine by default. No SDK source or dependency pin changed.
