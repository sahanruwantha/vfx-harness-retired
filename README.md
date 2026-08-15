# bambi-vfx

An **agent-driven Blender VFX pipeline** on the Claude Agent SDK. A shot is specified as a
markdown brief plus a board of reference frames; agents plan it, build it in a live headless
Blender, judge each piece against the references, and render the result.

The organising idea is that **every claim is checked against something objective**. A layer
does not pass because an agent says it looks right — it passes because a critic scored the
frames it is responsible for, a deterministic re-run of its script reproduced those frames
from an empty scene, and measured image metrics agree.

## The stages

```
plan → build (×N layers) → acceptance → render
```

| stage | command | what it does |
|---|---|---|
| **plan** | `python -m pipeline.plan_agent <shot>` | Reads `brief.md` + refs, emits `plan.md`, `layers.json`, `acceptance.json`, `critic_axes.json`. Two-pass by default: opus-5 drafts, fable-5 audits. Any ambiguity becomes a **question answered before building starts**, never mid-build. |
| **build** | `python -m pipeline.build_agent <shot> --layer 1` | Builds ONE layer as an additive delta script (`build/01_layout.py` …). Iterates live in Blender, then writes a script that must rebuild it from empty. |
| **acceptance** | `python -m pipeline.accept_agent <shot>` | Replays the whole chain from an empty scene and judges the approval moments on the full rubric. `--repair` routes a failure back to the layer that owns the failing axis. |
| **render** | `python -m pipeline.render_shot <shot>` | Runs the accepted chain and encodes the frame range to mp4. |

Supporting commands: `pipeline.escalate` (answer plan questions), `pipeline.asset_agent`
(image→3D asset caching), `pipeline.verify_recipes` (audit the cookbook), `pipeline.skills`.

## Layers

A shot is built as an ordered stack of **layers**, ids starting at 1. Each layer is one
delta script that adds only its own contribution and must not break what earlier layers were
judged on. Layer N runs every accepted script below it first, so the chain is always built
the way it will finally be rendered.

Each layer declares:

- **`owns`** — the look axes it is responsible for. The critic marks every other axis `"n/a"`,
  so a layout layer is not penalised for absent lighting.
- **`judges`** — every frame it answers for. A layer passes only if **all** of them clear.
  Single-frame judging is what once let a blacked-out stretch of a shot through.

## Fail-closed by design

The pipeline refuses rather than proceeding on unreviewed work:

- building a layer on a prior that never passed (`UnpassedPrior`, exit 6)
- judging acceptance on a partial chain (`IncompleteChain`, exit 7)
- rendering a chain with missing or unaccepted layers (`IncompleteRender`, exit 7)
- building when `brief.md` has changed since the plan was written (exit 8)
- building with unanswered plan questions (exit 5)

Each has a `--force` for debugging, which names exactly what it is overriding.

## Judging

The critic receives the reference, the candidate render, an optional motion strip and the
previous best attempt **as attached images** — it cannot score a frame it never saw. It must
also declare `reference_usable`; handed a mismatched plate it fails the verdict instead of
quietly grading against the brief's prose.

Critic scores are noisy — the same render against the same reference has scored 4.0, 3.0,
3.0 and 2.0 — so a verdict landing near the pass line goes to **best-of-three with a median**.
Objective metrics (`pipeline/metrics.py`) run alongside and can decide a moment outright.

## Quickstart

Requires **Blender 5.x** on `PATH` (headless) and Python ≥ 3.11.

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env          # set CLAUDE_CODE_OAUTH_TOKEN (and MESHY_API_KEY for assets)

.venv/bin/python -m tests.test_harness        # deterministic suite, no Blender or network

set -a; . ./.env; set +a
.venv/bin/python -m pipeline.plan_agent  shots/barrel_roll
.venv/bin/python -m pipeline.build_agent shots/barrel_roll --layer 1
```

Run the whole shot — every layer, acceptance, then the mp4 — under one run id:

```bash
.venv/bin/python -m pipeline.run_shot shots/barrel_roll            # --dry-run to preview
```

It skips layers already recorded as passed (so it doubles as resume), stops at the first
failing layer rather than stacking work on it, and propagates that layer's exit code.

## What a run leaves behind

```
shots/<shot>/
  brief.md refs/            inputs: the spec and the reference board
  plan.md layers.json …     the plan, plus plan.provenance.json (hashes of its inputs)
  build/NN_*.py             one delta script per layer — the real artifact
  shot.json                 the ledger: verdicts, rounds, run/attempt ids, acceptance
  logs/run_layerN.json      per-layer report: rounds, cost, tokens, cache hit, hooks
  renders/                  judged frames, motion strips, the final mp4
```

`logs/run_layerN.json` is the place to look first when a layer goes wrong: it records what
the hooks did, and a hook that *never fired* is the signal that something silently no-opped.

## Layout

```
pipeline/            the stages, ledger, metrics, prompts, hooks
pipeline/blender/    the warm headless Blender session, its tools and bvfx_* helpers
pipeline/recipes/    the cookbook — vetted, measured Blender techniques
tests/               deterministic suite (no Blender, no network)
shots/               shot briefs, reference boards and outputs (untracked)
```
