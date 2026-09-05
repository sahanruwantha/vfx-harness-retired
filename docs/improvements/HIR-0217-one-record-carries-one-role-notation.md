---
id: HIR-0217
title: One record carries one role notation
status: accepted
introduced_in: unreleased
date: 2026-09-05
failure_class: control_roles_read_absolute_roles_while_its_sibling_field_refused_them
mechanism: every_authoring_field_constrained_to_the_units_own_roles_is_compiled_from_one_namespace
adr: ADR-0005
supersedes_scope_of: HIR-0150
---

# One record carries one role notation

## Observed failure

Thirteen materialization refusals across **seven sessions on all three shots**, every one
of them a materializer writing `mutates.control_roles` in the notation the field beside it
had just taught, verbatim from the transcripts:

| shot | session | refusal |
|---|---|---|
| caesar_curia | `20260905T012532Z-073ad8` L1 | `control_roles.dolly maps roles outside mutation scope: $self` |
| hansa_silk_road | `20260904T143358Z-238376` L1 | `control_roles.camera.rig.aim maps roles outside mutation scope: $self` |
| hansa_silk_road | `20260904T143358Z-238376` L1 | `control_roles.camera.rig.aim maps roles outside mutation scope: aim` |
| hansa_silk_road | `20260904T143358Z-238376` L2 | `control_roles.podium_elevation maps roles outside mutation scope: podium` |
| hansa_silk_road | `20260905T003850Z-f7f2cc` L2 | `control_roles.facade_material maps roles outside mutation scope: $self` |
| hansa_silk_road | `20260905T003850Z-f7f2cc` L2 | `control_roles.facade_material maps roles outside mutation scope: material_slot` |
| hansa_silk_road | `20260905T003850Z-f7f2cc` L2 | `control_roles.facade_material maps roles outside mutation scope: hero.facade.material_slot` |
| hansa_silk_road | `20260905T003850Z-f7f2cc` L2 | `control_roles.podium_anchor maps roles outside mutation scope: hero.podium` |
| hansa_silk_road | `20260905T003850Z-f7f2cc` L2 | `control_roles.mass_rig_stability_control maps roles outside mutation scope: rig_control` |
| room_1046_opening | `20260903T211552Z-8ad44d` L2 | `control_roles.streetlamp_energy_schedule maps roles outside mutation scope: exterior.streetlamp` |
| room_1046_opening | `20260903T211552Z-8ad44d` L2 | `control_roles.streetlamp_energy_schedule maps roles outside mutation scope: exterior.island.pole` |
| room_1046_opening | `20260903T094435Z-007748` L2 | `control_roles.ground_streetlight_flicker maps roles outside mutation scope: exterior.ground.streetlight_a` |
| room_1046_opening | `20260904T043459Z-1c35c2` L2 | `control_roles.key_reveal_ramp maps roles outside mutation scope: exterior.mass` |

Every refusal names the offending token. None names an accepted value.

## Reproduction

At `09daa94`, against the staging schema the materializer is actually given:

```
control_roles schema entry: {"type":"object","additionalProperties":{"type":"array",
                             "items":{"type":"string","minLength":1},...}}
role_members description:   "Relative role suffixes inside role_namespace; use $self for
                             the namespace tag itself. Absolute roles are not accepted."

$self             schema OK -> unit.mutates.control_roles.podium_elevation maps roles
                               outside mutation scope: $self
relative member   schema OK -> unit.mutates.control_roles.podium_elevation maps roles
                               outside mutation scope: podium
absolute          schema OK -> compiled roles ['hero.podium','hero.podium.podium'] PARSE OK
```

Byte-identical to the shot transcripts. The only accepted form is the one form the
schema tells the model, three lines above, is not accepted.

## Root cause

Two boundaries, one cause: the staging authoring dialect was introduced without being
made the *only* dialect.

**1. `control_roles` kept the pre-HIR-0150 notation.** HIR-0150 made "two write namespaces
in one unit" unrepresentable by replacing absolute `mutates.roles` with one `role_namespace`
plus relative `role_members`, `$self` denoting the namespace tag. Its decision enumerated
the typed exceptions it had considered — `dresses`, consumed interfaces — and did not
consider `control_roles`, **the one remaining field whose values are drawn from that very
list**: `MutationScope.parse` computes `set(targets) - set(roles)`, so a `control_roles`
value is legal only if it is one of the compiled roles. After HIR-0150 a materializer never
types an absolute role anywhere in the record, while `control_roles` silently kept reading
the absolute shape, with no `description` of its own.

