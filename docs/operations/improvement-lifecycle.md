# Improvement Lifecycle

Use this lifecycle whenever a run exposes a pain point, a failure, or a possible harness
improvement. Its purpose is to turn local friction into a validated general mechanism without
allowing a single shot or model opinion to become architecture by accident.

## Decision authority

When evidence conflicts, use this order:

1. Safety, declared invariants, and explicit human constraints.
2. Reproducible executable contracts, semantic facts, and empty-scene replay.
3. Qualified evaluation results across representative fixtures.
4. Qualitative critic evidence within its declared claim scope.
5. Model proposals and implementation convenience.

A lower authority may identify a problem or propose a change, but it cannot overrule a higher
authority. Missing or contradictory blocking evidence fails closed and routes to escalation.

## Lifecycle

### 1. Capture the observation

Record the run ID, expected and actual behavior, affected work unit or boundary, impact, minimal
reproduction, environment/configuration, and links to immutable artifacts. Preserve the failing
case before changing code or prompts.

### 2. Reproduce and classify

Reproduce at the narrowest reliable boundary and classify the earliest cause:

- contract or state-model gap;
- orchestration, ownership, or dependency error;
- mutation or Blender adapter defect;
- evidence, judge, or acceptance error;
- recovery, checkpoint, or replay failure;
- observability or configuration gap;
- shot-specific content issue rather than a harness defect;
- unknown, requiring a research note or probe.

### 3. Choose the decision record

- Create or update an **HIR** for a harness failure, causal mechanism, and validation.
- Add an **ADR** when the improvement changes durable architecture, authority, contracts,
  ownership boundaries, or compatibility policy.
- Use **research** for an unresolved hypothesis or disposable probe. Promote its conclusion into
  an HIR/ADR before it becomes production authority.
- Add a **changelog** entry only when an accepted outcome ships. Link its HIR and ADR rather than
  repeating the reasoning.

Small internal maintenance that changes no behavior may rely on a focused test and commit/PR
description. If it changes acceptance, repair, replay, or mutation behavior, it is not small.

### 4. Select a mechanism

Compare options against the pipeline north star and record rejected alternatives. Prefer the
smallest mechanism that fixes the cause across the failure class while keeping mutation scope,
ownership, checkpoints, rollback, and evidence explicit. Do not promote shot names, fixed frames,
display-name selectors, or prompt-only discipline into core behavior.

Escalate before implementation when authority conflicts, the root cause is unsupported, the
change must cross an undeclared ownership boundary, or it requires weakening an invariant.

### 5. Implement as a bounded change

Define the owner, allowed files/state, compatibility effect, rollback path, and acceptance
evidence. Keep the change separable from unrelated cleanup. Update contracts before adapters and
make enforcement executable wherever possible.

### 6. Validate proportionally to risk

Validation must show both directions:

- the preserved failing case now passes for the intended reason; and
- previously accepted behavior remains accepted.

Use the narrowest unit/contract checks first, then integration and cumulative replay. Changes
claiming generality need heterogeneous fixtures or held-out cases. Record commands, results,
artifact hashes/paths, cost and latency changes, and any untested risk in the HIR.

### 7. Accept, release, and observe

Mark the HIR/ADR accepted only after its required evidence passes. Update the changelog and
version according to compatibility and user-visible impact. Preserve the rollback boundary and
watch subsequent runs for the same failure class, near-neighbor regressions, cost growth, or new
repair loops.

If the mechanism fails later, reopen or supersede the record; do not erase the earlier reasoning.

## Completion test

An improvement is complete only when another developer or coding agent can answer from the
repository: what failed, why it failed, what mechanism changed, which alternatives were rejected,
what evidence accepted it, which version introduced it, and what would trigger reconsideration.
