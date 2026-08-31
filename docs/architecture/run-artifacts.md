# Run Artifact Contract

Generated output is a public interface between the VFX runtime, evaluation tools, developers,
and coding agents. It must be discoverable from stable metadata rather than reconstructed from
filenames, timestamps, or a scan of the shot directory.

## Authority boundaries

```text
shots/<shot>/
  brief.md, refs/              authored inputs
  plans/current.json           atomically selected immutable plan generation
  plans/, *.json contracts     legacy consumer compatibility surface; never planner staging
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
      stop-envelope.json       one immutable typed stop when the run is unaccepted
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
      plan-workspace/          mutable global-plan candidate; authored inputs only
    deliverables/
```

`manifest.json` is the entrypoint and declares the schema, identity, invocation parameters,
layout, and authority relationships. `status.json` is the terminal state. For a failed or
interrupted run it selects `reports/stop-envelope.json` by relative path and content digest;
proximity alone does not select an envelope. `artifacts.json` is a sorted catalog with path,
category, size, and media type. `reports/summary.json` is the compact semantic digest.
`latest.json` is a small pointer, not a symlink, so it works across platforms and copied
evaluation fixtures.

`vfx-harness.stop-envelope/v1` is the machine-readable unaccepted-boundary authority. It
binds one closed stop class and stage, exact authority/state identity, stable cause fingerprint,
exact attempt-evidence digest, content-addressed evidence references, and one typed action with
its target, dispatch mode, state preconditions, progress postcondition, and required receipt
schema. Its expected/found/next-action prose is derived diagnostic text. A valid envelope does
not mean the action can already be dispatched: no controller, persistent controller journal,
key-consuming transaction adapter, or transaction-receipt producer exists yet. An untyped
boundary is published as `harness_defect`; readers never infer a narrower class from its exit
code or prose.

For planning runs, `reports/plan_gate.json` is the terminal deterministic authority. Its outcome,
blocking count, and report path are repeated in `status.json` and the summary so readers can decide
whether to open the full finding set without rerunning the gate.

A gate-clean untagged planning run also stores a content-addressed plan bundle under its
`checkpoints/plans/bundles/` directory and atomically updates `plans/current.json`. The pointer
names the producing run, bundle path, aggregate hash, and gate outcome. Bundle-aware readers verify
every member hash and fail closed on a malformed pointer. Bundles include typed requirements,
obligations, assumptions, harness-authored input provenance, and every nested Markdown unit or
evidence plan present at publication. A ready-unit plan authored during global planning executes
directly from that immutable bundle; a later JIT plan remains shot-root state and must carry a
sidecar pin to the same selected bundle hash. Current build and evaluation readers select global
plan artifacts through the pointer and never fall back from malformed selected authority.
Pointer-less archived fixtures retain a bounded compatibility read until republished.

The planner's current working directory is
`runs/<run-id>/scratch/plan-workspace/`. Staging copies `brief.md` and materializes `refs/`, but
does not copy prior plans, contracts, questions, builds, or runs. Draft, verify, repair, hooks,
and the plan gate share this workspace. A repair may read only the exact immutable snapshot named
in its assignment outside the workspace; general Read/Glob/Grep discovery and every Write/Edit
remain confined. Only a complete clean candidate can cross from scratch into the run's immutable
plan bundle and then become selected by the atomic shot-root pointer.

## Reader protocol

1. Read `runs/latest.json`, or select a run with `vfx inspect --run <id>`.
2. Validate `manifest.json.schema`; fail closed on an unsupported schema.
3. Read `status.json` before interpreting partial output. For `failed` or `interrupted`,
   require its stop-envelope path and digest, then strictly read back that envelope and
   verify its run identity. The only exception is an explicit envelope-unavailable
   publication failure, which halts and authorizes no action. `detail`, `terminal_cause`,
   and exit code are operator summaries, never dispatch authority. HIR-0037 still ensures
   an integer `SystemExit` does not degrade that summary to a digit. Materialization sessions bind
   `logs/transcripts/plan/materialize-layer-*.jsonl` (HIR-0038).
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
- Publish an unaccepted run's one immutable stop envelope and read it back before selecting
  its digest in terminal status. If publication or read-back fails, publish only the explicit
  envelope-unavailable failure state and halt; do not dispatch from partial state.
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
