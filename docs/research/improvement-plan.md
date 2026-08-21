# The plan sets the target; the verifier only says whether you hit it

**Status:** Phases 1 and 2 shipped (`e5c346e`, `94a18a6`). Phase 0.1 run 2026-08-18 —
`lighting_and_form` scored **0.0** on a black frame against 2.0 on real renders, so the axis
is image-dependent and Phase 2 was justified. **Phase 3 (gate the plan) is underway** — 3.3b
shipped as `vfx evals grounding`, the broken metric it found (3.3c) is fixed in both of its
implementations, and 3.1/3.3 shipped as `vfx evals plan` + `vfx plan --until-clean`;
**3.2, 3.4 and 3.5 remain.**

The measurement that reframed Phase 3, taken across both shots and all four plan documents:
the planner already HAS every tool for knowing what it does not know — `WebSearch`,
`WebFetch`, `Grep`, a spike lab, `ask_supervisor` — and every one of those mechanisms reads
**zero**. `[unknown]`, which gates all research: 0. `ask_supervisor`: never fired. Source
URLs, which step 6 requires: 0. Verify-pass spikes: 0. Against which, spike citations are
precise and true (`spike_05.out` line 9 cited for "the default is 100"; line 9 reads
`volumetric_start/end: ... 100.0`). **`spike` writes a file; `WebSearch` writes nothing.**
Evidence a tool deposits on disk survives; evidence the model is asked to record does not.
That is the design rule for the rest of this phase, and it is why the repair loop converges
against a deterministic gate rather than another critique pass. Phase 3 was
added after the run evidence was attributed: every layer that thrashed did so for a reason
the plan authored, and the plan is the only artifact nothing checks. Evidence is from the `barrel_roll` run of
2026-08-15/16, from probes run against Blender 5.2.0 LTS on this machine on 2026-08-18, and
from a literature sweep of the same date. Measured numbers are marked; inferences are
marked as such.

> **Note on ordering.** Phases 1–2 were built before Phase 0 ran. That was the right call —
> both are independent of the blank-frame result and both caught real defects immediately
> (`spike_render_isolation.py` found a diagnostic mode corrupting every canonical render
> after it by 47.8/255). But Phase 0.1 is now *more* important, not less: we have added a
> visual channel without ever measuring whether the critic uses the one it had.

Nine layers were planned. Five were attempted. Layer 5 has never passed. Layers 6–9 have
never been built. The final attempts alone cost $47.48; layer 2's five discarded attempts
cost a further $78.76 by `docs/research/findings/lighting.md`'s own accounting.

This document is the plan for why that happened and what to change.

---

## Part I — The diagnosis

### 1. The pass gate is a conjunction of noisy coin flips

A layer passes only if **every** judge frame clears in one canonical sweep. Measured
per-frame pass rates from `shot.json`:

| layer | judge frames | per-frame pass rate | sweeps passed | attempts |
|---|---|---|---|---|
| 1 layout | 2 | 100% | 2 / 2 | 1 |
| 2 signage | 4 | **44%** | **1 / 9** | 6 |
| 3 city | 2 | 38% | 1 / 4 | 3 |
| 4 sky | 2 | 100% | 1 / 1 | 1 |
| 5 lighting | 2 | **10%** | **0 / 5** | 4, failed |

With judge sd measured at **0.603** and a pass line at 3, each frame is a Bernoulli trial.
Layer 2 passed on sweep 9 of 9 — that is what p⁴ looks like. **Adding judge frames, which
is correct for coverage, makes the gate exponentially harder for reasons unrelated to
quality.** The passing sweep is then frozen as truth and bound to a script hash.

### 2. Rubric scope moves the verdict more than the image does

Same render, same reference, same critic model, now preserved under
`artifacts/evaluations/variance/barrel_roll`:

```
layer scope  (1 axis scored):   camera_framing = 3.00   → PASS
full rubric  (6 axes scored):   camera_framing = 1.88   → FAIL
```

A 1.12-point swing from prompt context alone, on a range that is effectively 1–4. This is
why layers pass individually and end-to-end acceptance does not.

### 3. Numeric specs converged; prose specs thrashed

Counting hard numeric targets in each layer's `reads:` string against attempts to pass:

| layer | numeric targets | attempts |
|---|---|---|
| 4 sky | **12** | 1 |
| 1 layout | 4 | 1 |
| 3 city | 5 | 3 |
| 2 signage | 2 | 6 |
| 5 lighting | **0** | 4, failed |

The score was never the mechanism. **The numeric spec was.** Where the plan gave numbers,
the loop had something to converge on. Where it gave prose, the score *was* the mechanism,
and a σ=0.6 grade is not a mechanism.

### 4. Layer 5 was judged against the wrong image

| layer | owns | judged against |
|---|---|---|
| 1 | camera_framing | finished shot |
| 2 | facade / signage | finished shot (plates exist, advisory only) |
| 3 | city | **isolated L3 plate** |
| 4 | sky | **isolated L4 plate** |
| 5 | lighting_and_form | finished shot, **no plate exists** |

`critic_axes.json` asks the critic to *"Score MODELLING BY LIGHT only — emissive sources
must not raise or lower this score."* That is asking a vision model to mentally subtract
emission from a beauty render. In Blender it is a render setting.

