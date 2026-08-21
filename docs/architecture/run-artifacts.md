# Run Artifact Contract

Generated output is a public interface between the VFX runtime, evaluation tools, developers,
and coding agents. It must be discoverable from stable metadata rather than reconstructed from
filenames, timestamps, or a scan of the shot directory.

## Authority boundaries

```text
shots/<shot>/
  brief.md, refs/              authored inputs
  plans/current.json           atomically selected immutable plan generation
  plans/, *.json contracts     temporary authoring compatibility surface
  build/, shot.json            accepted build and milestone ledger
  state/                        durable operational state shared across runs
  runs/                         generated output, isolated by invocation
```

Run output cannot become planning or build authority merely because it is nearby. Promotion from
evidence into a contract, plan amendment, HIR, or ADR is an explicit decision.

## Canonical run layout

```text
runs/
  latest.json
  <run-id>/
    manifest.json
    status.json
    artifacts.json
    logs/
      console.log
      transcripts/<stage>/<label>.jsonl
      cost.jsonl
    reports/
      summary.json
      plan_gate.json
      layers/layer-<id>.json
    evidence/
      renders/
      comparisons/
    checkpoints/
      blender/
      scripts/
      journals/
      repairs/
    scratch/
      blender/
      plan-lab/
    deliverables/
```

`manifest.json` is the entrypoint and declares the schema, identity, invocation parameters,
layout, and authority relationships. `status.json` is the terminal state. `artifacts.json` is a
sorted catalog with path, category, size, and media type. `reports/summary.json` is the compact
semantic digest. `latest.json` is a small pointer, not a symlink, so it works across platforms and
copied evaluation fixtures.

For planning runs, `reports/plan_gate.json` is the terminal deterministic authority. Its outcome,
blocking count, and report path are repeated in `status.json` and the summary so readers can decide
whether to open the full finding set without rerunning the gate.

A clean untagged planning run also stores a content-addressed plan bundle under its
`checkpoints/plans/bundles/` directory and atomically updates `plans/current.json`. The pointer
names the producing run, bundle path, aggregate hash, and gate outcome. Bundle-aware readers verify
every member hash and fail closed on a malformed pointer. During the ADR-0004 migration, existing
build consumers still read the shot-root compatibility files; the pointer is the durable
publication record but does not yet make those legacy reads transactional.

## Reader protocol

1. Read `runs/latest.json`, or select a run with `vfx inspect --run <id>`.
2. Validate `manifest.json.schema`; fail closed on an unsupported schema.
3. Read `status.json` before interpreting partial output.
4. Read `reports/summary.json` for decisions and findings.
5. Use `artifacts.json` to locate detail; do not recursively scan or parse meaning from names.
6. Open transcripts, renders, or checkpoints only when the summary identifies a reason.

## Writer protocol

- Create the layout once in the invocation driver and propagate `VFXH_RUN_DIR` to subprocesses.
- Resolve paths through `vfx_harness.observability.run_artifacts`; do not add ad hoc
  `shot/logs`, `shot/renders`, or hidden-directory writes.
- Use stable semantic subdirectories. Filenames identify the artifact inside its category and do
  not repeat the shot ID, run ID, stage, and category already encoded by parent directories.
- Publish JSON atomically. Append-only JSONL is reserved for event streams and queues.
- Put resumable accepted state in `checkpoints/`; put disposable intermediary files in `scratch/`.
- Put immutable plan bundles and repair-input snapshots under `checkpoints/plans/`; never write
  shot-global `plans/global.roundN.md` snapshots.
- Put cross-run state needed by a later invocation under `state/`, never under a prior run.
- Refresh the artifact index and summary before publishing a terminal status.

## Strict migration

There is no legacy read or write fallback. Direct `vfx plan`, `vfx build`, `vfx accept`, and
`vfx render` invocations create their own structured run when they are not children of `vfx run`.
Shot-wide `logs/`, `renders/`, `.artifacts/`, `.snapshots/`, and `.versions/` are unsupported and
must not influence a verdict, comparison, diagnosis, or resume decision.

Historical files may be archived outside the active shot or imported deliberately into a
schema-valid run. The runtime never guesses which old files belong together.