**2. `patch_materialization` never compiled at all, and the durable parser dropped what it
sent.** The patch tool writes the same `mutates` object the staging tool takes, but passes
the raw value through. `MutationScope.parse` read only the keys it knew, so
`role_namespace` and `role_members` were **silently discarded** and the unit landed with
`roles: ()`. Verified directly:

```
MutationScope.parse({"mode":"scoped","role_namespace":"hero.facade",
                     "role_members":["material_slot"],"controls":["facade_material"],
                     "control_roles":{},...})
  -> roles=()  controls=('facade_material',)  control_roles=()
```

That is the mechanism, not a metaphor for it. hansa_silk_road's unbuildable
`hero_podium_material` — `mutates.roles: []`, a bare `controls` entry, no derived write
cluster, no legal `run_bpy` at all — is the residue of exactly this: the materializer
patched a coherent clustered `mutates`, `role_members` was dropped on the floor, and what
persisted was a control that steers nothing. HIR-0215 landed the gate that refuses such a
unit at publication. This record removes the thing that builds one.

Attributing the 13 refusals to their tool and payload shows both halves:

| shape | tool | count |
|---|---|---|
| coherent relative (`$self` / bare member) with a namespace | stage ×3, patch ×2 | 5 |
| absolute role, correct notation, not a declared member | patch | 1 |
| control mapped on a unit with **no** `role_members` at all | stage | 7 |

The third row is the zero-cluster unit being authored, live, seven times: with `roles`
empty, `set(targets) - set(roles)` is everything, so `$self`, a relative member and an
absolute role all produce the identical sentence. The message could not distinguish *wrong
notation* from *wrong role* from *no roles at all*, and named none of them.

The earliest owning decision is therefore HIR-0150's scope, not `claims.py`. The boundary
that should have prevented it is the authoring dialect itself: it was introduced on one
tool, and neither the sibling field, the other tool that writes the same object, nor the
durable parser that must never hold it were converted with it.

The refusal is the second, independent defect: AGENTS.md requires that rejections name
"the violated contract, the observed value, and the legal next action", and this one named
only the observed value — while the accepted set sat in a local variable one line above.

## Decision

- Every authoring field whose legal values are roles of the unit's **own** write namespace
  speaks one notation: relative members of `role_namespace`, `$self` for the namespace tag.
  Those fields are enumerated once, in `NAMESPACE_RELATIVE_ROLE_FIELDS`, and compiled by
  one function from one namespace value.
- `dresses` is deliberately excluded and stays absolute: it names *another* layer's roles
  by design (ADR-0007), so it is not constrained to this unit's namespace.
- `control_roles` carries the same JSON-schema pattern as `role_members` and a description
  stating the notation and that its values must be drawn from `role_members`.
- A `control_roles` entry on a unit with no `role_members` is refused at compilation, naming
  the consequence the materializer cannot otherwise see: a control that steers no mutated
  role derives no write family and the unit can execute no mutation.
- **The staging and patch tools speak one dialect.** `patch_materialization` compiles a
  `mutates` value — or a whole stage row carrying one — through the same function
  `stage_materialization_unit` uses. What a session may stage, it may patch.
- **The durable parser fails closed on the staging-only keys.** `MutationScope.parse`
  refuses `role_namespace`/`role_members` rather than ignoring them, so an uncompiled
  authoring payload can never again be read as a unit that mutates nothing. The key list
  has one source of truth, shared by the compiler and the parser.
- `MutationScope.parse` — the path every direct and non-clustered caller takes — names the
  accepted set in its refusal: this unit's mutation roles, or, when there are none, the
  legal next action.
- The staging tool description states the `control_roles` notation beside the role notation.

The durable `WorkUnit` shape, `unit_digest`, and every consumer of
`unit.mutates.control_roles` are unchanged: compilation still emits the absolute mapping.
This is an authoring type, exactly as HIR-0150 was.

## General mechanism