`refs/layer2/`, `refs/layer3/`, `refs/layer4/` exist. There is no `refs/layer5/`. The
plates are hand-made PNGs, untracked, with no script that produces them.

### 5. The critic cannot see what it is scoring

Measured against Claude's documented 28×28 px patch tokenisation:

```
render scale 0.4 (builder default)   hero tower  80.6px  →  5.8px per facade pier
critic JPEG cap 1568                 hero tower 164.6px  → 11.8px per pier
metrics canonical h=320              hero tower  67.2px  →  4.8px per pier
```

A 6px feature is **0.21 of one patch** — no representational slot in the encoder. Measured
elsewhere: fine discrimination is **0.00 accuracy at ≤2 patches (28px)** and **1.00 at ≥4
patches (56px)**. Your features are five times below the floor.

And `_DISPLAY_H` was **512**, so a 1920×960 render shipped to the critic at 1024×512 = 703
tokens. The full render passes Claude's high-res tier **unresized** at 2415 tokens, for
$0.012. The adjacent `_METRIC_H = 320` is correct and its reasoning is sound; the two
constants answer different questions and the reasoning for one leaked into the other.

*(Fixed — `_DISPLAY_H = 1024` as of `94a18a6`, with a comment stating why the two differ.)*

### 6. Lighting is the worst possible axis for a VLM

Measured just-noticeable-difference against human:

| axis | GPT-4o vs human |
|---|---|
| blur | **parity** (1.25 vs 1.24) |
| JPEG | **better than human** |
| colour | 2.3× worse |
| **brightness** | **2.3× worse** |
| **contrast** | **4.4× worse** |

On geometry/lighting inconsistency specifically: best model **34.2%** against human 84.8%.
On structural "hierarchy" axes: Pearson **−0.14 to +0.38**. Layer 5 owns exactly these.

Worse: sub-JND changes compound invisibly. One model reported "identical" for *every*
brightness+contrast combination at ≤⅘ of each per-axis threshold. The loop can drift the
image visibly while the score never moves.

### 7. The loop has no state and the critique may be worth nothing

Layer 5's sixteen rounds, verbatim from `shot.json`:

```
r3  "over-lit into a uniform light grey ... drop key energy"
r6  "near-black ... add an off-axis AREA key"
r9  "key far too hot ... cut energy 3-4x"
r11 "hero shaft is unlit ... add a large AREA key"
r12 "over-keyed ... halve it"
r14 "non-window pixels sit near-black ... add a dedicated hero key"
```

The critic has no cross-round memory, so it reports the current overshoot each time. The
journal shows the consequence: `energy=3.0e4 → 4.4e4 → 8.0e5 → 2.5e4 → 8.0e4`, five lights,
one scalar reward, no recorded parameter→measurement mapping.

`layer_state.json` stores six `tried` entries that are **three approaches recorded twice
each, verbatim**. `ruled_out` is empty. Reflection Repetition Rate = **0.50**, exactly the
published threshold for flagging a confabulating loop.

### 8. Nothing learns

| loop | status |
|---|---|
| `recipe_use.jsonl` | written by `recipes.py:75`, **read by nothing** |
| `distill_queue.jsonl` | drained only by a manual `vfx_harness.agents.distill` call |
| retrieval | keyword scoring, fixed k=3, no floor |
| `skills.py` | reads scores, reports to a human, writes nothing |
| `_JUDGE_SD = 0.603` | measured on Fable-5; the critic is now Opus-5 |

Retrieval handed the *signage* layer `blackout-beat` and `animated-shutter-profile`, and
handed the *lighting* layer nothing at all.

`verify_recipes.py` boots headless Blender, executes each recipe, invokes every callable
and records a `code_sha256` — this is the strongest admission gate in the published
literature, and it is **not in any run path**. It is referenced only in prompt strings,
guardrail denial text, `skills.py` reporting, and tests. `recipe_index()` defaults to
`verified_only=False`, so `verified: true` is worth **+1 in a keyword score**, not a filter.

### 9. The plan is the only ungated artifact, and it is the largest source of waste

Attribution of every layer that thrashed, from this repo's own documents:

| layer | attempts | root cause | authored by |
|---|---|---|---|
| 4 sky | 1 | — | plan gave **12** numeric targets |
| 1 layout | 1 | — | plan gave **4** numeric targets |
| 3 city | 3 | done-check was a band statistic describing content the layer does not own | **plan** |
| 2 signage | 6, ~$88 | asked to build geometry **already in the mesh**, on a layer that shades rather than models, framed at y0.95 — the bottom edge of frame | **plan** |
| 5 lighting | 4, failed | **zero** numeric targets; no plate; rubric demands mentally subtracting emission | **plan** |

`docs/architecture/layer-references.md` states three of those defects in one sentence. `docs/research/findings/lighting.md`
prices one of them at **$78.76** for building a facade that was already 260,332 polygons of
mesh.

**The validation on `layers.json` is, in full: the file exists, and the judge list is
non-empty** (`ledger.py:114-131`). Nothing checks that every axis in `critic_axes.json` has
an owner, that a `reads:` string contains anything measurable, that referenced plates exist,
that judge frames are in range, or that a layer's owned axes are visible at those frames.

The commit log already knew. `1c0c9a2` *teach the plan agent the departments it kept
omitting* · `43f5aa9` *give lighting its own stage* (the original plan had none) · `39ff349`
*catch scope prose citing a layer which no longer exists* · `da89c13` *a one-sided target
gets optimised into a defect* · `ddd1213` *a band statistic is not a subject statistic*.
**Five of the last twenty-five commits patch the plan agent — the most-patched component in
the repo and the only ungated one.**

