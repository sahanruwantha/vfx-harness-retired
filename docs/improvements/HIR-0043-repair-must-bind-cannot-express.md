---
id: HIR-0043
title: Repair must bind cannot_express_in_scope
status: accepted
introduced_in: unreleased
date: 2026-08-27
failure_class: repair_abstention_was_not_on_the_session
mechanism: candidate_server_binds_cannot_express
adr: null
---

# Repair must bind cannot_express_in_scope

## Observed failure

Run `20260827T042342Z-a21ce1` canonical repair 1 on `atmosphere` ToolSearch'd
`cannot_express_in_scope` and got **no matching deferred tools**. The session then
abstained in prose, reverted density/emission edits, and reported the id in the
summary. The harness did not set `comparison_state["cannot_express"]`, re-scored
canonical f150 at 2.0, and burned repair 2/2 on the same inert knobs.

## Root cause

HIR-0031 registered `cannot_express_in_scope` on the **live blender** MCP server
and taught the repair prompt to call it. Canonical repair is `_run_script_agent`
with `_script_options`: candidate (`probe_candidate`) plus recipes. There is no
blender MCP. ToolSearch cannot find a tool that is not on the session, and prose
is not a tool result, so the repair loop still spent the remaining budget.
Classification: missing compiled instrument on the repair session (HIR-0031
remaining limitation).

## Decision criteria

- Repair `allowed_tools` includes `mcp__candidate__cannot_express_in_scope` when
  `probe_ctx` carries the live `comparison_state` dict the loop reads.
- Finalize does not bind that tool.
- `record_cannot_express` is the single writer used by live blender tools and
  the candidate server.
- The repair prompt names the bound candidate tool; it does not tell the model
  to ToolSearch blender.

## General mechanism

`probe_ctx["comparison_state"]` is the live phase dict. `_script_options` strips
it for finalize. The candidate server adds `cannot_express_in_scope` only when
that dict is present.

## Rejected patch-level alternatives

- Parse "cannot_express_in_scope" from the repair summary: conversation is not
  execution authority.
- Auto-skip remaining repairs whenever look is 2.0 and scene is 3/3: some look
  units have in-scope levers; abstention is a typed tool call.
- Mount the full blender MCP on repair: repair edits the script, not the warm
  scene.

## Validation

- `tests/unit/test_builder_instruments.py`:
  `test_repair_candidate_server_binds_cannot_express`,
  `test_record_cannot_express_requires_ids_and_reason`.
- `tests/integration/test_harness.py`: REPAIR_SCRIPT with `probe_ctx` lists the
  candidate tool; FINALIZE_SCRIPT does not.

## Release and rollback

No schema migration. Rollback is a repair session that only has
`probe_candidate` and recipes.

## Remaining limitations

The agent must still invoke the tool. Look axes with no bound optical contract
remain unsatisfiable until a later instrument or plan amendment (HIR-0044 keeps
live mutation open; it does not invent shafts). HIR-0045 stops an uncovered
unit judge frame from becoming that critic vote.
