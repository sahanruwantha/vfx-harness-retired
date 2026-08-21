# VFX Harness agent guide

VFX Harness is an agent-driven Blender production runtime. Treat the repository as a
control system: model judgment proposes work, while typed contracts, deterministic evidence,
checkpoints, and replay decide whether work is accepted.

## Start here

- Product and commands: `README.md`
- Current architecture: `docs/architecture/`
- Durable decisions: `docs/decisions/`
- Failure-to-mechanism history: `docs/improvements/`
- Operational procedures: `docs/operations/`
- Provisional experiments: `docs/research/`

Read the relevant ADRs, HIRs, and tests before changing core harness behavior. Do not infer
current authority from old probes, generated shot output, or transcript history.

## Operating the project

Before planning, running, resuming, or diagnosing a shot, follow
`docs/operations/running-vfx-harness.md`. Use the public `.venv/bin/vfx` commands from the
repository root; do not improvise internal module commands when the CLI owns the operation.

After any invocation, read `shots/<shot>/runs/latest.json` and then the selected run's files in
this order: `manifest.json`, `status.json`, `reports/summary.json`, `artifacts.json`. Open detailed
reports, evidence, or transcripts only after that. Shot-root legacy output directories are
unsupported and must not be used as evidence or as a write destination.

## Pipeline north star

Before changing core runtime behavior, read
`docs/architecture/pipeline-end-goal.md`.

Complexity must scale by adding bounded, dependency-ordered work units, not by enlarging prompts
or agent sessions. Every unit must be checkpointed, locally validated, repairable, and proven
through cumulative empty-scene replay.

## Repository boundaries

- `src/vfx_harness/domain/`: pure contracts and state; no Claude SDK, Blender, network, or filesystem adapters.
- `src/vfx_harness/application/`: user-facing use cases.
- `src/vfx_harness/orchestration/`: scheduling, repair, rollback, checkpoints, and revalidation.
- `src/vfx_harness/agents/`: model roles, prompts, context, and tool policy.
- `src/vfx_harness/blender/`: the only direct Blender process/API boundary.
- `src/vfx_harness/evidence/`: deterministic and qualitative decision evidence.
- `src/vfx_harness/observability/`: run IDs, logs, transcripts, cost, and provenance.
- `src/vfx_harness/infrastructure/`: configuration, sandbox, and external runtime adapters.
- `src/vfx_harness/knowledge/`: packaged VFX recipes and their verification.
- `evals/`: tracked suites and fixtures; `artifacts/`: generated evidence; `shots/`: local production work.

Generated shot output must use `observability/run_artifacts.py`. A coding agent should read
`shots/<shot>/runs/latest.json`, then that run's `manifest.json`, `status.json`,
`reports/summary.json`, and `artifacts.json`; do not infer run ownership by scanning mixed
shot-root logs or filenames. Durable cross-run operational state belongs under `state/`, not in
`runs/`.

Dependencies point inward toward `domain`; runtime code must never import offline evaluation
fixtures or generated artifacts.

## Non-negotiable invariants

- Nothing self-certifies. A model verdict cannot replace authoritative executable evidence.
- Parallelize evidence production; serialize authoritative scene mutation and integration.
- Empty-scene replay is the source of truth for a published build artifact.
- Builders and repairs may mutate only declared semantic roles, controls, and script spans.
- Fail closed on stale, ambiguous, incomplete, or schema-incompatible authority.
- Core code must not contain shot names, display-name selectors, fixed shot frames, or scene-specific coordinates.
- A current shot is a fixture, not a template. Generalization requires heterogeneous fixtures.

## Change protocol

Follow `docs/operations/improvement-lifecycle.md` when a pain point, failure, or improvement is
found. In short: capture evidence, reproduce, classify the cause, choose the owning record,
implement the smallest general mechanism, prove the failure now passes without regressions, and
record the outcome. Do not fix a harness defect with prompt wording alone when it can be enforced.

- Use an HIR for a failure-to-mechanism improvement and its validation.
- Use an ADR when the choice changes durable architecture, authority, contracts, or boundaries.
- Use a research note while the cause or mechanism remains uncertain.
- Use the changelog only for accepted, user-visible outcomes; link the HIR/ADR for reasoning.
- If evidence is missing, authority conflicts, or scope must widen, stop and escalate rather than
  silently guessing.

Preserve user changes in a dirty worktree. Generated data belongs under `shots/` or
`artifacts/` and must not be imported as source.

## Verification

Run from the repository root:

```bash
.venv/bin/ruff check src tests
.venv/bin/python -m pytest -q
.venv/bin/vfx --help
```

Report which checks ran and any checks that could not run.
