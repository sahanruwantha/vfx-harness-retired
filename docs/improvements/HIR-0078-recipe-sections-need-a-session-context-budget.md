---
id: HIR-0078
title: Recipe sections need a session context budget
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: sectioned_recipe_reconstruction
mechanism: per_session_recipe_body_budget
adr: null
---

# Recipe sections need a session context budget

## Observed failure

HIR-0067 replaced unconditional full-body recipe retrieval with section indexes, but run
`20260827T234950Z-2d6f18` reconstructed much of the same context before its first mutation. The
builder loaded four sections of `volumetric-god-rays` and two sections of `dark-void-staging`,
after opening three recipe indexes. The section calls injected more than eight thousand body
characters and preceded a 50-second model-only pause.

## Root cause

Sectioning bounded one tool result but not cumulative session context. Every individually legal
fragment was replayed into every later model request, so the model could rebuild a large recipe
body one slice at a time. The tool also allowed an exact section to be reread despite the result
already being present in the conversation.

## General mechanism

Each recipe MCP server now owns a session-local body budget. Fuzzy summaries and exact-name
indexes remain available for discovery, but at most three distinct body requests and 12,000 body
characters may be admitted. Repeating an already-loaded section is refused as no new authority;
a fourth body or character overflow directs the agent to act on typed measurements or name a
missing instrument. An explicit `#full` body consumes the same budget, so one genuinely necessary
full recipe remains possible but cannot be followed by incremental cookbook reconstruction.

## Rejected patch-level alternatives

Reducing every section's size would split coherent code and encourage more calls. Prompting the
model to choose fewer sections leaves cumulative context unbounded. Removing recipes entirely
would send hard Blender API work back to improvisation.

## Validation

`test_recipe_body_budget_refuses_rereads_and_a_fourth_fragment` pins identity and count limits.
`test_recipe_body_budget_also_bounds_total_replayed_characters` pins the independent character
ceiling.

## Release and rollback

No persisted schema migration. The budget resets with each bounded build or repair session.
Rollback restores unbounded cumulative recipe reconstruction.
