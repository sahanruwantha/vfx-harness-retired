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
| **plan** | `bambi plan <shot>` | Reads `brief.md` + refs (attached as images), emits `plan.md` plus four machine-readable companions: `layers.json`, `acceptance.json`, `critic_axes.json`, [`checks.json`](#executable-checks--the-contract-a-layer-is-held-to). Two-pass by default: opus-5 drafts, opus-5 audits. `--until-clean` adds a repair loop against the deterministic [plan gate](#checking-the-plan-itself). Any ambiguity becomes a **question answered before building starts**, never mid-build. |
| **build** | `bambi build <shot> --layer 1` | Builds ONE layer as an additive delta script (`build/01_layout.py` …). Iterates live in Blender, then writes a script that must rebuild it from empty. |
| **acceptance** | `bambi accept <shot>` | Replays the whole chain from an empty scene and judges the approval moments on the full rubric. `--repair` routes a failure back to the layer that owns the failing axis. |
| **render** | `bambi render <shot>` | Runs the accepted chain and encodes the frame range to mp4. |

Supporting commands: `bambi_vfx.escalate` (answer plan questions), `bambi_vfx.agents.asset_builder`
(image→3D asset caching), `bambi_vfx.verify_recipes` (audit the cookbook), `bambi_vfx.skills`.

## How the flow actually runs

```
brief.md + refs/  ──►  PLAN ──►  gate ──►  repair ──┐
                        (draft → verify)   ▲         │  loop until clean,
                                           └─────────┘  stalled, or budget

                  ──►  BUILD layer N  ──►  critic (owned axes only)
                        │                  metrics (can fail outright)
                        │                  propose_checks  ──►  checks.json
                        └─► canonical replay from empty ──► ablation ──► pass

                  ──►  ACCEPT (full rubric, whole chain)  ──►  RENDER
```

**Nothing self-certifies.** Every mechanism the prompts merely *asked* for reads zero in
practice — `[unknown]` tags used 0 times across four plan documents, `ask_supervisor` never
fired, no plan carried a single source URL. What changed behaviour was making the harness
*refuse*: `measure_ref` returning `n/a` instead of a fabricated number moved fingerprint
accuracy from 104/110 to 95/95 with no prompt change at all.

So the rule throughout: **evidence a tool deposits on disk survives; evidence a model is
asked to record does not.** `spike` writes a file to the lab and its citations are precise
and true. `WebSearch` writes nothing, and no plan has ever carried a link.

### Where reasoning happens, and where it does not

| decision | who | why |
|---|---|---|
| resolve brief↔reference contradictions | planner (VLM, refs attached) | a whole-shot fact; a single layer cannot see it |
| ask what only the client can settle | `ask_supervisor` → `answers.md` | the build stage has no way to ask — by the time a layer finds the problem, earlier layers have committed |
| choose which crop/metric expresses a target | planner or builder (VLM) | picking *what* to measure is judgement |
| decide whether a check is sound | `checks.py` — **no model** | five rules over pixels; a VLM proposes, arithmetic disposes |
| decide whether a layer did its work | `verify_necessity` — **no model** | pass after, fail before |
| score what has no number | critic (VLM) | and this list should keep shrinking |

The planner escalates rather than guessing. On this shot it caught that the brief demanded
an off-axis camera while every wide approval still is square-on, measured the silhouette at
0.098W, and asked instead of silently choosing — the failure mode that once put a 16:9 crop
against 2:1 references and made every composition score meaningless.

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

## Executable checks — the contract a layer is held to

A done-condition written in prose is a sentence nobody runs. Three shipped in one plan
before anything caught them: a lit/shadow pier ratio of `1.35–2.2` against a plate that
reads **1.06**; a whole-frame `G/R < 1.08` against a plate that reads **1.139** — in a plan
that had *already* caught that exact failure at another frame, written it up as a resolved
decision, and repeated it six hundred lines later; and a `mean >= 14` **floor** aimed at a
defect that was a **ceiling**, which the known-bad render passes comfortably.

So a check is a **record**, not a sentence, and it may not enter a plan until it has been
RUN. `checks.json` is the fourth machine-readable companion, and `bambi evals plan` re-runs
every rule against the artifacts on disk:

```json
{ "id": "L2c-3", "layer": "2", "axis": "hero_facade_cells", "frame": 1,
  "ref": "refs/f001_open.jpg", "metric": "region_lit_variance",
  "regions": {"r": [0.38, 0.60, 0.62, 0.95]},
  "op": "band", "lo": 0.13, "hi": 0.30, "stage": "pre_grade",
  "rejects": ["renders/2@f1_canonical_f1.png"],
  "proof": {"ref": 0.157, "adversary": [0.0819]} }
```

Five rules, each earned by a defect that got past the others:

| rule | rejects |
|---|---|
| **reachable** | the reference itself fails it — the target is unreachable, so the layer loops until its budget is gone |
| **has teeth** | the named adversary passes it too — it cannot fail the defect it exists for |
| **beats noise** | reference and adversary differ by less than the metric's own resampling movement, measured here and never declared by the author |
| **proof reproduces** | the shipped spec does not reproduce its recorded `proof` — the spec that was tested is not the spec that shipped |
| **not fragile** | nudging the region 1% inverts the verdict, so it measures where the box was put rather than what is in the picture |

`rejects` is the author's real work. Rule 2 is only as strong as the negative it is graded
against: scoring a sky on band σ looked discriminating only because a *layout* render with
no sky at all failed it, while the banded fog-wall it was written to catch sailed through.

**Coverage.** Every layer needs at least one check runnable **at its own stage**. A layer
whose checks all need the final grade is judged on prose until layer 7 exists — which is
how a layout layer shipped with one `post_grade` check and took three build attempts.

## The builder authors checks too

Checks were authored by the stage with the **least** information. The planner writes them
before any scene exists, from reference images alone — so every metric compares pixels to a
plate, most checks land `post_grade`, and layout gets one that cannot fire. That is not
laziness: a planner cannot write a check about a scene that does not exist.

The builder can. It re-derived that the plan's pitches put the hero's roof at NDC 0.89 and
that the roll ladder was mirrored — real verification, found by the only stage able to find
it, with nowhere to go. `propose_checks` gives it somewhere:

> **pass on your render, and fail on the state before your layer ran.**

That is a mechanical statement of *this layer did its work*, and it cannot be gamed because
the author does not choose the adversary — the previous layer's render is. Layer 2 recorded
seven, with separations like σ **50.53** against layout's **9.03**.

Builder checks are **re-verified when the renders stop moving** (layer pass, before
ablation) and dropped if they no longer hold: a check authored mid-layer describes whatever
render was in front of it, and a later attempt replaces every render. Layer 1 shipped three
stale ones before this existed.

## Where judgement is allowed

The pipeline drifts toward VLM scoring wherever a measurement is hard, and that is where it
fails: a lighting axis spent 16 rounds unresolved; a facade shipped the brief's loudest
anti-goal — *"windows on a regular grid, identical spacing, one colour"* — and **passed at
3.0**, because every metric was a histogram or an edge count and a uniform lattice with
varied per-cell brightness satisfies σ completely.

Each fix moves one decision from *scored* to *measured*:

| defect | the metric that sees it |
|---|---|
| flat fog-wall sky | `aniso_top` — vertical vs horizontal gradient energy. `structure_top` reads 15% apart on two skies with nothing in common; this reads 76% |
| evenly-lit facade | `region_lit_variance` — spread of lit fraction across sub-blocks. Render **0.082**, reference **0.157**, same mean occupancy |
| glow that cannot be measured | `halation` returns *absent*, never `0.0`, below a hot-core density floor |

The first attempt at the facade metric measured spatial **periodicity** and was thrown
away: the references scored *more* periodic than the render, because real towers do have
regular window columns. The anti-goal was never about spacing — it is about **occupancy**.
Shipping it would have failed the references and passed the flat render.

The VLM is still needed for what genuinely has no number. That list should keep shrinking.

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

## Checking the plan itself

The plan sets every target the rest of the run aims at, and until recently nothing checked
it. The planner *is* given the tools to know what it doesn't know — `WebSearch`, `WebFetch`,
`Grep`, a Blender spike lab, an `ask_supervisor` escalation path — and a two-pass structure
where an adversarial verifier audits the draft. What it lacked was anything that could tell
whether it used them. Measured across both shots and all four plan documents:

| mechanism | the contract says | actual |
|---|---|---|
| `[unknown]` tag | gates **all** web research | **0** in every document |
| `ask_supervisor` | ask instead of guessing | **never fired** |
| research source links | "source links in the ticket" | **0 URLs**, anywhere |
| verify-pass spikes | "re-spike what is uncited or contradicted" | **0** — it audits by reading |
| verify instruction #2 | "measure-check every approval still" | **7** shipped that don't reproduce |

Against which: spike citations are *precise and true*. `spike_05.out` **line 9** is cited for
"the default is 100", and line 9 reads `volumetric_start/end: ... 100.0`.

The difference is not diligence. **`spike` writes a file to the lab; `WebSearch` writes
nothing.** Evidence a tool physically deposits survives and stays checkable; evidence the
model is merely *asked* to record evaporates. So every check below is a check on an artifact,
never on a claim about one:

```bash
bambi evals plan          # free, no model, no Blender; every shot by default
bambi evals grounding     # just the fingerprint half
bambi evals plan --feedback   # the repair brief a loop round would receive
```

- **grounded** — every stated number re-derives from the plate it describes, through *the
  same functions the planner ran*. 153/160 reproduce. The 7 that don't are all one metric:
  `M2` asked for `halation 2340.1 (the shot's peak)` and `M1` for `halation 0.0 (no point
  sources — do not manufacture a halo here)` against plates with almost no blown pixels. Both
  were real readings from a ratio dividing by a 0–4 pixel denominator, and one had been
  reasoned into a build instruction. A checker that only diffed numbers would have called
  `M1` a perfect match — hence a third verdict, **`UNMEASURABLE`**, distinct from `MISMATCH`.
- **citations** — every cited file resolves and every cited line exists. Not fabrication:
  link rot. Five citations into `../barrel_roll/logs/plan_lab_draft/` broke when that shot
  was archived. The claims stayed true; a build agent reading them today gets nothing.
- **evidence** — a `✓spiked` ticket must cite an artifact that *resolves*. Matching the
  substring `spike_04` would pass every ticket in a plan whose lab no longer exists.
- **contracts** — an axis a layer `owns` must exist in `critic_axes.json`, and every ref must
  be a real file. A layer owning an axis the critic never scores cannot be judged on the one
  thing it is responsible for, and nothing else notices.

Numbers and tokens no rule recognises are reported, never skipped: a checker that quietly
ignores what it doesn't understand reports "all grounded" while checking nothing.

### Does the planner actually see the shot?

It does now, by construction. It used to be luck. The kickoff handed over a **list of
filenames** — `_refs_block`'s own docstring said *"stills are the ONLY visual input"* and
then emitted ten lines of text. `measure_ref` returned numbers with no picture. The tools
that *do* return images, `contact_sheet` and `extract_frames`, were decorated with `@tool`
and **never passed to `create_sdk_mcp_server`** — uncallable on every shot, while
`contact_sheet`'s description read *"This is how you do the scene read."*

So whether the planner ever looked at the thing it was planning came down to whether it
happened to try `Read` on a `.jpg`. In the run that produced `barrel_roll`, it did — all
ten. And the plan still scored **26** mentions of `halation` (which `measure_ref` reports)
against **0** for camera angle, shadow side or solid form (which only the picture carries).
The render read as a flat card.

Three changes, so the visual half of the brief is guaranteed rather than hoped for:

- every reference still is **attached to the kickoff** as an image, in shot order — the
  same guarantee the critic has had ("it cannot score a frame it never saw"). There is no
  argument for holding the stage that *writes* the targets to a lower bar than the stage
  that checks them
- `measure_ref` returns **the image alongside its numbers**, so "measured it" and "looked
  at it" stop being separable
- the video tools are **registered when the shot has video**, so their absence is a
  decision with a reason instead of an omission

### Iterating until it holds

```bash
bambi plan shots/barrel_roll --until-clean [--max-rounds 3]
```

Draft → verify → **gate** → repair → gate → … The loop converges against the deterministic
gate rather than another critique pass, because "solid" judged by the same model that wrote
the plan is the self-certification problem again. Three ways to stop, and two are failures
reported as failures: **clean**, **stalled** (a round produced findings identical to the
previous one — the repair isn't converging, so paying for the same answer again helps
nobody), and **budget**. Each round snapshots its input as `plan.roundN.md`.

The repair brief carries only blocking findings and states that a finding is closed by making
the plan *true*, not by making the check quiet — deleting a target, dropping a citation or
softening a number all clear the gate and leave the plan weaker. It also permits the pass to
**contradict** a finding in §0 with evidence: a gate that cannot be argued with encodes its
own bugs into every plan.

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
  checks.json               executable checks — the contract, re-run by the gate
  answers.md                supervisor decisions; these are LAW and outrank inference
  shot.json                 the ledger: verdicts, rounds, run/attempt ids, acceptance
  renders/                  judged frames, motion strips, the final mp4
  logs/
    run_layerN.json         per-layer report: rounds, cost, tokens, cache hit, hooks, tools
    transcript/*.jsonl      every message: prompts in, text/thinking out, tool calls,
                            tool results, critic verdicts   ← the durable record
    console/<run-id>.log    the run's console narrative, exactly as it scrolled past
    layer_state.json        per-frame conclusions that survive a compaction or crash
    cost.jsonl              one row per model session, labelled by ROLE — a layer total
                            cannot say whether the builder or the critic spent it
    tool_failures.jsonl     every tool call that raised, with the input that raised it
    journals/               the run_bpy calls a layer made, for replay
    recipe_use.jsonl        which cookbook entries were pulled
    plan_lab/               the planner's scratch spikes and their output
    N_prerepairM.py         the build script as it was before a canonical repair
```

Three records, three jobs, and they are not substitutes for each other:

| | answers | shape |
|---|---|---|
| `logs/run_layerN.json` | did this layer go well? | aggregates |
| `logs/cost.jsonl` | what did it cost, **by role**? | one row per model session |
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
