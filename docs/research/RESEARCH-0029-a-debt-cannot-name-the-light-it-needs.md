# RESEARCH-0029 — a judgment debt cannot name the illumination it needs

Status: cause established, mechanism undecided. Recorded so the evidence survives the
session that produced it. Three shots contributed; two candidate mechanisms are open and
one is refuted.

## What happened

`caesar_curia` layer 2 built seven units, all PASSED, and the layer then failed to
finalize. Exit 9.

    composed canonical owes independent reference judgment for due debt
      jd-5a80dc98… · render mode eevee
    candidate 2@f1_finalization_group_0_canonical_f1.png has no optical signal
      (stddev=0.072, edges=0.037, range=1)  — not calling [the critic]
    LAYER VERDICT — layer 2 terminal finalization is 'failed', not 'passed'

The debt is permanently unpayable. Three payment attempts, all no-signal; per HIR-0163 a
no-signal plate correctly never invokes a critic, so each attempt is append-only and the
debt stays `due` forever. The harness refused to judge a black frame rather than pass it,
which is the design working.

## Why the plate is black — measured, not inferred

    16 accepted build scripts, scanned for
      bpy.data.lights | lights.new | type='(SUN|POINT|AREA|SPOT)'
      emission | emissive
      bpy.data.worlds | scene.world | worlds.new
    -> light=0  emission=0  world=0  on every one

Agrees with the builder's own in-worker report, `lights=(none) and world=(none) across the
entire replayed scene`. That report is a direct read: `blender/worker.py:1481` is
`world = sc.world`, so `(none)` is a measurement and not an untagged default.

Layer order is not incidental. `jit.depends_on_layers` is `1<-2<-3<-4<-5`, strictly linear,
with the only lighting producer last. Layer 2's replay prefix is layer 1 forever, so no
layer-scoped transaction can put illumination in front of it.

## Root cause

The debt vocabulary cannot express a dependency on illumination.

    domain/judgment_debt_models.py:20   RENDERED_CARRIER_FAMILIES = {mesh, volume, compositor}
    domain/image_signal.py:27           IMAGE_SUBJECT_FAMILIES    = {mesh, volume, compositor}
    domain/image_signal.py:26           IMAGE_SIGNAL_FAMILIES     = {light, shading, volume, compositor}

`RENDERED_CARRIER_FAMILIES == IMAGE_SUBJECT_FAMILIES`, exactly. `light` and `shading` are
not merely absent from the carrier set — they are actively refused, at
`judgment_debt_models.py:247, 353, 441, 738`. A planner that wanted to write "this debt
needs a light in its prefix" has no way to say it.

Stated against the write-family vocabulary, which is where the asymmetry is visible:

    write families  {camera, compositor, control, keyframe, light, mesh, shading, volume}
    debt carriers   {         compositor,                        mesh,          volume}
    excluded        {camera, control, keyframe, light, shading}

Three of those exclusions are correct — `camera`, `control` and `keyframe` carry no
rendered subject and emit no light. Only `light` and `shading` are wrongly
unrepresentable, and they are exactly the two `IMAGE_SIGNAL_FAMILIES` already names. The
vocabulary that could distinguish them exists, in a third module, unused by the debt.

Framing (vfx-harness-4d): HIR-0110 required an optical-signal family in the prefix;
HIR-0160 added the subject-carrier requirement. **Judgment-debt activation inherited only
the subject half.** One existing rule, one code path that never received it.

Consequences, both confirmed by reading:
  - `orchestration/jit_materialization/judgment_authority.py:94` filters providers to
    `cluster.instrument_family in RENDERED_CARRIER_FAMILIES`, so a `light` or `shading`
    unit produces no `JudgmentProvider` row and activation never sees it.
  - `domain/judgment_debt_activation.py` contains `observation_medium` zero times, so
    `eevee` and `workbench_solid` activate identically.

Note the second is the smaller half: consulting the medium changes nothing while no debt
may declare a signal carrier.

## Upstream: the gate checks coverage, not satisfiability

`evaluation/plan_gate/meta.py:617,627` compares a requirement's declared `evidence_domains`
against the owner layer's declared domains — a declaration-vs-declaration subset check.
Caesar's L2 declares `image`, so coverage holds and the gate passes CLEAN. Nothing asks
whether L2's prefix can produce an image.

Measured across two shots, three draws, six plan transcripts:

    evidence_domain ~660 mentions   image ~390   optical / signal / payable  0 / 0 / 0

The verify pass reasons about domain coverage exhaustively and about domain
satisfiability never.

## Three shots, and only one is broken

    room_1046      image evidence routed to the layer that can JUDGE it       payable
    hansa_silk     illumination emissive, INSIDE the image-owning layers      payable
    caesar_curia   illumination in a LATER layer than the image               unpayable

Two ways to be sound, one way to be broken, and the gate distinguishes none of them.
Neither passing shot passes because a gate checked it; both pass by authorship. A
predicate must therefore accept both shapes or it will refuse a working shot — an
early formulation of this check ("a `light` family must exist in the prefix") would have
refused hansa's emissive design, which renders.

Not a brief-structure problem: room's brief has `## Lighting and materials` after its
geometry sections, lighting last at L4 of 4, structurally identical to caesar's.

