---
id: HIR-0062
title: Fuzzy recipe search must not inline full bodies
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: unbounded_fuzzy_recipe_context
mechanism: two_phase_recipe_discovery
adr: null
---

# Fuzzy recipe search must not inline full bodies

## Observed failure

In run `20260827T192500Z-abdc37`, `lighting_atmosphere` issued several overlapping fuzzy
queries for motion blur, god rays, atmospheric depth, and dark staging. Fuzzy motion-blur queries
ranked the roughly 10KB `blender-5-api` recipe first and injected its complete body repeatedly;
other top bodies added another 15KB. Tool results remained in every subsequent model call before
the first substantive lighting mutation.

## Root cause

`find_recipe` always returned the top hit's full body. Ranking confidence was treated as an exact
selection, although fuzzy discovery and exact retrieval are different operations. Telemetry also
counted every ranked hit as used even when only its one-line alternative was shown.

## Decision criteria

Discovery must remain cheap and in-scope, exact verified code must remain available on demand,
and recipe-use telemetry must mean a body was actually retrieved. No prompt-only limit can bound a
tool result the harness constructs.

## General mechanism

Fuzzy queries return ranked name/when summaries only. At introduction, a query whose normalized
text exactly named a recipe returned that verified body plus alternative one-liners. HIR-0067
subsequently section-indexed exact retrieval. Only section/body retrieval is recorded as use.

## Rejected patch-level alternatives

Truncating bodies can remove load-bearing code or gotchas. Asking the model to use exact names does
not stop fuzzy calls from returning large bodies. Lowering the number of ranked hits leaves the top
body unbounded.

## Validation

The successor `test_recipe_retrieval_requires_section_or_explicit_full_body` proves fuzzy and
exact-index calls exclude a synthetic large body while a selected section or explicit full query
retrieves it and records only that recipe as used. Focused Ruff and recipe tests pass.

## Release and rollback

No data migration. Revert the response formatter if the extra exact-name round trip causes a
measured regression larger than the saved replayed context.

## Remaining limitations

Exact recipe bodies are section-indexed by HIR-0067. Frequently selected sections should still
graduate into typed helpers when their behavior is mechanically stable.