The README's thesis is *"every claim is checked against something objective."* Build has
canonical replay bound to a script hash; recipes have `verify_recipes`; checks have
known-bad fixtures. The plan has a second agent reading the first agent's prose. Two agents
agreeing is not a gate — it is the same failure mode as a critic scoring itself.

**This reorders the document.** A noisy verifier makes a good plan expensive; an
unmeasurable plan makes *any* verifier useless, because there is nothing to converge on.
Layer 5 did not fail because the critic was noisy. It failed because the plan never said
what "lit form" would measure as, so the noisy score was the only signal available.

### 10. A whole class of defect has no visual signature

| defect | what a beauty render shows |
|---|---|
| flipped normals | nothing — two-sided shading renders them fine |
| doubled vertices | nothing — pixel-identical |
| n-gons | nothing — flat now, shatters on subdivide |
| wrong scale | nothing — a 4× asset alone in frame is identical |
| interior geometry | nothing, by construction |
| hero occluded | nothing until you look for it |

Every one is a `bmesh` query costing zero renders. The pipeline has none of them. This is
the bug class of *"The asset was right the whole time; layer 1 was deleting it."*

---

## Part II — What the research settles

Compressed to the decision-relevant. Full sourcing is in the session transcript.

**The verifier is the ceiling, not the generator.** Test-time compute research, BlenderGym's
compute-allocation curve (⅓ verification at <30 queries, **¾ at >100**), and the memory
literature all land here independently. Layer 2 spent 46 rounds — deep in the high-compute
regime — with ~5% on verification.

**Pointwise Likert is the wrong instrument, worst in exactly this regime.** Pairwise vs
Likert ICC: **0.665→0.785** on high-variation data, **0.276→0.562** on low-variation. Iterating
one shot *is* the low-variation case. At ICC 0.276 the signal is close to noise.

**But pairwise is not a free win.** It flips ~35% under a distractor attack vs 9% for
absolute scoring, and order-flip rate is **0.94 when there is no real quality gap**. It
needs both orders, discarded inconsistencies, and an explicit tie. On a *strong* judge the
gain also shrinks: one controlled study found pairwise ≈ single-answer scalar at 85% human
agreement, with pairwise ahead by only ~4pp when ties are included. The measured gains are
largest for weak judges (+0.41 Spearman on Llama-2) and smallest for strong ones (+0.09 on
GPT-4).

**A 1–5 scale collapses to two effective levels.** Asked to score image quality 1–5, one
MLLM emitted **5 → 84%, 1 → 9%, 3 → 5%, and 2 and 4 under 1% each** — *"biased towards
positive, biased towards extreme, and with only two effective scales."* This is our data
too: `barrel_roll` scores are almost entirely 2 and 3, with 1 and 4 rare. The scale is
nominally five-valued and actually binary.

**The fix is reading the distribution, not the emitted token.** Softmax over the discrete
rating tokens beat argmax over the same tokens for **every model on every dataset** tested —
e.g. SRCC 0.101 → 0.442. We cannot do this directly (the Anthropic API exposes no logprobs),
but the sampling equivalent is available and we are already half-way there: the panel
samples up to 3 and takes a **median**. Take the **mean** instead, sample more, and report
the spread as confidence.

**Do not go all the way to binary.** Measured ICC against human: **0–5 → 0.853**, 0–100 →
0.840, 0–10 → 0.805. Binary was never tested. The optimum is a *coarse ordinal*, not two
values. This is compatible with decomposing a single criterion into binary sub-checks, which
is a different move and measured positive (~20pp agreement).

**Trial counts.** 3 trials → 90% consensus fidelity, 11 → 95%, hard items 15+. The panel
caps at 3 and only convenes when `_borderline` fires.

**Crop targeting is the bottleneck, not the crop.** Oracle bbox 0.752 · predicted bbox
0.550 · centre crop 0.490 · random crop 0.453 · no crop 0.443. **A bad crop is barely better
than none.** The scene graph puts us on the oracle row for free.

**Telling often beats showing.** Location as *text* +24.89pp vs the same location drawn on
the image +10.10pp. Visual prompt *alone* −10.6pp; visual **plus** textual +7.7pp. Every new
render mode needs a caption.

**Accept-if-better is statistically invalid.** Measured 72–100% false-commit rates when no
real gain is available. Named systems that do it: ADAS, Gödel Agent, DGM, Voyager. And
this ledger.

**Retry beats architecture, per dollar.** On HumanEval: temperature-ramp retry **93.2% at
$2.45** vs LATS 88% at $134.50 and Reflexion 87.8% at $3.90. Layer 2 passed on attempt 6 —
that *is* retry-N.

**Memory does not beat a budget-matched baseline.** Give a vanilla actor the same token
budget and it beat AWM/ASI/ReasoningBank on every backbone tested. Run-to-run variance was
±5–10pp, larger than nearly every claimed gain.

**Verbatim beats extracted.** LoCoMo 43.9% (chunks) vs 28.0% (LLM-extracted facts). The
build scripts, with measured numbers in their docstrings, are the better artifact.