## Candidate mechanisms

1. **Declared optical-signal capability at plan time.** Extend the `jit.provides`
   vocabulary; a layer declaring `image` must reach a signal provider in its dependency
   closure or declare it itself. The machinery exists — `plan_gate/contracts.py:147-192`
   already builds a capability closure over `jit.depends_on_layers` and refuses a layer
   whose closure lacks `camera`, naming the repair. Decidable at plan time *because it
   tests a declaration*. Note no layer currently declares anything but camera:
   `jit.provides` is `{}` for every non-camera layer in both caesar draws.
   Status: open. Schema change to plan authority — ADR territory.

2. **Signal-aware activation.** Compile signal witnesses alongside carrier providers and
   use them in the existing topological predicate at `judgment_debt_activation.py:70-79`,
   so an `eevee`-medium debt activates at the first prefix that can carry it.
   Correct form is "prefix **or own units**" (vfx-harness-7b): hansa's `hero_facade`
   builds the Emission node *and* owns the layer's image debts, so a prefix-only rule
   would defer its payable debt past the layer that still holds mutation authority —
   turning a payable debt unpayable. Inherits `image-subject-bootstrap`'s existing
   closure-membership shape.
   Deferring does not break payer binding: `activates_at` and the payer set are computed
   together (`judgment_debt_models.py:743` `for_definition`, taking
   `definition.binding.activates_at` at `:754`), so a later activation yields a longer prefix
   that strictly contains the earlier one.
   Status: open. Prerequisite: the carrier vocabulary must admit signal families first,
   across all nine gates (below), or there is nothing for the predicate to match.

3. **A field on the seed. Refuted.** `JudgmentDebtSeed.as_dict()` includes
   `carrier_families` and feeds `seed_digest`, so any additive field changes the digest of
   every existing seed — HIR-0208, a sealed record that still parses and stops verifying.
   `debt_id` is safe (its identity dict at `:253` excludes carriers and medium); the seed
   digest is not. Would require a `DIGEST_SCHEMA` bump and `vfx migrate-digest-schema`.

## The widening has nine gates, six modules, two names

Any change admitting signal families must move all of these together. Two are easy to
miss: the ninth is under a different name in a different package, and the eighth writes a
durable surface.

    domain/judgment_debt_models.py:247, 353, 441, 738
    domain/judgment_debt_activation.py:95
    orchestration/jit_materialization/judgment_authority.py:94, 208
    domain/layer_finalization_projection.py:312          <- durable projection
    blender/observation_environment.py:50                <- named CARRIER_FAMILIES

`OBSERVATION_MEDIA` is likewise duplicated across `judgment_debt_models.py:21` and
`observation_environment.py:16`, with different iteration order.

## What would have caught it, and when

    plan time     a declared capability closure                    $0
    layer start   read the accepted prefix scripts for light /
                  emission / world -- no Blender needed, the
                  builder already replays those scripts            $0, before any unit spends
    build time    what actually happened                           $42.70 on layer 2

The layer-start read is worth noting separately: unlike the camera-relative checks
(`clip-w`, `bbox_*`, projection) this question needs no Blender. Existence of a light,
an emission shader or a world is answerable from accepted script text and the DAG, and
that static answer agreed exactly with the in-worker `inspect_scene` report here.

