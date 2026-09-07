---
id: HIR-0249
title: One picture, two obligations
status: accepted
introduced_in: unreleased
date: 2026-09-06
failure_class: composed_groups_re_rendered_a_plate_that_could_not_differ
mechanism: a_capture_is_reused_on_a_verified_key_over_everything_that_moves_a_pixel
adr: null
---

# One picture, two obligations

## Observed failure

A layer finalization runs one composed group per judgment debt, plus one per medium no
debt covers (HIR-0241). Each group replays the layer and renders its own plate under
`canonical_namespace=f"finalization_group_{group_index}"`, which guarantees a distinct
destination file per group.

Groups that differ only in *which debt they pay* replay the same scripts, at the same
frame, in the same mode, at the same scale. Their plates are byte-identical.
`hansa_silk_road` produced three identical Workbench-solid captures in one finalization.

This wastes rendering and nothing else: no verdict was wrong, and the duplication does not
block any shot. It is recorded because the cost is real and recurring, not because it
broke something.

## Cause

The group index reached the render destination as part of the tag, so identity of the
*group* became identity of the *plate*. Those are different things. A group's identity is
the obligation it settles; a plate's identity is the scene, frame, mode and scale that
produced it. Nothing enforced the second, so every group got its own render by
construction.

## Mechanism

`domain/capture_equivalence` digests everything that determines the pixels and nothing
that determines the judgment:

```
replay scene   ordered executed replay inputs: (script_path, script_sha256)
               plus each input's dependency digests
frame
render mode
render scale
```

Which debt a group pays, which claims it evaluates, its judge points and its verdict are
**deliberately absent**. Sharing a plate must never share an obligation: two groups may
look at one picture and owe two separate answers about it. The test that pins this asserts
the key function takes no debt, claim or verdict parameter at all, because the absence is
the property.

`agents/builder/capture_cache.CaptureCache` lives for one finalization. On a hit it copies
the cached bytes to the requesting group's own locator, so every group's receipt still
names a file that exists under the name a reader expects; only the Blender render is
skipped. Two properties keep reuse safe rather than merely cheap:

- **A hit is verified, not trusted.** The cached file must still exist and still hash to
  the digest its receipt claims, and the receipt's frame/mode/scale are re-checked against
  the request. A dictionary lookup is not evidence about a file, and the cost of being
  wrong is one group judging another group's picture.
- **A miss is always safe.** An unusable key returns `None` rather than raising, so this
  can never be the reason a layer cannot seal. Every failure mode degrades to the
  behaviour that predates it.

## Measured on the shot that produced it

hansa_silk_road's attempt-10 finalization, read from its evaluation receipt by that shot's
session:

```
f1    group_0 solid  mean 26.81  sha 32bd2170ad89     f1    group_2 eevee  mean 0.08
f1    group_1 solid  mean 26.81  sha 7d12487a392e     f51   group_2 eevee  mean 0.08
f51   group_0 solid  mean 26.83  sha 700b6c28cd7f     f151  group_2 eevee  mean 0.08
f51   group_1 solid  mean 26.83  sha 97bd5339a375
f151  group_0 solid  mean 26.97  sha ab467eef80bf
f151  group_1 solid  mean 26.97  sha 58a264026a7b
```

Nine renders where six would do: three wasted, not six. Group 2 is a genuinely different
medium and must render. **The duplication is per (medium, frame) pair**, which is what the
key computes.

The load-bearing detail is in the hashes. Groups 0 and 1 are **pixel-identical** at every
frame -- diff bbox `None`, max delta `0` -- and **no two of the nine plates share a
SHA-256**, because PNG output carries metadata that differs per write. So:

- **Keying on the produced file would silently never hit.** That is the obvious
  simplification of this design, it would look like it was working, and it is documented
  in the module and pinned by a test for exactly that reason.
- **Copying cached bytes to each group's own locator is right for a second reason.** The
  copies become byte-identical where separate renders of one scene are not, so a later
  digest-of-artifact check over these files becomes meaningful rather than accidentally
  distinguishing identical pictures.

`render_scale` is 0.5 everywhere on that shot and nothing currently varies it. It stays in
the key as the defensive component: omitting it costs nothing today and returns the wrong
picture the day something does.

## Can a composition unit move the scene?

The empirical evidence could not settle this -- hansa's two duplicate groups drew on the
same units, so it distinguishes "a composition unit cannot touch the scene" from "neither
of mine did" not at all, and that shot's session declined to infer it rather than guess.

Read from `_verify_script` instead. Before the render, `active_unit` reaches exactly two
things: the scoped-mutation check, which compares object manifests and mutates nothing,
and `_unit_raster_mode`, which selects the render mode and is itself in the key. The scene
is produced by the executed replay inputs, which are the key's first component. So two
composition units differing in mutation scope, claims or judge points key identically, and
the only pixel effect a unit carries is its medium.

## The test double that confirmed the assumption

The first version of the dependency check read `script_path`/`script_sha256` off each
dependency. Those are `ExecutedReplayInput`'s field names; `ExecutedReplayDependency`
carries `kind`/`path`/`sha256`. Read through `getattr(..., "")`, every dependency collapsed
to empty strings, so **changing a promoted asset's digest produced an identical key** --
the false-hit direction, which serves one scene's plate for another's.

Its test passed. The test defined its own `_Dependency` dataclass with the same wrong field
names, so the double was shaped to the assumption rather than to the type, and the
assertion confirmed the belief instead of the behaviour. A fixture invented alongside the
code it tests inherits the code's misreading; the tests now construct real
`ExecutedReplayInput` and `ExecutedReplayDependency` values.

The structural half is that a shape mismatch must not degrade quietly. Identity is read by
direct attribute access rather than `getattr` with a default, so an unrecognised type
raises; `CaptureCache.key` catches that and returns a miss. A blank dependency digest is
also refused outright, because a dependency read as blank is indistinguishable from one
that did not change. Every failure mode renders again; none of them keys on what it
managed to read.

## Validation

`src/tests/unit/test_capture_reuse_across_composed_groups.py`, ten tests, both directions
weighted equally: a missed reuse costs a render, a false reuse is a wrong verdict on real
evidence. Reuse happens on a matching key; a changed frame, mode, scale, script body, or
*dependency* digest each force a fresh render on their own; a hit whose bytes changed
underneath is dropped rather than served; a vanished plate is a miss rather than a crash.

**Each refusal test carries a positive control on the same cache.** Without it, every
refusal test passes identically when reuse is broken or absent — a check that could not
have failed. Verified by disabling reuse: with controls, 7 of 10 fail; without them, only
2 did, and the four parametrised refusal cases were passing vacuously.
