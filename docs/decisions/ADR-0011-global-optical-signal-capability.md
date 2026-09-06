---
id: ADR-0011
title: Optical signal is a globally declared capability, like camera
status: proposed
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
2. Global publication requires: every layer declaring an `image` evidence domain must have
   an `illumination` provider in its transitive `depends_on` closure, or be that provider.
   The refusal names the layer, its declared domains, and the layers that could supply it —
   the same shape as the `deferred_owner` domain-coverage refusal (HIR-0124).
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

## Open question for the decision-maker

Whether the global refusal should be blocking at publication, or a declared blocker the
planner may close with an explicit `approved_start`/`planner_start` decision when a layer's
appearance is genuinely deferred. Camera is blocking today. The asymmetry argument is that a
missing camera makes projected evidence *unmeasurable*, while a missing light makes image
debt *unpayable but still well-defined* — which is the same distinction HIR-0124 draws
between structural and image domains.
