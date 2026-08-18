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
| **plan** | `bambi plan <shot>` | Reads `brief.md` + refs, emits `plan.md`, `layers.json`, `acceptance.json`, `critic_axes.json`. Two-pass by default: opus-5 drafts, opus-5 audits. Any ambiguity becomes a **question answered before building starts**, never mid-build. |
| **build** | `bambi build <shot> --layer 1` | Builds ONE layer as an additive delta script (`build/01_layout.py` …). Iterates live in Blender, then writes a script that must rebuild it from empty. |
| **acceptance** | `bambi accept <shot>` | Replays the whole chain from an empty scene and judges the approval moments on the full rubric. `--repair` routes a failure back to the layer that owns the failing axis. |
| **render** | `bambi render <shot>` | Runs the accepted chain and encodes the frame range to mp4. |

Supporting commands: `bambi_vfx.escalate` (answer plan questions), `bambi_vfx.agents.asset_builder`
(image→3D asset caching), `bambi_vfx.verify_recipes` (audit the cookbook), `bambi_vfx.skills`.

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
Objective metrics (`bambi_vfx/metrics.py`) run alongside and can decide a moment outright.

## Checks that need no judgment

A score is the wrong instrument for anything measurable. `check_scene` answers questions a
beauty render cannot show at all, with no critic and no cost: is the hero actually visible
from the camera (ray-cast), where is it in frame (NDC bbox), is the move unbroken (speed,
acceleration, jerk), is the mesh sound (non-manifold edges, loose verts, islands), is scale
applied, does the render buffer contain NaN.

Each of these is gated on a **known-bad fixture**. A check nobody has watched fail is not a
check, so `bambi_vfx.evals checks` builds one deliberately broken scene per check and fails
if any of them stays silent:

```bash
bambi evals checks     # free, Blender only, no model
```

`render_pass` shows the builder what it is being judged on rather than a composite it has to
squint past: `pass='diffuse_direct'` renders modelling by light with emission removed
(measured: an emissive body goes from 180/255 to 0.1), `shade='clay'` or `'silhouette'` for
form, `light='<LightObject>'` for one lamp's contribution, and `crop` + `res_pct=400` for a
true optical zoom instead of an upscaled thumbnail. Every mode ships a caption naming what to
look for — a visual channel with no text to read it by measured *worse* than not adding it.

Two probes keep that honest, because both caught real defects:

```bash
.venv/bin/python docs/probes/spike_render_modes.py      # does each mode isolate what it claims?
.venv/bin/python docs/probes/spike_render_isolation.py  # is the DEFAULT render path unchanged?
```

The first caught passes that rendered bit-identical to beauty under a caption promising
emission had been removed, and a light-group isolation EEVEE never performs. The second
caught a diagnostic mode leaking a world back into the scene, brightening every canonical
render after it by 47.8/255.

## Quickstart

Requires **Blender 5.x** on `PATH` (headless) and Python ≥ 3.11.

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env          # set ONE auth variable (below), + MESHY_API_KEY for assets

.venv/bin/python -m tests.test_harness        # deterministic suite, no Blender or network