`compile_clustered_mutation_roles` compiles `control_roles` values through the same
`_compiled_member(namespace, member)` rule as `role_members`. Because the values are
prefixed with the one namespace, a control cannot reach a second write cluster through the
mapping — the invariant HIR-0150 established for `roles` now holds for every field that
carries them.

An absolute value is now a *typed* mistake rather than the only working form, and its
refusal says which relative member it should have been. A value that is relative but not a
declared member is refused with the exact member list. Both messages end in the accepted
set.

## Validation

`src/tests/unit/test_work_units.py`:

- `test_clustered_control_roles_speak_the_relative_role_notation` — `$self` and a bare
  member both compile, and the parsed `MutationScope` still carries the absolute mapping
  the write clusters consume.
- `test_clustered_control_roles_refuse_absolute_roles_and_name_the_members` — both
  wrong-notation shapes from the transcripts are refused naming `Legal members: $self, aim`.
- `test_clustered_control_roles_without_role_members_name_the_write_family_cost` — the
  zero-cluster unit's first step is refused with its consequence.
- `test_control_roles_refusal_names_this_unit_s_mutation_roles` — the parser's own refusal
  states the accepted set in both the populated and the empty case.
- `test_every_namespace_relative_role_field_is_compiled_and_documented` — a field added to
  the clustered `mutates` schema that carries this unit's roles must be enumerated,
  patterned, and described, so the next one cannot reintroduce a second notation.
- `test_durable_mutation_scope_refuses_uncompiled_staging_keys` — the silent drop that
  manufactured the unbuildable unit is now a typed refusal, and the compiled shape still
  parses.
- `test_one_authoring_dialect_across_the_staging_and_patch_tools` — replays the three
  payloads hansa f7f2cc sent to `/layer/stages/5/mutates`; two now compile and the third
  is refused naming its legal member. A whole-stage patch compiles too, and a
  non-`mutates` pointer is left untouched.
- `test_staging_dialect_keys_have_one_source_of_truth` — the compiler and the parser cannot
  disagree about what is staging-only.

All eight fail at `09daa94` and pass with the mechanism; run both ways.

### Live validation on a shot

hansa_silk_road run `20260905T055913Z-bf035c`, layer 1, picked the change up mid-flight
(the materialize subprocess imports these modules lazily). Its materializer authored this
on its **first** staging attempt, unprompted and with the schema description not yet in the
process image:

```json
"mutates": {
  "role_namespace": "camera.rig",
  "role_members": ["$self"],
  "controls": ["lens", "pitch", "dolly", "roll", "orbit"],
  "control_roles": {"lens": ["$self"], "pitch": ["$self"], "dolly": ["$self"],
                    "roll": ["$self"], "orbit": ["$self"]}
}
```

A/B on those exact bytes, both directions:

| code | compiled `control_roles` | result |
|---|---|---|
| `09daa94` | `{"lens": ["$self"], …}` left uncompiled | refused: `control_roles.lens maps roles outside mutation scope: $self` |
| with the mechanism | `{"lens": ["camera.rig"], …}` | accepted; unit `camera_path` staged |

`control_roles` refusals in that materialization: **0**. The same shape historically cost
2–3 refusals per unit on that shot.

The load-bearing observation is *which* form the model chose with no positive guidance in
its schema: relative, on the first try, for all five controls. The old absolute form was
not what it reached for. That is evidence the notation was wrong rather than merely
inconsistent — the record was asking for the one shape the model does not produce.

## Rejected alternatives

- **Only improve the message.** It would have converted 13 refusals into 13 cheaper
  refusals. The model still has to type a notation the sibling field forbids, and the
  inconsistency stays representable for the next field.
- **Accept both notations in `control_roles`.** A value like `aim` is ambiguous — a
  relative member, or an absolute one-token role — and resolving it by "try relative, fall
  back to absolute" is a heuristic compatibility read, which ADR-0004 forbids.
- **Drop `control_roles` and derive control→role from claims.** `atomicity.py` needs the
  mapping before claims are validated, and the mapping is authored knowledge (which control
  steers which role), not derivable from evidence bindings.

## Reconsider when

A future authoring field needs to name a role that is *neither* this unit's own nor another
layer's dressable grant. `NAMESPACE_RELATIVE_ROLE_FIELDS` and its architecture test would
then be describing two rules, not one, and the split belongs in the type rather than in the
list.