**Measurement alone causes false convergence — both channels are required.** This is the
sharpest correction to the thesis of this document. In the cleanest published ablation of
the two, removing *vision* from a judge that also had exact kernel measurements blew mean
Chamfer error **1.42 → 49.68**; but the exact numbers *alone* produced results that were
**dimensionally correct and structurally malformed** — the authors' term is "false
convergence." Both were necessary.

Read that against our own commit `cad7f99`, *"flag measuring over looking."* The flag was
right, and it is only half of a two-channel system. Phase 4's deterministic gate is not a
replacement for the critic; it is the other half. A layer can satisfy every band and still
be wrong in a way only looking catches, and the reverse is the case we have been living in.

---

## Part III — The plan

Seven phases. Each has a gate; a phase that fails its gate stops rather than cascading.

### Phase 0 — Measure the instrument before changing it

Nothing here changes pipeline behaviour. All three answer questions that determine whether
later phases are worth executing.

| task | method | what it decides |
|---|---|---|
| **0.1 Blank-frame control** | Score each axis with the reference and a **black frame** | The language-prior floor. If `lighting_and_form` scores 2.0 blind, sixteen rounds of renders carried no visual information and Phase 2 is premature |
| **0.2 Recalibrate the noise floor** | `vfx_harness.evaluation.cli variance` crossed over judge model × prompt template × temperature × repetition; report ICC(2,1) and the between/within split | `_JUDGE_SD = 0.603` is a Fable-5 number. The real floor is likely 40–60% higher because the current estimate omits prompt and model variance |
| **0.3 Budget-matched retry-N** | Layer 5, four fresh attempts, forced approach divergence, **no repair loop, no approach review, no memory**, capped at the $18.63 already spent failing | Whether the machinery beats plain resampling. If it does not, Phases 4–6 are optimising the wrong thing |

**Gate:** if 0.1 shows an axis is not image-dependent, that axis's problem is the rubric,
and it goes to Phase 3 rather than Phase 2.

### Phase 1 — Checks that need no judgment — **SHIPPED**

Landed as a single `check_scene(kind=…)` tool in `src/vfx_harness/blender/tools.py:516` with
kinds `visibility · framing · motion · mesh · scale · passes · bbox`. Each is gated on a
**known-bad fixture** via `vfx evals checks`, which builds one deliberately broken scene
per check and fails if any stays silent — a check nobody has watched fail is not a check.
`bbox` returns the oracle crop box for `render_pass`, which is what makes Phase 2's zoom
land on target rather than guess.

Original spec kept below for reference.

```python
check_visibility(obj, frame)   # ray_cast from camera to bbox corners → visible fraction
check_framing(obj, frames)     # world_to_camera_view → NDC bbox, width, centre, horizon
check_motion(obj, frames)      # matrix_world per frame → velocity, accel, jerk; arc curvature
check_mesh(obj)                # bmesh: non-manifold, non-contiguous, degenerate, loose, poles
check_scale(obj)               # dimensions, scale == (1,1,1), unit system
check_passes(frame)            # beauty − Σpasses ≈ black; NaN/Inf/negative counts
```

`check_motion` first. `build/01_layout.py` already carries the right metric as prose:

> `travel: max speed 4.66 u/f (f24), max |accel| 0.39 u/f^2 - one unbroken move`

Computed by hand, written into a comment, verified by nothing. It *is* the `camera_framing`
axis, currently judged by showing a critic two stills.

**Gate:** each check must fire on a known-bad scene before it is trusted.

### Phase 2 — Let the critic see — **SHIPPED**

`_DISPLAY_H` is now **1024** with a comment stating why it and `_METRIC_H` answer different
questions. `render_pass` carries `pass · shade · light · crop · res_pct`, and every mode
returns a caption naming what to look for.

Two probes keep it honest, and both caught real defects rather than confirming the build:
`spike_render_modes.py` found passes rendering **bit-identical to beauty** under a caption
promising emission had been removed, plus a light-group isolation EEVEE never performs;
`spike_render_isolation.py` found a diagnostic mode leaking a world back into the scene,
**brightening every canonical render after it by 47.8/255**. That second one is the more
important result — a diagnostic tool that silently corrupts the measurement path is worse
than not having it, and it would not have been found by looking at the diagnostic output.

Remaining in this phase: `diff_frame` (chain with/without layer N) is partially wired and
needs the same fixture treatment.

Original spec kept below for reference.

**2.1 `_DISPLAY_H`.** Ship the render you already paid to compute. Keep `_METRIC_H = 320`
and its reasoning intact — add a comment explaining why the two differ, because this is the
kind of thing that regresses.

**2.2 `render_frame` gains four parameters:**

```python
render_frame(frame,
    pass  = "beauty|diffuse_direct|emit|shadow|ao|normal|depth|crypto",
    light = "<lightgroup>",
    shade = "beauty|clay|silhouette|matcap:<name>",
    crop  = (x0,y0,x1,y1), res_pct = 400)
```

Verified available on this machine: 30 render passes, light groups
(`vl.lightgroups.add()` + `light.lightgroup`), custom AOVs, Workbench matcaps including
`check_reflection_horizontal` (zebra) and `check_normal+y`, `material_override` (now honoured
by EEVEE in 5.x), and true optical zoom via `use_border` + `use_crop_to_border` +
`resolution_percentage` — measured at 768×2880 for a region, ~4× linear for ~1.2× the pixel
cost of a full frame.

