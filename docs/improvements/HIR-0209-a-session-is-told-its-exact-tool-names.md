---
id: HIR-0209
title: A session is told the exact deferred tool names the harness already passed it
status: accepted
introduced_in: unreleased
date: 2026-09-05
failure_class: a_session_guessed_names_the_harness_held_in_the_same_construction
mechanism: the_one_options_seam_states_the_qualified_mcp_tool_names_and_the_single_query_that_loads_them
adr: null
---

# A session is told the exact deferred tool names the harness already passed it

## Observed failure

caesar run `20260904T191549Z-e048a6`, rematerialization session, verbatim:

```
[ 7.9s] ToolSearch {"query": "select:evidence_vocabulary,stage_materialization_unit,..."}
[ 7.9s]   -> "No matching deferred tools found"
[11.4s] ToolSearch {"query": "select:mcp__plan__evidence_vocabulary,..."}
[11.5s]   -> 8 tool_references
```

Two calls and three and a half seconds to learn a prefix the harness had passed as
`allowed_tools` in the very construction that started the session.

It was first reported as a builder-only defect, with materialization sessions offered as
a clean control that resolved on the first try. The control did the same thing. The
reporting session withdrew its own A/B: four observations had split cleanly by luck, and
the defect is model variance on every session type rather than a property of one.

## Root cause

Deferred MCP tools arrive as names without schemas, so a session must resolve them through
`ToolSearch` before its first call. The exact qualified names are known before the session
starts — `build_plan_tools` constructs them as `mcp__<server>__<tool>` and returns them,
and every session passes them straight into `allowed_tools`. Nothing ever told the session.

So the harness held the answer and let the model derive it by rejection. That is the
instrument gap AGENTS.md names: the option space was knowable and enumerable, and was
neither enumerated nor presented, so a session guessed and paid for the guess. The cost is
small per session and it is paid by every session, on every run, forever.

Stating the names is enumeration, not prompt tuning. A prompt-level patch would be telling
the model to remember prefixes; this supplies the exact strings the harness already
computed, at the moment it computes them.

## Decision criteria

- Enumerate, don't imagine: a knowable option space is stated, never rediscovered.
- One owner: the names are stated where they are known, so no kickoff can omit them.
- The statement names the exact rejection it prevents, so a session that sees that string
  elsewhere knows what it means.
- Built-in tools are not deferred and are not listed; only `mcp__`-qualified names appear.
- The tool list itself is unchanged — this states it, it does not alter it.

## General mechanism

1. `agents/tool_manifest.tool_selection_card` renders the sorted qualified names and the
   single `ToolSearch` query that loads all of them, naming the exact
   `"No matching deferred tools found"` rejection a bare name produces. It returns empty
   for a session with no MCP tools.
2. `agents/sdk_options.sdk_options` — already the one constructor every session goes
   through, for the turn budget (HIR-0199) — prepends that card to `system_prompt`
   whenever `allowed_tools` carries qualified names. A session with no system prompt is
   left untouched.

## Rejected patch-level alternatives

- Adding the names to each kickoff separately: six sites, each able to drift or be
  forgotten, and new sessions start without it.
- Telling sessions in prose to use the full prefix: the same guess with encouragement.
- Resolving unambiguous bare names inside `ToolSearch`: not the harness's tool to change,
  and it would hide the mismatch rather than remove it.
- Accepting the cost as small: it is per session, on every run, and it is paid to
  rediscover something the caller already computed.

## Validation

- `src/tests/unit/test_deferred_tool_names_are_stated.py`: the card names every qualified
  tool in one query and omits built-ins; a session with no MCP tools gets no card; the card
  reaches the session through the single options seam with the original system prompt
  intact and the tool list unchanged; a session with no system prompt is left alone.

## Release and rollback

Additive text on the system prompt of sessions that already carry MCP tools. No behaviour,
schema, or authority change. Rollback restores the two wasted calls.

## Remaining limitations

This removes the guess; it does not verify the session then used the names. Nothing asserts
that a session's first `ToolSearch` succeeds, so a future regression would show up as
latency rather than as a failure. The measurement that found this — two calls where one was
needed — is not something the harness reports about itself.
