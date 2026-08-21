---
id: HIR-NNNN
title: Short failure-to-mechanism statement
status: proposed
introduced_in: unreleased
date: YYYY-MM-DD
failure_class: stable_machine_readable_class
mechanism: stable_machine_readable_mechanism
adr: null
---

# Short failure-to-mechanism statement

## Observed failure

State expected versus actual behavior, impact, reproduction steps, and links to run IDs or
artifacts. Preserve the failing evidence before implementing a change.

## Root cause

Explain the earliest harness mechanism that allowed the failure. Distinguish the cause from the
visible symptom and state what evidence supports the diagnosis.

## Decision criteria

List the constraints used to choose a mechanism: generality, evidence authority, deterministic
replay, bounded mutation, rollback, latency/cost, and compatibility as relevant.

## General mechanism

Describe the smallest reusable change and its ownership boundary. Link an ADR if durable
architecture, authority, contracts, or package boundaries change.

## Rejected patch-level alternatives

Record plausible fixes that were rejected and why they treat the symptom, weaken an invariant,
or fail to generalize.

## Validation

Record the previously failing regression, protected tests, heterogeneous fixtures, replay or
evaluation evidence, and exact commands/results. A proposed improvement is not accepted without
evidence appropriate to its risk.

## Release and rollback

State the target version, migration or compatibility effects, feature flag if any, and how to
restore the last accepted behavior.

## Remaining limitations

List known gaps, follow-up triggers, and claims that the evidence does not support.
