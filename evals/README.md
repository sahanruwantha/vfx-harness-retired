# Evaluation packages

Tracked evaluation inputs are split by authority:

- `fixtures/` contains redistributable authored inputs and deterministic generators.
- `suites/` contains closed expected invariants and execution policy.
- `graders/` stages fresh ignored workspaces and grades public typed artifacts.

Generated workspaces and result summaries belong under `artifacts/evaluations/`; they are
local evidence and are never imported as source.

## HIR-0170/HIR-0171 two-layer seal

From the repository root, create a new trial (the stager refuses to overwrite one):

```bash
.venv/bin/python evals/graders/judgment_debt_seal_v1.py stage --trial-id trial-001 --blender blender
```

The stage receipt binds the fixture and suite digests, generator digest, exact Blender
executable/version, pinned Blender 5.2 EEVEE settings, and generated reference PNG digest. Print
the safe public execution sequence with:

```bash
.venv/bin/python evals/graders/judgment_debt_seal_v1.py commands --trial-id trial-001
```

That sequence performs strict preflight, clean global planning, the plan evaluation gate,
pre-run authority capture, a two-round normal `vfx run`, and offline grading. Do not add
`--force`, partial-run switches, or skip acceptance/render. The grader reads selected plan/JIT
authority, coordinator lineage, typed layer publications, debt state, acceptance, artifact
inventory, and the final-render snapshot; it does not parse transcripts or filenames.
