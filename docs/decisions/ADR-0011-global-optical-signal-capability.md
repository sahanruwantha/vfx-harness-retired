---
id: ADR-0011
title: Optical signal is a globally declared capability, like camera
status: accepted
date: 2026-09-06
supersedes: null
---

# Optical signal is a globally declared capability, like camera

## Context

`GLOBAL_SCENE_CAPABILITIES` is `{"camera"}`. A sparse layer declares `jit.provides` as
capability -> reserved-role selectors, every judged layer's transitive closure must contain
camera, and a materialized camera unit must mutate one of that layer's exact reserved
camera-interface roles (ADR-0005, HIR-0086, HIR-0098). Camera availability is therefore
proved **at global publication**, before any layer materializes and before any builder
budget is spent.

Optical signal has no such proof. `UNIT_PROVIDES` gained `illumination` in
[HIR-0234](../improvements/HIR-0234-shading-is-a-light-modifier-not-a-light-source.md), so a
*unit* can declare that it is the light. But `image-signal-bootstrap` (HIR-0110) and
`image-subject-bootstrap` (HIR-0160) evaluate a unit's own derived write cluster, its
same-layer dependency closure, and *earlier materialized layers* — all of which exist only
at just-in-time materialization. The global DAG cannot state that a layer owing image debt
has a lighting provider anywhere before it.

The observed cost, three findings:

- **HIR-0110**: look labels, role names, object counts, geometry, camera, controls and
  keyframes were self-certifying optical signal. Fixed at the JIT gate.
- **HIR-0160**: a shading-only root on a camera-only scene could owe beauty with no
  rendered carrier. Fixed at the JIT gate.
- **HIR-0234**: `hansa_silk_road` layer 2 authored four unpayable image debts **on two
  independent designs**, because both bootstrap gates passed — `shading` counted as signal
  and a `mesh` tower as carrier — while the cumulative scene through layer 2 had
  `world=None` and zero light objects. Fixed by splitting source from modifier, again at
  the JIT gate.

Every one of those is a gate that fires after the global plan is published and paid for,
on a layer the plan already promised would own appearance. The planner is not told, at the
point where it can still change the DAG, that a layer owing `image` evidence needs a
lighting provider in its closure. It discovers this one layer at a time, at materialization
cost, and — in hansa's case — twice for one layer.

## One predicate, four boundaries, enforced at the two lowest

The hansa_silk_road driver's table, from that shot's tenth attempt:

```
global publication   a layer declares evidence_domains ['image']   UNCHECKED
debt seeding         an eevee observation needs illumination       unbuilt
group planning       one plate per medium                          HIR-0241
contract binding     image-signal-bootstrap                        HIR-0110 / HIR-0160
```

HIR-0124 checks that a layer **declares** every domain a deferred requirement names.
**Nothing checks that it can produce one.** So `image` is admitted at publication and
refused three boundaries later, each time after the plan has been paid for.

**The measured cost of that ordering on one shot: `$1.19` to publish the plan, `$79.15`
and ten attempts to reach the failure.** The tenth attempt is the sharpest form of it --
both units passed, the silhouette defect was fixed, every critic panel passed on every
frame it could judge, and the layer still failed because a judgment debt declared `eevee`
on a layer with `world=None` and zero lights. The plate was black. Nothing was wrong with
the work.

Two of that layer's requirements make the point that this cannot be repaired downstream:

```
R8   "A dark podium/base anchoring the hero tower to the city plane"     image -> eevee debt
R50  "The hero tower remains identifiable and visually dominant …"       image -> eevee debt
```

Both are appearance propositions about a **lit** scene. A rule at debt seeding refuses them
correctly and leaves the layer declaring `image` with no legal way to close it -- a better
failure at a cheaper boundary, but still not a satisfiable plan. Only the publication check
refuses while the DAG can still be changed.

## Decision

Add optical signal to the globally declared capability vocabulary, with exactly the camera
mechanism and no new one:

1. `GLOBAL_SCENE_CAPABILITIES` becomes `{"camera", "illumination"}`. A sparse layer may
   declare `jit.provides.illumination` mapping to reserved-role selectors, as it does for
   camera.
2. **A layer declaring the `image` evidence domain declares `image_observation_media`, a
   non-empty subset of `{workbench_solid, eevee}`.** Global publication requires an
   `illumination` provider in the transitive `depends_on` closure of every layer naming a
   *lit* medium; a layer declaring only `workbench_solid` is never blocked by this rule.
   The refusal names the layer, the lit media it declared, both repairs, and the option of
   declaring solid-only if the layer judges form rather than appearance.
3. A materialized unit on an illumination-providing layer must mutate one of that layer's
   exact reserved illumination selectors, exactly as the camera grant already requires.
4. The JIT bootstrap gates are unchanged. They remain the check that the *unit* actually
   derives a source family; the global grant is a promise, not evidence, precisely as
   `jit.provides.camera` is a promise that a camera unit must still honour.

## Consequences

A plan that cannot light a layer it promised appearance for fails at publication rather
than at that layer's materialization. The gate that catches it is deterministic and costs
no model budget.

The vocabulary stays closed and small. This deliberately does **not** add a rendered-carrier
capability (`mesh`/`volume`/`compositor`) to the global set: geometry ownership is already
expressed by the layer DAG and reserved roles, and HIR-0160's carrier check has not yet been
observed to fire on a layer whose sparse DAG could have predicted it. Adding one capability
on evidence and one on symmetry would be padding.