## Evidence

    artifacts/_archive/caesar_curia-20260905T130421Z-unpayable-debt/
      state/judgment-debts.jsonl              jd-5a80dc98…, state `due`, payer dais_shell
      state/judgment-payment-attempts.jsonl   3 attempts, all no-signal
      build/units/**                          the 16 scripts scanned
    artifacts/_archive/caesar_curia-fixtures-image-debt-activation/
      draw1-layers.json   6 layers, lighting L6, 5 image layers stranded
      draw2-layers.json   8 layers, lighting L8, 6 stranded
      README.md

Three CLEAN-but-stranded plans of one shot at three granularities, plus room's and
hansa's bundles as two structurally different passing cases. A fix satisfying both
passing shapes cannot be shaped around caesar.

Cost of establishing this: $54.68 across one abandoned build and two rejected plan draws.

## Caveat for a layer-start implementation

Do not build the layer-start read on the deferred-row forecast path. It carries geometry
guards that a signal check must not inherit — `jit_materialization/validate.py:620,732`
and `evaluation/plan_gate/evidence_coherence.py:652` all gate on
`"geometry" in unit.provides`. Use the activation ids directly (vfx-harness-4d).

Stated precisely, because the nearby guard works the other way and is easy to conflate:
`jit_materialization/judgment_authority.py:96` is **additive**, not a filter —

    families = {cluster.instrument_family for cluster in write_clusters(...)
                if cluster.instrument_family in RENDERED_CARRIER_FAMILIES}
    if "geometry" in unit.provides:
        families.add("mesh")

a unit declaring `geometry` gains `mesh` even where its write clusters do not derive it.
That line does not silence non-geometry layers; the `validate.py` and
`evidence_coherence.py` guards are the ones that would.

## A fix is not exempt from the defect it fixes

This finding's own remedy carried an instance of the class it belongs to. The merged
`a0404a1` derives "states that can reach `retryable`" from `TRANSITIONS` to tell an
operator which transaction clears an unclaimable unit. `unit_state_claims.py` already
held that set, hand-listed, gating `release_unclaimed_unit_for_retry` — the very command
the new advice names. Byte-identical, same package, nothing making them agree. Had they
drifted, the harness would have printed `vfx units retry` for a state retry then refuses:
advice resolving to a refusal, worse than the traceback it replaced.

Two of the day's eight duplicate-derivation defects were introduced *by fixes*: this one,
and HIR-0199, which installed a turn counter that overshoots its budget by up to 38 in
place of one that overshoots by at most 3.

The generalisation (vfx-harness-4d): **a change that introduces a derivation is as likely
to introduce a duplicate as any other change, and less likely to be checked for it,
because the reviewer is checking whether the fix works.** Worth a pass specifically for
new derivations at review time, separate from whether the fix is correct.

## A note on this note

Every line number above was re-verified against `cc49c24` by reading the line, not by
trusting the message it came from. Two were wrong on the first pass — both relayed from
another session's report. That is the same failure this file documents one level up: a
true statement, taken from a description rather than the artifact, that stops being true
where it matters.

**And that verification pass itself introduced an error while removing one.** Correcting
`judgment_debt_models.py:742` (a decorator line, off by one) I wrote `:725`, which is the
`payer_layer: str` field declaration — a different construct entirely, not the assignment
the claim rests on. Caught by the session whose number I was correcting. The citation is
now `:743` for the method and `:754` for the assignment, both read.

That is three times in one day that a correction carried the defect it was correcting:
HIR-0199 installed a turn counter worse than the one it replaced, `a0404a1` introduced an
eighth duplicate of the rule it derives, and this pass replaced a wrong line number with a
differently wrong one. The mechanism (vfx-harness-4d) is not carelessness: **attention goes
to the thing being fixed, and the fix's own new content gets the attention a first draft
gets, not the attention a review gets.**

A related instance from the same night, worth recording because it is about how this
analysis was produced rather than about its subject: three sessions reasoned from a
feasibility model that had already been falsified on the shot in question, and none
questioned its premise — including one who had read another's account of that exact error
an hour earlier. Reading about a failure mode does not confer immunity to it.
