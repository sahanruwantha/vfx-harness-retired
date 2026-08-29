---
id: HIR-0067
title: Exact recipe retrieval must be sectioned
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: exact_recipe_context_spike
mechanism: indexed_recipe_sections
adr: null
---

# Exact recipe retrieval must be sectioned

## Observed failure

Layer-2 `lighting_atmosphere` sessions stopped rereading the full brief/contracts after HIR-0054
and stopped inlining fuzzy recipe hits after HIR-0062, but still loaded exact recipe bodies of
6.6K (`volumetric-god-rays`), 10K (`blender-5-api`), and 4–5K additional recipes before their
first mutation. These payloads repeatedly preceded 60–180 second model-only pauses. The builder
usually needed one compositor socket or one volumetric setup fragment, not the whole cookbook
entry.

## Root cause

Recipe retrieval had only two granularities: one-line fuzzy summaries or the entire exact body.
An exact name proved identity, not that every prose and code section was relevant to the active
decision.

## General mechanism

An exact recipe name now returns a stable index of alternating prose and fenced-code sections,
including id, character count, and a short title. `<name>#<section>` loads one fragment.
`<name>#full` remains available only when several indexed sections are genuinely required.
Telemetry records use only when a section or explicit full body is loaded, not when an index is
viewed. Existing recipe files need no new authoring schema because sections are derived
deterministically from their prose/code boundaries.

## Rejected patch-level alternatives

Truncating bodies at an arbitrary character count can cut code mid-expression and hides what was
omitted. Hard-coding special sections for the two large recipes would not generalize. Removing
exact retrieval would force the model back to hand-rolled Blender API guesses.

## Validation

`test_recipe_retrieval_requires_section_or_explicit_full_body` pins index, section, and explicit
full modes. `test_recipe_sections_are_stable_across_prose_and_code_blocks` pins deterministic ids.
On the current cookbook, exact `volumetric-god-rays` falls from 6.6K to 1.5K characters and
`blender-5-api` from 10K to 2.2K before a section is selected.

## Release and rollback

No persisted schema migration. Callers that genuinely require a full body append `#full`.
Rollback only if section boundaries cannot express a verified recipe, in which case add explicit
frontmatter sections rather than restoring unconditional full-body injection.

## Remaining limitations

Prose between code fences is one section even when long. Frequently selected sections should
still graduate into typed helpers so the model no longer needs to adapt them at all.