Blender 5.x API breaks to handle: `scene.node_tree` → `scene.compositing_node_group`;
`media_type` must be set before `file_format`; `OutputFile.base_path/file_slots` →
`.directory/.file_output_items`; engine id is `BLENDER_EEVEE`, not `BLENDER_EEVEE_NEXT`.

**2.3 `subject_bbox(obj, frame)`.** Five lines, no render. This is what makes crops safe —
it moves us from the 0.550 predicted-bbox row to the 0.752 oracle row.

**2.4 `diff_frame(frame, layer)`.** Chain with/without layer N, subtracted. The ablation as
an image: black diff = the layer did nothing; energy outside its `owns` region = it did work
it doesn't own.

**Every mode ships with a caption** stating what the image is and what to look for. A visual
channel alone measured −10.6pp.

**Gate:** layer 5's existing script, re-judged on `diffuse_direct` cropped to the hero at
400%. If it passes, the layer was never the problem.

### Phase 3 — Gate the plan — **NEXT**

The plan is the only artifact in this pipeline that nothing checks, and Part I.9 attributes
every thrashing layer to it. It is also the cheapest thing to fix: all of the checks below
are `check_scene` plus the existing metrics, and **none of them costs a model call.**

**3.1 Structural validation of `layers.json` / `acceptance.json` / `critic_axes.json`.**
Today: file exists, judge list non-empty. Add, and refuse to build on failure:

- every axis in `critic_axes.json` has exactly one owner across all layers
- every layer owns ≥1 axis, and no axis is owned twice
- every `ref` path exists on disk
- every judge frame lies inside the shot's frame range
- layer ids are contiguous from 1, and `script` paths are unique
- no `reads:` prose references a layer id that does not exist (this is `39ff349`, which was
  fixed in the prompt rather than the gate — a prompt is not a gate)

**3.2 Every layer contract must contain at least one machine-checkable target.** This is
the correlation from Part I.3, stated as a rule: 12 targets → 1 attempt, 0 targets → never
passed. A layer whose `reads:` cannot be reduced to a measurement is not buildable, and the
plan stage should fail rather than hand the builder a σ=0.6 grade as its only signal.

**3.3 The pre-build contract check — the centrepiece.** Before layer N is allowed to build,
evaluate layer N's own targets against the chain built **without** layer N:

```
targets already pass   → the plan asked for something already present
targets not evaluable  → the contract is unmeasurable
subject not in frame   → the plan asked for something invisible at the judge frame
```

Each branch corresponds to a failure this project has already paid for:

| branch | what it would have caught | cost avoided |
|---|---|---|
| already pass | layer 2 asked to build a facade already present as 260,332 polys | **$78.76** |
| not evaluable | layer 5's contract had zero numeric targets | 4 attempts, never passed |
| off-frame | layer 2's podium specified at y0.95, the bottom edge of frame | folded into the above |

This is the same necessity test as Phase 5's gate, moved upstream of the money. The gate
asks *"did removing this layer degrade what it owns?"* after building; the plan check asks
*"would this layer's targets already hold before it builds?"* — the same measurement, and
the answer is free.

**3.3b Every stated number must be re-derivable from the artifact it claims to describe.**
— **SHIPPED.** `vfx evals grounding`, `src/vfx_harness/evaluation/grounding.py`.
`measure_ref`'s own description says *"use these MEASURED numbers as the plan's look targets
— never invent fingerprint values."* Nothing enforced it. Enforcing it is one loop over
`acceptance.json`, no model call and no Blender.

It re-derives each claim through **the same functions `measure_ref` itself ran** rather than
reimplementing the measurement, which would only test the checker against itself. Numbers no
rule recognises are reported, never skipped — a checker that quietly ignores what it does not
understand reports "all grounded" while checking nothing, which is the failure class it
exists to catch.

Run across both shots 2026-08-18: **153 of 160 claimed values reproduce within 15%.** The
plan agent did follow the instruction. And the finding is sharper than the pass rate: **all
seven failures are the same metric.** Every `mean`, `black%`, band `μ/σ`, `detail` and
`light points` claim in both shots reproduces. Only `halation` does not, in 7 of 15 moments.
That is not a planner that invents numbers — it is one instrument that cannot be read, which
is a conclusion a single shot could not have supported.

**3.3c A target is only as real as the metric under it. Perturb the measurement before
accepting the target.** `halation` on `refs/f045_pullback.jpg`, varying only the prep width:

| prep width | hot px (≥240) | halo px | halation |
|---|---|---|---|
| 320 | **0** | 799 | **0.0** |
| 480 | **1** | 1799 | **1799.0** |
| 512 | **0** | 2022 | **0.0** |
| 640 | **0** | 3155 | **0.0** |
| 768 | **0** | 4538 | **0.0** |
| 960 | **4** | 7078 | **1769.5** |

`metrics.py:175` is `halation = halo / hot if hot else 0.0`. On this image the denominator is
**between 0 and 4 pixels**. One pixel crossing the 240 threshold swings the output from 0.0
to 1799. Three separate defects:

- **undefined is reported as zero** — "no hot cores present" and "no halation" are the same
  output, so the metric cannot distinguish a dim frame from an unmeasurable one
- **discontinuous in resolution** — 0.0 at four of six widths on a frame the plan calls
  *"the shot's peak"* halation