Existing sparse bundles declare no `illumination` grant. Under strict migration (ADR-0004),
a bundle whose judged layers declare an `image` domain without a reachable provider is
rejected and must be republished; there is no compatibility window. The three shots in
flight are all pre-`illumination` and would need one republication each.

## Alternatives rejected

**Leave it at the JIT gate.** This is the current state and it is what produced two
independent unpayable designs for one hansa layer. The gate is correct and its refusal is
actionable; the objection is that it is reached only after the planner has committed a DAG
it cannot now change without a global amendment.

**Infer the provider from write families in the sparse DAG.** Sparse layers carry no units,
so there is nothing to derive a family from. Inferring from layer titles, `owns` axes or
reserved-role names is the role-name heuristic HIR-0098 retired for camera, and there is no
reason it would be sound here.

**A generic `signal` capability covering light, volume and compositor.** The three are not
interchangeable at plan time — a compositor provider implies a rendered prefix, a volume
provider implies a medium — and collapsing them would let a plan promise the wrong one.
`illumination` names what the closure must reach; which family supplies it stays the
materializing layer's decision, checked by the JIT gate.

## A partial implementation exists and was withdrawn

An attempt at the JIT half of this — `exact_signal_layers()` compiling the layers whose
ready units write an `IMAGE_SIGNAL_FAMILIES` cluster, and `compile_provider_activation`
gaining `require_signal` for `eevee` observations — was written, left unwired, and swept
into an unrelated commit by a `git add -A`. It is withdrawn, not adopted: the demand was
live and no caller supplied the witness, so every `eevee` judgment debt failed to compile
and four tests caught it.

It is recorded here because the shape was right and the level was wrong. Compiling signal
layers from *ready* units answers the question only for layers that have already
materialized, which is the same limitation the bootstrap gates have. The witness this ADR
proposes comes from the sparse DAG, where it can be checked before the plan is paid for.
The withdrawn patch is not a starting point for that.

## The open question, answered

The question put to the decision-maker was whether the refusal should block at publication
or be a closable blocker, given that a missing camera makes evidence *unmeasurable* while a
missing light makes it *unpayable but well-defined*.

**Answered: blocking, and scoped to lit judgments.** The asymmetry is real but it is not
between camera and light — it is between the two media a layer may be judged in. Blocking
every layer that declares `image` would refuse legitimate Workbench-solid form judgment,
which needs no lamp and no world. Blocking none admits hansa's failure. So the declaration
carries the distinction and the gate reads it, rather than the gate guessing from the
domain.

That is why `image_observation_media` exists rather than a bare closure check on `image`.
The declaration is also what makes the promise binding: a judgment debt seeded with a
medium its layer did not declare is refused at materialization, because publication proved
illumination reachability against the declaration and a debt outside it schedules an
observation the plan never promised.

`LIT_OBSERVATION_MEDIA` is derived from `RENDER_MODE_BY_MEDIUM`, not listed beside it. A
medium added later inherits its lighting requirement from the mode it realises instead of
being absent from a hand-kept set and silently reading as "needs no light" — the failure
direction that renders black.

One coupling had to be undone to land this. `allowed_unit_provides` computed a unit's legal
capabilities as `UNIT_PROVIDES - GLOBAL_SCENE_CAPABILITIES`, so adding `illumination` to the
global set would have silently removed it from what a unit may declare — retiring HIR-0234's
mechanism, under which an emissive facade declares that it is the light on an ordinary look
layer. Layer exclusivity is a *camera* rule, not a property of being globally declarable, so
it now has its own set (`LAYER_EXCLUSIVE_CAPABILITIES = {"camera"}`) and the two questions
are asked separately.

## The fixture that proves declaration beats inference

hansa_silk_road's selected view, read by that shot's session at the time of the decision,
holds 29 distinct debts: 18 `eevee`, 11 `workbench_solid`. Six of them carry **one verbatim
statement** at both media, produced by a single materialization:

```
jd-7a3dce76  workbench_solid   "Hero windows should come from a repeatable facade
jd-95b82d78  workbench_solid    module or shader mask rather than individually
jd-bc92453d  workbench_solid    modeled rooms."
jd-a3311859  eevee             (same statement, verbatim)
jd-e416a127  eevee
jd-e8549f34  eevee
```

The proposition is about construction method — windows visible in a solid plate mean
modeled geometry, windows absent mean a shader mask — so the three solid debts are correct
and the three eevee ones are over-specified. **Nothing in the sentence says which.** A gate
inferring the medium from the proposition would have to separate two textually identical
groups, which is the argument for declaring rather than inferring, stated by production
rather than by design.

That shot's DAG also declares no illumination provider on any of its seven layers, so for
those eighteen eevee debts there is no later layer at which any becomes satisfiable. A gate
that fired only at activation would let the plan publish and then die eighteen times. It
also means "schedule appearance judgments where lighting exists" has no target in that DAG:
the repair must *add* a provider, not relocate judgments to one.

## Migration

Strict, with no compatibility window (ADR-0004). A layer declaring `image` with no
`image_observation_media` is rejected where a debt would be seeded against it, naming
republication: nothing proved a light is reachable for that layer, and reading the absence
as "any medium is fine" is the silent compatibility this gate exists to prevent.

Measured on the three shots in flight at the time of the decision: **fifteen layers across
three shots declare `image`, and no layer in any of them declares an illumination
provider.** All three need one republication. None was running when enforcement landed —
caesar_curia and hansa_silk_road terminal `failed`, room_1046_opening `interrupted` — so no
active run was interrupted by the transition.
