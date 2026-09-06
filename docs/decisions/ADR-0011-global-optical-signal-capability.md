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

## Open question for the decision-maker

Whether the global refusal should be blocking at publication, or a declared blocker the
planner may close with an explicit `approved_start`/`planner_start` decision when a layer's
appearance is genuinely deferred. Camera is blocking today. The asymmetry argument is that a
missing camera makes projected evidence *unmeasurable*, while a missing light makes image
debt *unpayable but still well-defined* — which is the same distinction HIR-0124 draws
between structural and image domains.
