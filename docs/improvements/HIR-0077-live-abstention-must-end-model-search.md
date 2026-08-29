---
id: HIR-0077
title: Live abstention must end model search before critique
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: ignored_live_abstention
mechanism: abstention_zeroes_live_round_budget
adr: null
---

# Live abstention must end model search before critique

## Observed failure

Run `20260827T234950Z-2d6f18` correctly recorded `cannot_express_in_scope` after the bounded
black-frame search closed. The live builder nevertheless continued for roughly five minutes:
it rendered through `compare_frame`, queried contracts, manipulated the worklist, ended the first
model response, paid a visual critique, and launched a revision session. The revision could not
mutate because the causal guard was closed, but it continued making blocked probes and
comparisons until the operator interrupted it.

## Root cause

The orchestration loop consumed `comparison_state.cannot_express` only at candidate freeze and in
canonical repair. Live build and live revision had no transition on that state. Separately, the
black-search fence covered `render_frame`, `render_pass`, and `probe_control`, but omitted other
rendering entry points such as `compare_frame`, `render_frames`, `verify_change`, and
`measure_regions`.

## General mechanism

An in-session typed abstention now reduces the remaining live critique/revision budget to zero
immediately after the current model response returns. If abstention is recorded during a revision,
the live-round loop breaks before another critic call. Finalization still compiles the accepted
mutation journal so the falsification has deterministic evidence, but image-debt abstention skips
canonical repair as before. The closed black-search state is also checked by every tool that can
render or probe new pixels; read-only inspection and consumption of an existing evidence handle
remain available.

## Rejected patch-level alternatives

Returning stronger prose from `cannot_express_in_scope` was already ineffective: the builder
continued after two explicit "do not edit" responses. Letting the critic repeat the same failure
does not add authority once the typed finding names the measured floor. Killing the whole process
inside the MCP tool would lose journal finalization and the durable falsification transaction.

## Validation

`test_typed_cannot_express_ends_live_critique_budget` pins the zero-round transition while
preserving the configured round count without abstention. The black-search stop-message regression
test pins the exact typed next action, and the producing retry must demonstrate that no visual
critic or live revision begins after the abstention event.

## Release and rollback

No persisted schema migration. Rollback restores redundant critic/revision spend after a typed
finding and can delay falsification publication until a model budget limit or operator interrupt.
