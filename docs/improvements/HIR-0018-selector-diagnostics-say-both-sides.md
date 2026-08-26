---
id: HIR-0018
title: Selector instruments knew both sides of every mismatch and reported one
status: accepted
introduced_in: unreleased
date: 2026-08-25
failure_class: diagnostic_asymmetry_makes_wrong_theories_cheaper_than_right_ones
mechanism: two_sided_miss_enumeration_single_resolver_either_of_matching_and_selector_coherence_lint
adr: ADR-0003
---

# Selector instruments knew both sides of every mismatch and reported one

## Observed failure

Layer 2's `atmosphere_and_bloom` unit (shot generation `088b4a7c…`) failed two full
builds and four repair rounds (runs `20260825T0447…`/`20260825T070153Z-f6cb7d`,
~55 min and 162 tool calls in the first alone) on defects that were each one edit
away, while the build script accumulated three confident, wrong, mutually reinforcing
comments:

1. The plan's requirements and all three bloom selectors say `world.bloom.compositor`
   (six mentions); the script tagged `world.bloom`, with a comment citing the unit's
   own `layer_state.json` "tried" entry as the recorded accepted approach — the
   builder's failed-experiment residue outranked published authority.
2. A comment asserted that `scene.compositing_node_group` is "not where
   bvfx_glare_bloom materializes the graph" and re-fetched via `scene.node_tree` —
   an attribute Blender 5.2 removed. Every canonical run crashed on that line, which
   also invalidated repair 1's role-rename experiment and let it "prove" the role
   string irrelevant.
3. The atmosphere control tag sat on nodes with no `Value` socket (VolumeScatter,
   then Background), so the auto-socket sweep could never resolve; the pre-`ab66d57`
   verdict text routed this away from repair as a binding defect.

Every one of these survived because the failing instrument said only its own half:
`semantic control matched 0 nodes`, `has no requested auto socket`. The resolver had
already enumerated the graph's nodes and the node's sockets to produce those verdicts
— the true tag (`world.bloom`), the real socket inventory, the one-edit cure — and
withheld them. A capable model given half the evidence invents the other half; the
three comments are the rational output of that instrument set.

Two adjacent structural defects surfaced while proving the fix:

- `probe_control` (blender/tools.py) carried a private near-copy of the control
  resolution script — singular selector keys, the same terse errors. The tuning
  instrument and the authoritative instrument could drift apart silently
  (ADR-0003's split-registry failure, re-grown).
- The node matcher read `bvfx_control or bvfx_role`, so a node tagged with both had
  its role SHADOWED: `AtmosphereVolume` carried role `world.atmosphere.volume` plus
  control `world_atmosphere_density`, and the role selector silently resolved to a
  different node while reporting a clean `matched 1` — a wrong answer shaped like a
  right one.
- The published view pairs `world-bloom-response` (auto socket → demands a literal
  `Value` socket on its selector's ONE node) with `world-bloom-threshold-bound`
  (pins the same selector to a `Threshold` input). CompositorNodeGlare exposes
  `Threshold` and no `Value` — the row pair is unsatisfiable by the node class the
  contract intends, and nothing refused it at authoring.

## Root cause

A selector system whose miss diagnostics are one-sided converts every tagging
mismatch into an open-ended investigation. The information asymmetry is structural:
match sites (batch probe, control script, tool copy) each independently decided what
to say about a miss, none said what was present, one existed twice, and one collapsed
two tag namespaces by precedence. Meanwhile per-row validation could not see the
cross-row interface contradiction, so the one defect that genuinely needs
re-materialization published silently beside three that did not.

## Mechanism

- **Two-sided misses** (`e6577ea`): every selector-driven miss enumerates the other
  side — node/control misses list the semantic tags present in the searched graphs,
  socket misses list the node's actual input/output socket names plus the
  literal-`'Value'` resolution rule, object misses list scene role tags. Measured
  zeros (`node_count`, `object_count`) attach the same enumeration as a `note` that
  flows through evidence packaging, `probe_candidate` rows, and failing verdicts.
  Evidence error caps 160→400 so an enumeration cannot truncate into a
  complete-looking list.
- **One resolver** (`3e3051f`): `probe_control` renders
  `scene_checks._control_script` through a selector adapter; the private copy is
  deleted. Tuning probes and authoritative evidence can no longer disagree.
- **Either-of matching** (`8e648b8`): a selector matches if EITHER `bvfx_control` or
  `bvfx_role` matches. Real ambiguity (two nodes claiming one selector) now surfaces
  as `matched 2` with both namespaces enumerated instead of a silently wrong
  `matched 1`.
- **Selector coherence lint** (`f53cfb0`): `validate_row_set` refuses an auto-socket
  `control_render_response` that shares its `(graph, node_roles)` selector with a
  socket-pinned `node_socket_value` — at the authoring seams (`plan_guardrails`
  write hook, `validate_materialization`), where the author can still add the one
  field. The plan gate reports grandfathered instances as ADVISORY findings only: a
  permissive node class (Math — every input literally named `Value`) satisfies both
  demands, and the sealed, passing `world-arc-luminance-response` proves blocking
  retroactively would poison working authority.

## Validation

- Headless Blender 5.2: the exact production misses now read
  `node_roles ['world.bloom.compositor'] matched 0 nodes … semantic tags present:
  world.bloom` and `ShaderNodeBackground has no requested auto socket 'Value';
  inputs=['Color', 'Strength', 'Weight'] …`; the shadow scenario (role + control on
  one node) matches by role.
- In production, mid-run: the enriched socket message fired in run `f6cb7d`'s
  canonical critique, and the eternally-0 bloom role string was fixed within one
  round (`world-bloom-nodecount` 0 → 1, `world-bloom-threshold-bound` 0.85 ≥ 0.8).
  What kept failing after that is exactly the two materialization-owned rows.
- The lint, run against the shot's current published view, flags all three auto+pinned
  pairs and nothing else; regression test pins the production row shape, the socket
  fix, and the selector split. Suite 284, ruff clean.

## Consequences

The two `control_render_response` rows the build could never satisfy are now proven
materialization defects with executable evidence in the failure record (the Glare
socket inventory; the world-graph physics cliff — an unbounded world volume
extinguishes Sun lights at any density, verified at three magnitudes). Layer 2's
next materialization must declare response sockets (the lint now forces it) and
re-scope atmosphere response to a construction that can respond (bounded volume
domain in a material graph, per the `bvfx_volume` recipe). Either-of matching may
surface latent both-tag ambiguities in previously sealed evidence at next
revalidation; that is the honest reading and the intended fail-closed behavior.

## Follow-up (2026-08-26)

The builder scene tools still addressed display names after this record landed, so
HIR-0018 was unbound on the surface the builder actually calls. HIR-0022 routes
`inspect_scene`, `check_scene`, and `list_keyframes` through the one `match_semantic`
resolver and makes a miss name present names **and** roles. Object-count and node
misses in authoritative evidence already enumerated tags; the builder tools now do
the same.
