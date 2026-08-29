---
id: HIR-0097
title: The unit-ticket schema is an agent instrument
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: materializer_guesses_work_unit_schema
mechanism: closed_work_unit_tool_input_schema
adr: null
---

# The unit-ticket schema is an agent instrument

## Observed failure

Run `20260829T071417Z-25d50c` spent 244 seconds before its first staging call, then
submitted three units together. None could stage. It invented `consumes` fields named
`unit`, `control`, and `roles`; wrote prose into the `temporal_evidence` enum; and copied
an empty `composition_context` into units whose claims already bound their evidence.
Retries then removed one invalid composition field at a time. No scene work began.

## Root cause

The typed `WorkUnit` contract existed only behind the staging handler. The MCP tool
declared `unit` as an unconstrained object, and the kickoff's generic example omitted
publish/consume while presenting composition context as if it were universal. The agent
was forced to reconstruct a mechanical schema from parser errors.

## General mechanism

The domain now exports a closed JSON authoring schema derived from the same registries
used by `WorkUnit.parse`: temporal modes, claim/evidence domains, look capabilities,
provided capabilities, publish-interface kinds, mutation/protection shapes, and the
exact `{producer, interface_id, kind}` consume row. `composition_context` is optional;
when present its schema requires non-empty frames and exactly one source or contract-id
path. The staging MCP tool uses that schema directly.

The kickoff keeps only a minimal outer-document example, omits optional composition
context, and gives one exact typed predecessor handoff. The tool contract asks for one
staging call followed by its result before the next unit. Parser and cross-unit gates
remain authoritative after schema validation.

## Validation

Product-camera-target and motion-aim-control tickets validate against the exposed schema
and parse into the domain type. The three production mistakes—wrong consume keys, prose
temporal mode, and empty composition context—are rejected by the tool schema before the
staging handler. Existing materialization and planner tests remain the compatibility
boundary.

## Release and rollback

No persisted schema changes. This tightens only the agent-facing tool input and removes
misleading optional fields from the example. Rollback would restore parser-error-driven
schema discovery and is unsafe.
