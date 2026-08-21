# Evaluation definitions

This directory contains versioned definitions used to decide whether a VFX Harness
change generalizes. Generated runs, renders, transcripts, and shot-derived baselines belong
under `artifacts/evaluations/` and are not tracked.

- `suites/`: capability and regression suite definitions.
- `fixtures/`: small heterogeneous inputs and injected failures.
- `graders/`: evaluation-only scoring policies.
- `baselines/`: explicitly approved, distributable expected summaries only.
