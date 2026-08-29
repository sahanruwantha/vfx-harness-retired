---
id: HIR-0091
title: Unit-plan output is a fixed sink, not a model-selected path
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: repeated_unit_plan_path_guess
mechanism: harness_bound_unit_plan_publish_tool
adr: ADR-0006
---

# Unit-plan output is a fixed sink, not a model-selected path

## Observed failure

During both Layer-1 JIT sessions in runs `20260829T052107Z-93d1d9` and
`20260829T053222Z-b96457`, the planner tried to write the declared relative unit-plan
path under the repository root instead of the shot root. The write guard correctly
denied it. The planner then repeated the same call with the correct shot path, costing a
model/tool round and roughly 18–22 seconds each time.

## Root cause

The harness already knew the one legal target through `work_unit_plan_path`, but granted
the model the generic `Write` tool and asked it to reconstruct that target from a relative
string plus its working directory. The guard prevented corruption but left an irrelevant
path-selection decision in the model loop.

## General mechanism

JIT unit planning now receives `publish_unit_plan`, whose only argument is the complete
Markdown content. The harness binds the target before the session, verifies it is under
the active shot's `plans/` directory, enforces the existing 200-character and 160-line
bounds, and publishes atomically. Generic `Write` is denied in this phase. Global
planning and layer materialization keep their distinct typed write transactions.

This removes the invalid state rather than teaching one filesystem spelling: the model
cannot select a repository-root, sibling-shot, or arbitrary output path because no path
field exists in its tool schema.

## Validation

Registration fixtures prove the sink exists only when a harness target is bound and is
absent from unrelated plan phases. Heterogeneous camera and motion target fixtures prove
atomic publication under the shot while an injected escape target is refused. The full
repository suite passes 529 tests. The next production JIT unit must also prove that
publication earns the existing gate attestation while avoiding the denied first write.

## Release and rollback

This changes only the JIT unit-plan tool surface. Existing unit-plan files and authority
sidecars are unchanged. Rollback would restore generic path choice and its recurring
denied write, so it is unsafe.
