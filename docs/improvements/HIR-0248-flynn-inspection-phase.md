---
id: HIR-0248
title: Flynn initial inspection cannot consume the candidate budget
status: accepted
introduced_in: unreleased
date: 2026-09-07
failure_class: repeated_unchanged_initial_inspection
mechanism: phase_derived_tool_grants
adr: ADR-0012
---

# Flynn initial inspection cannot consume the candidate budget

## Observation and ownership

The first live DeepSeek executable-unit probe inspected the same empty scene five
times and then abstained when only the canonical replay reservation remained.
`write_candidate` was granted on all five requests. Each selected observation contained
only the same object manifest and its digest; execution phase was absent. Most tools
also had empty descriptions. The model's final explanation that writing was unavailable
was true only for its last request, not for the preceding five.

This is VFX tool-policy and context ownership. Flynn correctly enforced each supplied
grant, retained the operations and budgets in SQLite, and recorded abstention without
accepting state. No SDK change is required.

The preserved failed probe is `/tmp/vfx-live-flynn-pk120nzo/probe-summary.json` with
its sibling `provider-traces.json` and run-owned SQLite database. It used six model
calls, 20,051 prompt tokens and 272 completion tokens, with no candidate or receipt.
The fixture seeds selected planning authority; it does not test model planning.

## Mechanism

Initial scene inspection is available at most once, and only before the first
candidate write. The scratch write does not update the worker scene, so offering
initial inspection afterward would expose the wrong scene. Candidate replay supplies
subsequent measured scene evidence. The required bounded context now includes explicit
inspection, candidate, observed-digest and frozen-digest phase fields. Tool schemas
describe their actual effects. Abstention and candidate revision remain available under
the existing budget policy; freezing still requires the exact observed bytes, followed
by independent canonical replay and existing VFX publication/receipt readers.

A longer prompt or more budget would leave the repeat dispatch legal. Accumulating
history would violate bounded context. Moving this VFX phase policy into Flynn would
confuse generic operation execution with domain scheduling. None is needed here.

## Validation

The existing real Blender lifecycle test now asserts that inspection disappears after
its first result while candidate work remains possible. Before the production edit,
it failed on the second inference request because `inspect_unit` was still granted.
The same test covers passing evidence, a failed count, an undeclared role and a replay
exception, preserving the existing canonical acceptance/failure checks.

The fresh live probe at `/tmp/vfx-live-flynn-3c8ug5t4/probe-summary.json` used
`deepseek-v4-flash-vision-exp` through the installed pinned SDK `f6b164e`:
inspect → write → probe → freeze, four model calls, 14,052 prompt tokens and
216 completion tokens. The separate scripted canonical replay made no model call.
Real confined Blender evidence passed at both fixture judge points, and the native
checkpoint/completion reader verified script SHA-256
`7cd3b627519fa320f19c9de2f124d872d279cd95af23d171e947f69fb5aa58a3`.
SQLite selected evaluation receipt digest
`5d9e20f0ee805a2ca5de9e103cea9e4e02b39013c85537e8f54db1f053712a47`.
This proves one executable unit with seeded planning authority, not dependent-layer,
visual, full-shot or heterogeneous generalization. No provider-neutral dollar settlement
is claimed. No SDK source or dependency pin changed.

The focused lifecycle, script-publication and builder-fence suites passed **40 tests**.
Ruff passed on the complete `src` tree and `git diff --check` passed. The full suite
was not rerun for this change confined to one existing production module, with no
import or cross-package changes.
