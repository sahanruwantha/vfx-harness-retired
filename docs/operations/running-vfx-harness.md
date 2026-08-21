# Running VFX Harness

This is the canonical operating procedure for developers and coding agents. Run commands from
the repository root and use the public `.venv/bin/vfx` CLI. Every command that produces output
owns a structured run under the shot; do not invoke implementation modules to invent a different
workflow.

## 1. Establish the shot and environment

A shot lives at `shots/<shot-id>/` and requires:

```text
brief.md                 authored specification with frames/fps frontmatter
refs/                    authored visual references
```

Set credentials and model/runtime configuration in the repository `.env` or real environment.
Confirm Python dependencies and Blender before spending model budget:

```bash
.venv/bin/vfx preflight --strict
.venv/bin/ruff check src tests
.venv/bin/python -m pytest -q
```

Stop if preflight fails. Authentication, Blender, or configuration failures can resemble an empty
successful agent session and must not be diagnosed as a VFX-quality problem.

## 2. Create and gate the global plan

```bash
.venv/bin/vfx plan shots/<shot-id> --until-clean
.venv/bin/vfx evals plan shots/<shot-id>
```

Planning writes current authority to the shot root (`plans/`, `layers.json`, `acceptance.json`,
`critic_axes.json`, `checks.json`, and `scene_checks.json`) and puts generated planning evidence
inside its structured run. `--until-clean` exits 3 and records the run as failed if the gate
stalls or exhausts its repair budget with blocking findings; the dirty plan remains on disk as
diagnostic evidence. Planning status and summary records carry `outcome`, `blocking_count`, and
`plan_gate_report`; read `reports/plan_gate.json` for the reusable finding set without rerunning
the gate. Do not build while the deterministic plan gate reports blocking findings.

New acceptance fingerprints are typed `{metric_set, values}` records using
`vfx-harness.look-vector/v1`. Legacy prose remains readable, but a new plan must copy canonical
metric ids and values returned by `measure_ref`.

If planning raises client questions, inspect and answer them before the affected layer:

```bash
.venv/bin/python -m vfx_harness.orchestration.escalate shots/<shot-id>
.venv/bin/python -m vfx_harness.orchestration.escalate \
  shots/<shot-id> --answer <id> "<decision>"
```

## 3. Run the production chain

The normal operation is the whole driver:

```bash
.venv/bin/vfx run shots/<shot-id> --rounds 2
```

It performs just-in-time layer planning, the deterministic plan gate, bounded layer building,
cumulative acceptance, final rendering, and queued distillation under one run ID. It stops on the
first unaccepted boundary; do not force downstream work past it.

Useful bounded operations:

```bash
# Preview which layers would execute without calling models or Blender.
.venv/bin/vfx run shots/<shot-id> --from 1 --upto 3 --dry-run \
  --skip-accept --skip-render

# Re-run from the first invalid layer after fixing its cause.
.venv/bin/vfx run shots/<shot-id> --from <layer-id>

# Direct stages are supported and still create structured runs.
.venv/bin/vfx build shots/<shot-id> --layer <layer-id>
.venv/bin/vfx accept shots/<shot-id>
.venv/bin/vfx render shots/<shot-id>
```

`--force` is for a bounded debugging experiment only. Its results do not prove that an incomplete
or unaccepted chain is a deliverable.

## 4. Read output in the supported order

Never begin by recursively listing the shot or grepping every transcript.

```bash
.venv/bin/vfx inspect shots/<shot-id> --list-runs
.venv/bin/vfx inspect shots/<shot-id> --run <run-id> --json
```

For the latest run, read:

1. `runs/latest.json` — selected run ID and terminal state.
2. `runs/<run-id>/manifest.json` — schema, invocation, layout, and authority.
3. `runs/<run-id>/status.json` — `running`, `passed`, `failed`, `interrupted`, or `dry-run`.
4. `runs/<run-id>/reports/summary.json` — decisions, findings, cost, and trajectory.
5. `runs/<run-id>/artifacts.json` — exact catalog for locating supporting detail.

Then open only the necessary category:

- `reports/layers/` for a layer verdict and aggregated telemetry;
- `reports/plan_gate.json` for the final structured plan outcome and repair findings;
- `evidence/renders/` and `evidence/comparisons/` for visual proof;
- `logs/transcripts/` for prompts, tool calls, model output, and errors;
- `logs/console.log` for the chronological operator narrative;
- `checkpoints/` for resume/rollback material;
- `deliverables/` for published video or other final media;
- `scratch/` only for low-level debugging, never as accepted evidence by proximity.

## 5. Diagnose a failed or interrupted run

Use the status and summary before deciding what to change:

```text
preflight/config failure  -> correct environment; do not edit VFX logic
plan gate failure         -> repair plan/contracts; rerun the gate
builder evidence failure  -> inspect the owning layer report and cited evidence
canonical replay failure  -> repair deterministic script/checkpoint mechanism
acceptance failure        -> route to the declared fault-owning layer
process interruption      -> inspect the last checkpoint, journal, and final transcript events
```

For one layer's action timeline:

```bash
.venv/bin/vfx inspect shots/<shot-id> --run <run-id> --layer <layer-id>
```

Resume a truncated direct builder session only when its ledger resume record names an existing
checkpoint and journal:

```bash
.venv/bin/vfx build shots/<shot-id> --layer <layer-id> --resume
```

Otherwise start a new run from the fault-owning layer. Never copy a random old render, snapshot,
or script into the current run and call that a resume.

## 6. Authority and editing rules

- Edit `brief.md` and `refs/` only to change authored intent.
- Edit plans/contracts through planning, amendment, or an explicit reviewed repair.
- Treat `build/` and `shot.json` as the current accepted deterministic chain and ledger.
- Treat `state/` as durable cross-run operational state.
- Treat `runs/` as generated audit evidence. Do not hand-edit a run to make it pass.
- Ignore shot-root `logs/`, `renders/`, `.artifacts/`, `.snapshots/`, and `.versions/`; they are
  unsupported and have no decision authority.

After changing harness behavior, follow `improvement-lifecycle.md`: reproduce the cause, update an
HIR/ADR as needed, add regression evidence, run the full checks, and record the user-visible result
in the changelog.
