# The verifier is the pipeline

**Status:** plan, not built. Evidence is from the `barrel_roll` run of 2026-08-15/16, from
probes run against Blender 5.2.0 LTS on this machine on 2026-08-18, and from a literature
sweep of the same date. Measured numbers are marked; inferences are marked as such.

Nine layers were planned. Five were attempted. Layer 5 has never passed. Layers 6–9 have
never been built. The final attempts alone cost $47.48; layer 2's five discarded attempts
cost a further $78.76 by `docs/LIGHTING_FINDING.md`'s own accounting.

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

Same render, same reference, same critic model, from `evals/variance/barrel_roll`:

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

And `pipeline/blender/tools.py:150` sets `_DISPLAY_H = 512`, so a 1920×960 render ships to
the critic at 1024×512 = 703 tokens. The full render passes Claude's high-res tier
**unresized** at 2415 tokens, for $0.012. The adjacent `_METRIC_H = 320` is correct and its
reasoning is sound; the two constants answer different questions and the reasoning for one
leaked into the other.

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
| `distill_queue.jsonl` | drained only by a manual `pipeline.distill` call |
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

### 9. A whole class of defect has no visual signature

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

---

## Part III — The plan

Seven phases. Each has a gate; a phase that fails its gate stops rather than cascading.

### Phase 0 — Measure the instrument before changing it

Nothing here changes pipeline behaviour. All three answer questions that determine whether
later phases are worth executing.

| task | method | what it decides |
|---|---|---|
| **0.1 Blank-frame control** | Score each axis with the reference and a **black frame** | The language-prior floor. If `lighting_and_form` scores 2.0 blind, sixteen rounds of renders carried no visual information and Phase 2 is premature |
| **0.2 Recalibrate the noise floor** | `pipeline/evals.py` crossed over judge model × prompt template × temperature × repetition; report ICC(2,1) and the between/within split | `_JUDGE_SD = 0.603` is a Fable-5 number. The real floor is likely 40–60% higher because the current estimate omits prompt and model variance |
| **0.3 Budget-matched retry-N** | Layer 5, four fresh attempts, forced approach divergence, **no repair loop, no approach review, no memory**, capped at the $18.63 already spent failing | Whether the machinery beats plain resampling. If it does not, Phases 4–6 are optimising the wrong thing |

**Gate:** if 0.1 shows an axis is not image-dependent, that axis's problem is the rubric,
and it goes to Phase 3 rather than Phase 2.

### Phase 1 — Checks that need no judgment

Independent of everything else. Can land immediately. Each is 20–40 lines.

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

### Phase 2 — Let the critic see

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

### Phase 3 — Ask a better question

**3.1 `compare_pair(a, b, axis, ref)`** — one axis, both orders, explicit tie, returns verdict
plus a consistency flag. Treat a flip as **abstain**, not noise. Keep absolute scores for
logging only.

**3.1b Aggregate by mean, not median, and sample the distribution.** `build_agent.py:921`
takes the median of the panel's means. With no logprobs available, repeated sampling is our
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
spread, set the band wider. `pipeline/eval/determinism.py` already does most of this.

**Gate:** every axis must be classified as measurable or not. The unmeasurable residue —
my estimate is two of eleven — is deferred to end-of-shot judgment and routed back through
`owns`.

### Phase 4 — Replace the score with a gate

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

### Phase 5 — Loop control

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

### Phase 6 — Learning (only after the verifier works)

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

### Phase 7 — Structural (largest change, least evidence)

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
Phase 0  ──┬──▶ Phase 1  (independent, can run in parallel)
           │
           └──▶ Phase 2 ──▶ Phase 3 ──▶ Phase 4 ──▶ Phase 5
                                              │
                                              └──▶ Phase 6
                                                        │
                                                        └──▶ Phase 7
```

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
