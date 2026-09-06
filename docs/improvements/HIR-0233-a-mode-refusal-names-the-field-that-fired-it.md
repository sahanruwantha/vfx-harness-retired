---
id: HIR-0233
title: A mode refusal names the field that fired it
status: accepted
introduced_in: unreleased
date: 2026-09-06
failure_class: a_refusal_named_a_rule_the_payload_appeared_to_satisfy_and_was_retried_identically
mechanism: the_refusal_lists_every_offending_field_with_its_value_and_both_legal_modes
adr: null
---

# A mode refusal names the field that fired it

## Observed failure

`hansa_silk_road` layer 2 staged a measurement-only unit:

```json
{"mode": "none", "role_members": [], "controls": [], "control_roles": {},
 "script_spans": ["build/units/02/hero_placement.py"]}
```

and was refused, in full:

```
unit staging refused: staged unit.mutates mode 'none' cannot declare mutation targets
```

Every field the materializer thought of as a mutation target is empty in that payload.
Reading the refusal against its own request, the only available conclusion is *"but I
declared none"* -- so it **retried the identical shape on the next unit**, and was refused
identically. Two units, two turns of a bounded budget, no information transferred.

`script_spans` is what fired: `if mode == "none" and any((roles, controls, spans,
dresses))`. A script span is a mutation target because it is a file the unit writes, and
nothing in the message said so, named the field, or showed the value.

This is the `control_roles` signature (HIR-0217) one field over: **a form that cannot be
accepted, and no information about what would be.** There the refusal named what was
wrong and never what would have been right; here it names neither.

## Root cause

The check tests four fields at once and reports the conjunction. That is correct as a
rule and useless as a message: the author cannot invert `any((roles, controls, spans,
dresses))` back to which element was truthy, and the three they were thinking about were
all empty. The message also assumes a shared definition of "mutation target" that the
materializer demonstrably did not hold -- it had a mental model in which roles and
controls are mutation and a script span is bookkeeping.

## Decision

Both mode refusals name the observed value and both legal next actions:

```
unit.mutates mode 'none' declares no mutation, but
script_spans=['build/units/02/hero_placement.py'] is set. Every one of roles, controls,
script_spans and dresses is a mutation target -- a script span is a file this unit
writes. Either drop it and keep mode 'none' for a unit that mutates nothing, or use mode
'scoped' and declare what it mutates.
```

- **Every** offending field is listed, not the first one found -- a one-at-a-time refusal
  costs a turn per field on a bounded budget, which is HIR-0201's argument one layer down.
- The sentence that would have saved the retry is the one explaining *why* a script span
  counts, not the list.
- The `scoped` refusal gets the same treatment: it said what was needed but not that all
  four were empty, and did not offer `none` as the shape for a unit that mutates nothing.

**The rule is unchanged.** Only what it says when it fires. Two tests pin that: a
genuinely empty `none` scope is still accepted, and a `scoped` scope carrying only a
script span is still accepted.

## Validation

`src/tests/unit/test_mutation_mode_refusal_names_the_field.py`, seven tests: the field
and its value appear; the message explains why a script span counts; both legal modes are
named; every offending field is listed rather than the first; the `scoped` refusal says
what was empty; and the two acceptance cases prove the rule did not move.

Reverting `src/vfx_harness` alone fails five of seven on the old text, not on an import:

```
E  assert 'script_spans=' in "unit.mutates mode 'none' cannot declare mutation targets"
E  AssertionError: roles= missing from unit.mutates mode 'none' cannot declare mutation targets
```

## What this does not fix

The materializer was reaching for a **measurement-only unit** -- it tried the same shape
three times across two independent sessions (`hero_frame_composition`, `hero_placement`,
`hero_composition`) and eventually invented a throwaway anchor host purely to own a write
cluster. Every unit must own one, so "a unit that only measures" has no schema shape.
This change makes the refusal legible; it does not answer whether that unit should exist.
That is a design question and it is recorded, not closed.