- **unstable ratio** — as `hot → 0` the value explodes. This is the cause of the
  `halation_mid 1963 vs ref 0 (245375%` reading already recorded in the comment at
  `metrics.py:225`; that symptom was noted, this is the mechanism.

`halation_bleed` is a critic axis owned by layer 9 and has a stated target in all ten
acceptance moments. **The plan's 2340.1 is not reachable from this image at any width**, so
no build could have satisfied it, and a build that "achieved" 1799 would have done so by
moving one pixel.

*Correction to an earlier draft of this section:* it also said halation "sits in `SPEC` where
it can decide a moment outright." That is wrong and I should have checked before writing it.
All four `halation*` entries are `blocking=False`, and `acceptance.py:107` short-circuits the
critic only on **blocking** deltas — so halation never decided a moment by itself. Its damage
was via `guardrails.py:272`, where `report()` becomes advice text the builder acts on. The
defect was real; the severity claim was not.

**FIXED**, in both implementations — there were two. `metrics.py` and
`blender/tools._region_metrics` each carried their own copy of `halo / hot if hot else 0.0`,
and the second is the one the *builder* reads during a build, so fixing only the first would
have left the live path broken.

- undefined is now **absent** rather than `0.0`. `compare()` already skipped absent keys, so
  no caller changed — and "say nothing" is the correct response to an unmeasurable quantity
- the floor is a **density** (`_HOT_FLOOR_PPM = 500`), not a pixel count, so the answer does
  not depend on image size — a raw count had the two modules disagreeing about one plate
- 500 ppm is derived twice and the derivations agree: sampling noise `1/√hot ≤ ¼` of the
  metric's own 0.50 tolerance gives `hot ≥ 64` at the 960 grid (= ~500 ppm), and across all
  15 references the readings fall into two disjoint groups — `hot ≥ 133 → 2.3..24.1` and
  `hot ≤ 56 → 0.0 or 94..1770` — whose empty gap contains it
- a new always-defined `hot_core` carries the signal suppression would have eaten: "the
  render has no blown core where the reference has one" used to surface *by accident* as
  `halation 0.0 vs ref 11.5`, and `clipped_pct`'s 1.0-point floor is too coarse to catch it
- `_metrics_line`'s advice now distinguishes **no core to bloom from** ("raise emitter or key
  intensity until something blows out, THEN judge bloom") from **core present, glow too
  tight** ("crank bloom"). It previously said "crank bloom" to a builder whose scene had
  nothing bright enough to glare, where bloom multiplies zero

Residual, measured rather than assumed: 9 of 10 plates land on the same side of the floor at
both 960 and 2048; `f100_city` reads 486 and 669 and straddles it. A ≥240 threshold is
nonlinear under resampling, so downscaling averages a small core with dark neighbours and
pushes it under — plates well clear of the floor agree within ~2%, only sparse ones move. A
borderline plate reported as borderline is the behaviour being bought; the old failure was
one plate reading both 0.0 and 1769.5.

The rule this generalises to: before a numeric target is accepted, re-measure it under
perturbation — prep width, and for renders also sample seed. A target whose metric is not
stable under perturbation is not a target.

**3.4 Targets are bands, not one-sided thresholds** (`da89c13`, learned the hard way), and
are subject statistics rather than frame-band statistics where the axis is about a subject
(`ddd1213`, also learned the hard way). Both are currently prompt guidance. Move them into
3.1's validator.

**3.5 Plate provenance.** Layers owning *absolute* targets need a plate; layers owning
*relational* targets do not (see 4.2). The plan stage must declare which kind each layer
owns, and fail if an absolute-target layer has no plate. Layer 5 owns relational properties
almost exclusively — its contract is extractable from `refs/f045_pullback.jpg` with no plate
at all.

**Gate:** re-run the validator against the existing `barrel_roll` plan. It must reject
layers 2, 3 and 5 for the reasons in Part I.9. A validator that passes the plan we already
know is broken is not a validator.

### Phase 4 — Ask a better question

**3.1 `compare_pair(a, b, axis, ref)`** — one axis, both orders, explicit tie, returns verdict
plus a consistency flag. Treat a flip as **abstain**, not noise. Keep absolute scores for
logging only.

**3.1b Aggregate by mean, not median, and sample the distribution.**
`src/vfx_harness/agents/builder.py:954` takes the median of the panel's means, commented
*"median resists the outlier"*. With no logprobs available, repeated sampling is our
only estimate of the judge's distribution, and the median discards exactly the information
that estimate carries — a mean expresses "4.5 when torn between 4 and 5". Measured: greedy
0.635 → 5-sample mean 0.666. Trial counts to match a 50-trial reference: **3 → 90% fidelity,
11 → 95%, hard items 15+**. Where a verdict matters, buy trials rather than a better prompt.

**3.2 Numeric bands in the layer contract.** Replace prose `reads:` with measurable targets.
Two kinds, and only one needs a plate:

- **Absolute** (band means, black %, light-point counts) — change as later layers composite,
  so they need a plate showing the world *at that layer*. This is why layers 3 and 4 have
  plates.
- **Relational** (hero:neighbour ratio, facade σ, exposure ordering) — survive composition
  and can be read off the **final reference directly**.

Layer 5 owns relational properties almost exclusively. **Its contract can be extracted from
`refs/f045_pullback.jpg` without ever making an L5 plate.**