bambi preflight
bambi plan  shots/barrel_roll
bambi build shots/barrel_roll --layer 1
```

The CLI loads the repository `.env` explicitly; shell-exported variables take precedence.
Set `BVFX_ENV_FILE=/absolute/path/to/file` to use a different dotenv file. Importing the
Python package never loads credentials or mutates the environment.

**Auth — check it before you spend anything:**

```bash
bambi preflight
```

Two variables work and **only these two names are read**: `CLAUDE_CODE_OAUTH_TOKEN` for
subscription billing (`claude setup-token`) or `ANTHROPIC_API_KEY` for pay-per-token API
billing. When both are set the **API key wins** (measured). Prefer the API key if you care
about the cost numbers — `MAX_BUDGET_USD`, the per-layer budget stop, and `evals compare`'s
cost deltas all read `total_cost_usd`, which is real money under an API key and not
comparable to it under a subscription.

Get the *name* wrong and nothing tells you. A key added as `CLAUDE_API_KEY` — a plausible
name that nothing reads — was silently ignored while a stale OAuth token was used instead,
and that subscription was over its monthly spend limit. The result was not an error: the
session reported `subtype=success`, `cost=$0.0000`, one turn, and the limit message
arriving as ordinary assistant text. A build layer in that state "succeeds" having built
nothing, then pays the critic to score an empty scene. `preflight` names the dead variable
and the one to use instead; `empty_success` catches the zero-cost/zero-tool shape at
runtime. A *wrong* (rather than misnamed) key costs wall time instead: the CLI retries a
401 ten times with backoff, ~190s per call, so a run that stalls before anything renders
is usually auth.

Run the whole shot — every layer, acceptance, then the mp4 — under one run id:

```bash
bambi run shots/barrel_roll            # --dry-run to preview
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
  renders/                  judged frames, motion strips, the final mp4
  logs/
    run_layerN.json         per-layer report: rounds, cost, tokens, cache hit, hooks, tools
    transcript/*.jsonl      every message: prompts in, text/thinking out, tool calls,
                            tool results, critic verdicts   ← the durable record
    console/<run-id>.log    the run's console narrative, exactly as it scrolled past
    layer_state.json        per-frame conclusions that survive a compaction or crash
    journals/               the run_bpy calls a layer made, for replay
    recipe_use.jsonl        which cookbook entries were pulled
    plan_lab/               the planner's scratch spikes and their output
    N_prerepairM.py         the build script as it was before a canonical repair
```

Three records, three jobs, and they are not substitutes for each other:

| | answers | shape |
|---|---|---|
| `logs/run_layerN.json` | did this layer go well? | aggregates |
| `logs/transcript/*.jsonl` | *why* did it decide that? | one JSON line per event |
| `logs/console/*.log` | what did it look like happening? | the narrative, in order |

Read them with one command rather than six greps:

```bash
bambi inspect shots/barrel_roll             # the digest
bambi inspect shots/barrel_roll --layer 3   # action timeline
bambi inspect shots/barrel_roll --tools     # tool adoption
```

The digest leads with **findings**, not data: a layer whose score never moved, one that
regressed between rounds, one whose canonical replay did not reproduce, one that got no
objective metric feedback, one that measured more than it looked — and any diagnostic tool
the builder never called. That last one is reported as a finding on purpose: `render_pass`,
`check_scene` and `diff_frames` exist and are documented in the builder prompt, so zero
calls means the *prompt* is not landing, not that the tool is unnecessary. A layer whose
report predates the telemetry is reported as **unmeasured**, never as zero, so neglect is
never inferred from a run that could not have been measured.

Transcripts strip base64 image payloads to a one-line placeholder recording size and mime
type — a live probe put 780KB of base64 in and got an 18KB file out — while keeping
`run_bpy` scripts **verbatim**, because the console clips them to stay readable and a
clipped script cannot be diffed against the next attempt. Set `BVFX_NO_TRANSCRIPT=1` to
turn recording off.

```bash
jq -r 'select(.kind=="critic") | "\(.frame) \(.mean) \(.verdict)"' logs/transcript/*.jsonl
jq -r 'select(.kind=="tool_use") | .tool' logs/transcript/*.jsonl | sort | uniq -c
```

## Layout

```
src/bambi_vfx/agents/     planner, builder, acceptance, and asset orchestration
src/bambi_vfx/assets/     asset providers and normalization
src/bambi_vfx/blender/    headless Blender boundary, tools, and checks
src/bambi_vfx/eval/       evaluation and reproducibility checks
src/bambi_vfx/recipes/    verified agent cookbook
src/bambi_vfx/config.py   typed runtime configuration and explicit dotenv loading
src/bambi_vfx/cli.py      unified `bambi <verb>` command dispatcher
docs/probes/              spikes against real Blender
tests/                    deterministic suite (no Blender, model, or network)
shots/                    local shot inputs and outputs (untracked)
```
