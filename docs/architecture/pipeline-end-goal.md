# Pipeline End Goal

The detailed target design, architectural decisions, judge orchestration, rollout plan, and
verification strategy are specified in
[`staged-pipeline.md`](staged-pipeline.md).

## North star

The pipeline must handle simple and complex VFX shots with the same method: decompose the
work into the smallest meaningful dependency-ordered units, complete them one by one, validate
each unit locally, and repeatedly prove that the growing scene still contains everything that
was previously accepted.

A shot used to develop the harness is a test fixture, not a template. Core behaviour must not
contain its subjects, frames, stage count, look, or terminology.

## Work hierarchy

```text
shot
  -> department layer (ownership boundary)
       -> planner-generated work-unit DAG
            -> bounded semantic mutation
            -> local validation and accepted checkpoint
       -> cumulative layer regression
  -> full-chain acceptance
```

A **layer** owns a department or coherent axis set. A **work unit** is the smallest useful
planning/build/repair transaction inside that layer. The planner chooses the number and shape
of work units from the shot; the harness supplies only the generic schema and state machine.

Create a new work unit when the derived write-cluster, semantic subject, evidence mode,
reference moment, dependency, repair owner, or regression risk changes. Each unit declares:

- one concrete goal and its dependencies;
- semantic roles/controls it may mutate;
- earlier contracts and interfaces it must protect;
- relevant reference moments and an explicit primary moment, each covered by a
  required claim;
- static, keyframe, or motion evidence policy;
- executable and qualitative completion conditions;
- checkpoint, repair owner, and rollback boundary.

Plans are generated just in time. A layer charter records ownership and the work-unit DAG; a
unit plan reads sealed dependency outcomes and contains only that unit's execution authority.

Execution may prove that the plan is wrong. Replanning is therefore a versioned transaction:
freeze accepted state, validate a proposed DAG/scope/ownership amendment, compute its complete
dependency invalidation closure, preserve unaffected checkpoints, mark replaced outcomes
`superseded`, mark terminally unsatisfied dependants `blocked`, and resume from the earliest
legally reopened unit. Builders and repairs never broaden their own scope or rewrite plans.

## Validation pyramid

Validation widens as work accumulates:

1. **Mutation:** the requested semantic state actually changed.
2. **Work unit:** focused contracts and reference evidence prove the unit's goal.
3. **Interface regression:** persistent upstream semantic interfaces still hold.
4. **Layer regression:** all accepted units in the current layer still work together.
5. **Cross-layer regression:** the new layer preserves earlier visual and structural claims.
6. **Shot acceptance:** an empty-scene replay of the full chain passes every approval moment.

After a small edit, run its local checks plus a risk-selected upstream subset. Run the broad
suite at accepted checkpoints and final boundaries. A passing current render is not enough;
the deterministic artifact and its replay are the deliverable.

## Parallelization boundary

Freeze the script, settings, dependencies, and candidate hash before parallel evaluation.
Independent workers may produce reference measurements, isolated renders, render passes,
focus crops, deterministic checks, and critic opinions. A fan-in coordinator publishes one
ordered verdict after all required evidence is complete.

Authoritative scene/script mutation, integration, accept, rollback, and shared-control repair
remain single-writer operations. Render concurrency is resource-aware: one GPU may require a
serial render queue while CPU evidence and model judgements still run concurrently.

The governing rule is: **parallelize evidence production; serialize authoritative mutation
and integration.**

## Evidence and judgement

Use generic primitives such as semantic scene facts, region relationships, value ordering,
adjacent-region separation, gradients, edge rolloff, geometry state, material assignment,
keyframe behaviour, and controlled render passes. Shot plans bind these primitives to subjects,
frames, regions, references, and targets.

Measurable claims must be decided by executable evidence. Critics judge qualitative residuals
that cannot be reduced honestly to a stable contract. Critic observations never override a
passing authoritative measurement of the same fact, and no repair is justified by unsupported
measurement prose.

Blocking authority is earned. A qualitative claim may block autonomously only when its exact
claim type, judge model, prompt, and evidence shape pass a declared qualification budget. An
important unqualified claim becomes executable evidence, is requalified, or routes to audited
human adjudication; it is not silently made optional. Interaction claims use a bounded
coordination owner over enumerated shared controls, and broader balancing triggers replanning.

## Checkpoints, repair, and recovery

Every accepted unit records its cumulative script hash, input manifest, render-settings hash,
checks, verdicts, relevant renders, protected outcomes, and cost/time telemetry. These artifacts
are durable memory across agents, restarts, and context compaction.

Repair receives a machine-authored manifest containing the failure, protected passes, allowed
semantic mutation scope, relevant script spans, locked settings, and rejected deltas. A repair
is accepted only after failing and protected evidence is re-evaluated. Regression, truncation,
cancellation, process failure, or worker failure restores the last accepted checkpoint.

Faults route to their semantic owner. A downstream layer must not compensate for a broken
upstream interface, geometry, material, animation, or other sealed responsibility.

Mutation scope is enforced through semantic operations and before/after semantic facts. Blender's
derived dependency-graph changes use explicit tolerance classes; raw scene diffs remain audit
evidence rather than becoming a noisy sole gate. Protection wildcards resolve to explicit IDs at
candidate freeze and that immutable closure is stored in the checkpoint.

## Anti-overfitting rules

Core code must not contain shot names, object display names, fixed frame numbers, fixed layer or
stage counts, scene-specific coordinates, or department assumptions presented as universal.
There is no implicit primary frame or keyword-based evidence inference: plans declare both.

New mechanisms must be tested on heterogeneous fixtures such as static architecture, products,
characters, outdoor environments, transparent/volumetric subjects, camera-driven shots, and
multi-beat animation. Inject failures including missing roles, broken materials, flat lighting,
bad holds, upstream regressions, interrupted edits, failed workers, contradictory critics, and
out-of-order results.

A feature belongs in the core only when it solves the same class of problem across these
different fixtures. Shot-specific knowledge stays in shot plans, references, and contracts.

## Definition of success

The end goal is reached when complex shots scale by adding planner-generated work units rather
than enlarging one prompt or agent session; every unit is bounded, checkpointed, and repairable;
parallel evaluation is deterministic and resource-safe; an empty-scene replay remains the source
of truth; and generalization is demonstrated on held-out scenes and injected failures rather
than inferred from success on the development shot.