**3.3 Bands come from the noise floor**, not from taste — run the script twice, measure the
spread, set the band wider. `src/vfx_harness/evaluation/determinism.py` already does most of this.

Every renderer test suite hits our exact problem — Monte-Carlo variance makes exact diffs
useless, and a tolerance loose enough to survive it is too loose to catch a regression. The
conventions that survived, in rough order of how widely they are used:

- **Two levels, always: a per-pixel tolerance *and* a percent-of-pixels allowed to fail.**
  Blender's own render tests are one call — `oiiotool ref.png out.png --fail 0.016
  --failpercent 1 --diff` — with per-directory relaxations that price in noise (volume
  0.048 @ 3%, light/camera 0.02 @ 4%).
- **Their written policy is the part to copy: raise `fail_percent` before raising the pixel
  threshold.** Loosening the per-pixel bound hides real breaks; allowing more noisy pixels
  does not.
- **A hard-fail cap** on top, so noise can never conceal one catastrophic pixel.
- **Local block error, not just global.** Unreal scores failing-pixel *fractions* over a
  fixed 10×10 grid, which catches a localised break that a global percentage dilutes.
- **Multiple accepted references** rather than one golden plus slack (OSL globs `ref/*`; any
  match passes). Cheaper and more honest than widening a band.
- **Blocklist the nondeterministic test rather than loosening the gate for everyone.**
- **Or drop the threshold entirely**: Mitsuba stores a reference *variance* image alongside
  the mean and runs a per-pixel z-test, `z = |mean − ref| · sqrt(spp / ref_var)`,
  Šidák-corrected, passing if ≥99.75% of pixels clear. There is no tolerance constant at
  all. This is the most principled option and the one closest to what our bands should be.

Two tools worth taking rather than writing. **FLIP** (`pip install flip-evaluator`, CPU, BSD)
is the only perceptual metric built for *renders* — calibrated on aliasing, fireflies,
banding and denoiser smear rather than JPEG — and returns both a per-pixel error map and a
pooled scalar. **DISTS** (via `pyiqa`, accepts file paths directly) is the full-reference
metric least likely to fire on Monte-Carlo noise while still catching structural change.
Avoid butteraugli and SSIMULACRA2 here: both are calibrated on *compression* artifacts and
will score two correct renders at different sample counts as heavily degraded.

**Gate:** every axis must be classified as measurable or not. The unmeasurable residue —
my estimate is two of eleven — is deferred to end-of-shot judgment and routed back through
`owns`.

### Phase 5 — Replace the score with a gate

```
PASS(layer N) =
  conformance      every metric N owns is inside its band
  non-interference every metric N does NOT own moved less than tolerance
  necessity        removing N degrades N's owned metrics beyond the noise floor
  reproducibility  canonical replay from empty reproduces all three
```

No VLM anywhere in that. **Non-interference is free** — the metric set is fixed and `owns`
already exists, so every unowned metric becomes a "must not move" constraint with nobody
writing anything.

**Necessity is the answer to "did this layer do all its work."** The `ablation` field exists
and currently holds a boolean and a sentence. Make it a measured delta and it becomes the
gate: pull layer 5, and if facade σ doesn't move, it didn't do its job regardless of any
score.

Also: **accept-if-better must become a statistical decision.** A single passing sweep is not
evidence.

**Gate:** re-run layers 1–4 under the new gate. If a layer that passed now fails, understand
why before proceeding.

### Phase 6 — Loop control

- **Oscillation detection** — hash `(state, tool, args)`; identical triples or A→B→A
  parameter swings are stuck signals. Heuristic detectors measured 60.1% joint accuracy at
  $0 against 4.7–11% for frontier models reading the same traces.
- **Replace `plateau = mean <= prev_mean`** with embedding stability plus quality plateau.
  One approach review per layer, ever, is not enough.
- **Budget as pre-call reservation.** `MAX_BUDGET_USD` checks after a call returns, so it
  always admits one overshooting call. Structural counters overshoot dollars by up to 1395%.
- **Early abort.** 28–64% of tokens saved on failed trajectories for 1.6–4.2pp of success
  rate. Failed runs cost ~2× and take ~2× the steps.
- **A parameter→measurement table per layer**, so "key energy 8e5 → facade mean 210" is a
  data point rather than a forgotten turn. This is what kills the oscillation.

### Phase 7 — Learning (only after the verifier works)

Nothing here is estimable until Phase 3 lands. At a 17% verdict-flip rate, no item's utility
can be measured.

- **`verify_recipes` becomes an admission gate**, not a manual audit. Retrieval filters on
  `verified`, rather than adding +1 to a keyword score.
- **Outcome-conditional-on-retrieval.** Join `recipe_use.jsonl` against verdicts. This is a
  metric no published system reports and the plumbing already exists. (At n=5 it is currently
  degenerate — every layer that pulled anything passed, and the one that pulled nothing failed.)
- **Retrieval by embedding with a hard floor.** Return **nothing** rather than a near-miss —
  hard distractors cost 6–11 points while random irrelevant passages cost ~nothing.
- **Quarantine + k-of-n confirmation** before an item goes live. Counter to lucky-roll
  poisoning, which is the non-adversarial version of a measured attack class.
- **Delete on retrospective utility.** Add-everything measured 55.48% vs strict-admission
  70.95%.
- **Retrieve verbatim build scripts**, not distilled prose. Keep any recipe alongside, never
  instead.
- **Seed, don't bootstrap.** The 24 hand-verified recipes are the seed; protect them.
- **Keep a permanent no-memory control arm.**

### Phase 8 — Structural (largest change, least evidence)

Deferred deliberately.

- The chain is strictly serial with a hard stop, so one hard layer blocks four downstream.
  Real pipelines run lighting and FX concurrently. The dependency model encodes *render
  order* as *work order*.
- Plan is fixed at the start with no replanning path. Dynamic replanning measured +10.31pp
  as the largest single ablation gain — but a *naive* planner made things **worse than no
  planner** (36.97 → 20.60). This cuts both ways and needs its own experiment.
- A `spike` primitive the harness invokes itself when a layer stalls twice. This is what
  found the sun-in-volume-scatter trap — by hand, after $78.76.

---

## Part IV — What we are not building

Saves work, and each has evidence against it.

| not doing | why |
|---|---|
| Super-resolution before the critic | Bicubic beat **every** SR model on TextVQA (33.2 vs 28.1–31.5); bicubic also hallucinates least |
| Contact sheets for A/B comparison | −3 to −34 pts everywhere except multi-view reasoning |
| Marked-up renders as the *only* input | GPT-4o dropped 66.0 → 49.0 on V\*; keep the clean frame alongside |
| Overlaying anything we would not bet on | Measured on the same model and task: a **ground-truth** region overlay scored **+10.1** on the binary call; the **same overlay from a real detector scored −4 to −12.6**. An imperfect mark converts detector false positives into judge false positives, because the judge trusts the mark |
| Filled masks | GT contour **+10.86** vs GT filled mask **−1.05** on the same information — a 12pp swing purely from occluding rather than outlining. Filled masks were also the worst prompt type in an independent visual-prompt ablation. Outline, never fill |
| Overlays for whether-questions | Every real overlay tested improved *localisation* (+2.5 to +7.4) and hurt *discrimination* and *description*. Our gate is a whether-question |
| More camera angles as the lever | 1→4 input views bought **≤0.012** on a 12,963-triplet benchmark. **Legibility is the lever, not view count** — unique face colours, axis/bbox overlays, numeric markers and dimensioned orthographics all measured larger than adding cameras |
| False colour / contrast stretch as a judge aid | Essentially unmeasured on frontier VLMs, and pixel magnitude correlates **r = −0.08** with detection difficulty |
| Grid overlays for fine geometry | One model got 17× worse; 16×16 makes models fail outright |
| Batch-ranking >2 candidates | Position robustness falls below 0.5 at 3–4 options |
| 9-view montages | SRCC peaks at 6 views and declines by 16 |
| Normals in the look critic | RGB+normal degrades texture judgement 25% relative — route to a geometry-only call |
| Judge ensembles across model families | A 9-judge panel has effective sample size ~2.0–2.5; fusing three VLM judges scored *worse* than the single best |

---

## Part V — How we will know it worked

The current baselines are single-run point estimates (`best: {round: 1, mean: 4.0}`), which
cannot resolve a change against σ=0.603. Most of the 40+ harness commits were evaluated
against measurements that could not resolve them.

**Switch the primary metric to judge-independent quantities** you already log:

```
rounds-to-pass · attempts-to-pass · cost-to-pass · wall-clock-to-pass
```

Step-efficiency is the most robust finding across the entire agent literature precisely
because it is measured on the same tasks with the same evaluator. Layer 2 at 6 attempts /
46 rounds / $88 and layer 5 at 4 attempts / 16 rounds / $18.63-and-failing are unambiguous.

Secondary, once Phase 0.2 lands: **effect sizes with confidence intervals, ≥3 runs.** A
single-run delta under ~10% is inside noise.

**Permanent control arms:** no-memory, and budget-matched retry-N.

---

## Sequencing

```
Phase 0 ✔ ──┬──▶ Phase 1 ✔  (independent)
            └──▶ Phase 2 ✔ ──▶ Phase 3 ──▶ Phase 4 ──▶ Phase 5 ──▶ Phase 6
                             (gate the plan)                            │
                                                                        ├──▶ Phase 7
                                                                        └──▶ Phase 8
```

Phase 3 gates 4 and 5 rather than merely preceding them: Phase 5's conformance/necessity
gate has nothing to check until the plan emits measurable targets, and Phase 4's pairwise
judging still needs to know which axis it is comparing on.

Phase 1 depends on nothing and should start immediately. Phase 2 is gated on Phase 0.1.
Phase 6 is gated on Phase 3 landing, because utility is unmeasurable through the current
noise. Phase 7 is gated on everything.

## Open questions

1. **Where do layer plates come from?** They are hand-made PNGs today. Phase 3 reduces the
   need — relational targets need no plate — but layers owning absolute band statistics still
   do. Unresolved whether these can be generated or must stay a human input.
2. **Is the serial chain right at all?** Phase 7. The evidence on adaptive vs static
   decomposition cuts both ways.
3. **How much of the residue is genuinely unmeasurable?** Estimated two of eleven axes.
   Phase 3's classification will settle it.
4. **Does the critique text carry any value beyond the pass/fail bit?** One measured result
   found a coin-flip judge retained ~81% of a memory system's gain, and another found
   removing judge feedback entirely left degradation unchanged. Worth an ablation in Phase 3.
</content>
</invoke>
