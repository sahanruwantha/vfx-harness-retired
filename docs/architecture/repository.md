# Repository architecture

VFX Harness is organized by decision responsibility. A coding agent should be able to
locate the authority for a change without reconstructing the system from a flat module list.

```text
vfx-harness/
├── AGENTS.md                  Codex routing, invariants, and verification
├── CHANGELOG.md               release-facing change summary
├── src/vfx_harness/
│   ├── cli/                   command dispatch only
│   ├── application/           plan/build/accept/render/inspect use cases
│   ├── domain/                shot, contract, and work-unit language
│   ├── orchestration/         state, ledger, revalidation, and escalation
│   ├── agents/                model roles, prompts, hooks, and context
│   ├── blender/               Blender process and scene-tool boundary
│   ├── evidence/              runtime measurements and claim reconciliation
│   ├── evaluation/            offline and qualification evaluation harness
│   ├── assets/                external asset providers and normalization
│   ├── knowledge/             packaged recipes and their verification
│   ├── observability/         run IDs, logs, transcripts, cost, and provenance
│   └── infrastructure/        configuration and sandbox adapters
├── docs/
│   ├── architecture/          current system truth
│   ├── decisions/             durable architecture decisions (ADRs)
│   ├── improvements/          failure-to-mechanism records (HIRs)
│   ├── operations/            run, debug, recovery, and release procedures
│   └── research/              provisional findings and tracked probes
├── evals/                     tracked suites, fixtures, graders, and public baselines
├── tests/                     unit, contract, architecture, and integration guarantees
├── tools/                     repository development utilities
├── examples/                  redistributable example shots
├── shots/                     ignored local production workspace
└── artifacts/                 ignored generated renders, transcripts, and eval runs
```

## Dependency direction

Dependencies point toward stable concepts:

```text
cli -> application -> domain
agents -----------^       orchestration -> domain
blender ----------^       evidence ------> domain
assets -----------^       observability and infrastructure are adapters
```

- `domain` does not import agents, Blender, providers, evaluation suites, or generated data.
- `application` assembles use cases; it does not implement low-level Blender or SDK behavior.
- `agents`, `blender`, `assets`, `evidence`, `observability`, and `infrastructure` expose
  capabilities to application and orchestration code.
- Runtime code never imports `evals/`, `tests/`, `shots/`, `artifacts/`, or `docs/research/`.
- Offline evaluation may inspect runtime code; runtime acceptance does not depend on offline fixtures.

Large concerns live as **subpackages under these same layers**, not as a second architecture.
`domain/work_units/`, `evidence/scene_checks/`, `blender/tools/`, `evaluation/plan_gate/`,
`orchestration/jit_materialization/`, and `agents/{plan_tools,planner,builder}/` keep the
public import path of the former module (`from vfx_harness.domain.work_units import WorkUnit`).
Internals import sibling modules, never the package `__init__`. `blender/worker.py` stays a
single file: Blender launches it by path, helper inventory AST-parses `_HELPERS` from that
file, and revalidation hashes it.

Architecture tests should enforce these directions as the modules become less coupled. The
folder move establishes ownership; it does not claim that all historical coupling has already
been removed.

## Leftover trees that stay

These are not dead code and are not part of a layout cleanup:

- `docs/research/probes/` — owned research and verification captures.
- `src/vfx_harness/knowledge/recipes/_spikes/` — recipe verification spikes.
- `src/vfx_harness/evaluation/ownership_*` — research adapters, not wired into the plan gate.
- `examples/` and `tools/` — reserved slots (`examples/README.md`, `tools/README.md`).

## Information authority

- `AGENTS.md` is the single binding coding-agent instruction source. Do not mirror
  fragments under editor-specific rule directories; duplicated rules drift and create
  ambiguous authority.
- `docs/architecture/` describes the current design.
- `docs/decisions/` explains durable choices and rejected alternatives.
- `docs/improvements/` records observed failure, root cause, general mechanism, and proof.
- `docs/research/` contains findings that have not automatically earned runtime authority.
- `evals/` contains reusable definitions; `artifacts/evaluations/` contains generated outcomes.

Generated artifacts are evidence, not instructions. Codex must not infer current behavior from
an old shot transcript or probe when an architecture document, HIR, contract, or test exists.

## Naming conventions

- Distribution and repository: `vfx-harness`.
- Python package: `vfx_harness`.
- Public commands: `vfx <verb>` and `vfx-<verb>`.
- Environment variables: `VFXH_*`.
- Python identifiers use standard `snake_case`, `PascalCase`, and `UPPER_SNAKE_CASE` conventions.

## Configuration boundary

Importing `vfx_harness` does not load files or mutate the environment. Entry points call
`vfx_harness.infrastructure.config.load_environment()`. Configuration resolves from an explicit
path, then `VFXH_ENV_FILE`, then the repository `.env`; existing process values always win.
Secrets are not copied into typed diagnostics or run reports.
