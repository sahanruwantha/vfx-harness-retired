---
id: HIR-0243
title: A surface asks for what its field accepts
status: accepted
introduced_in: unreleased
date: 2026-09-06
failure_class: a_tool_description_asked_for_one_fault_owner_against_a_list_field
mechanism: the_description_asks_for_every_owner_and_the_handler_echoes_what_it_recorded
adr: null
---

# A surface asks for what its field accepts

## Observed failure

`caesar_curia`. A builder's `cannot_express_in_scope` named two fault owners in prose:

> "…produced by the sealed predecessor `chamber_shell`. …**Fault owner: chamber_shell**"
> "…**Fault owner: camera_rig** (frame-301 pose forces this), **secondarily chamber_shell**"

The typed `fault_owner_units` reaching the controller was `['camera_rig']`. `chamber_shell`
is layer 2, `camera_rig` is layer 1; owners spanning layers are refused, and the span check
reads the typed field, so it dispatched. The dispatch landed on layer 1, where 3 of the 4
contracts the finding names carry `owner_layer: "2"` and `validate.py:361` refuses a row
whose owner layer is not the candidate's.

## Why it was allowed

`blender/tools/reports.py`, `CANNOT_EXPRESS_DESCRIPTION`:

> "When executable evidence pins the floor to **a sealed upstream unit**, include **its**
> id from `unit_scope.fault_owner_options` so replan invalidates **the semantic owner**
> rather than only retrying this unit."

**Singular three times, against a field the handler reads as a set.** The builder found two
owners and recorded one, which is what it was asked for. That is not a careless model; it
is a producer answering the question the surface posed.

**And the same file already said the plural, thirty lines away.** `CANNOT_EXPRESS_SCHEMA`'s
own field description reads *"optional upstream unit **ids** whose sealed outcome causes the
measured floor"*. So this is not a surface that asks the wrong question -- it is **one
quantity with two derivations, in text**, where the wrong one is longer, carries the
rationale, and is what a model reads for intent. That is the pattern this repository already
names for code, in the layer nothing was checking. Found by the caesar_curia driver.

## What was rejected, and why each died

Four mechanisms were proposed across four sessions. Three were killed by evidence rather
than argument, which is the part worth keeping.

**Scan the reason text for known owner ids** (caesar_curia). Killed by the corpus: room's
`hf-6fc1f6f2` carries `fault_owner_units: ['camera_move']` and prose reading *"not a
`ground_island` placement or sizing defect"* -- **a unit named in order to exonerate it**.
Caesar's names `chamber_shell` twice to blame it. Same token, opposite meanings, nothing
structural between them. An id appearing in prose is not the same fact as an id being
blamed.

**Refuse a dispatch whose finding names contracts the target layer cannot own** (this
session). Killed twice. AGENTS.md's own HIR-0191 text compiles the amendment "on that
owner's layer view (never the stopped layer, which cannot change them)", so the failing rows
belong to the stopped layer *by construction, in every dispatch*; caesar's amendment changed
only `stages`, adding `camera_aim_anchor`, touching no contract row; and room's
`hf-2471bd031b5215b66ba1` -- correct, and the case ADR-0010 exists for -- names a contract
with `owner_layer: "2"` while targeting layer 1. The check's precondition is the
mechanism's normal shape.

**Refuse when a row's authored `fault_owner` disagrees with the finding's owners**
(caesar_curia, then falsified by its author). room's `facade-parallax-76-113` carries
`fault_owner: "2"` against a finding blaming `camera_path` in layer 1 -- the same
disagreement, in the case we are most confident is correct. It is what a *true* cross-layer
finding looks like, not a signal.

**Per-cause `causes: [{contract_ids, reason, fault_owner_units}]`**. One two-cause finding
in eight, no second instance; a record-shape change on a single dispatch. Not built.

**Make `fault_owner_units` required.** Most are empty and terminalize with no dispatch,
which is fail-closed. Requiring a value does not make a builder know an owner it does not
know -- it makes it name one, and a populated field is then indistinguishable from a known
owner downstream. Same shape as a metric returning a plausible number in the wrong medium
(HIR-0241).

## What the corpus does and does not support

An early reading of this record claimed *"5 of 8 falsification records carry the field empty
while their prose names owners"*. **That number is wrong and the corrected one is weaker.**
The caesar_curia driver deduplicated by `record_id` -- the raw file count is inflated by
scratch plan-consumer replicas -- giving **24 distinct records across three shots, live and
archived**:

```
records whose prose accuses a unit the typed list omits   : 1 / 24   (caesar's)
  ...with an EMPTY typed list                             : 0 / 24
records where any known unit id appears in prose but not
  in the typed list (broad reading)                       : 7 / 24
  ...with an EMPTY typed list                             : 5 / 24
```

The broad reading reproduces the original count and **its hits are dominated by ordinary
description** -- one record names seven unit ids at once, and another names `ground_island`
in the sentence exonerating it. So the corpus supports no *refusal* built on prose, and the
motivating case is a populated-but-incomplete field rather than an empty one.

That is why this record ships no check. What it ships is a surface that stops posing the
question that produced the incomplete answer, which needs no corpus support beyond the one
instance and the contradiction in the file.

## Mechanism

**The description asks for what the field accepts**, and says what the consumer does with
it:

> "…include EVERY one of their ids from `unit_scope.fault_owner_options` -- the field is a
> list and the controller reads only this list, so an owner you name in reason but omit
> here is invisible to it. If the causes have owners in different layers, list them all: a
> finding whose owners span layers routes to reviewed authority instead of an automatic
> dispatch, and that routing is the correct outcome."

The last sentence is deliberate. Without it, a builder that discovers listing both owners
forfeits a dispatch has an incentive to list one.

**The handler echoes what it recorded**, so the set is visible while turns remain. The tool
was **not** without a read-back -- it already echoed `contract_ids`, the classification and
the full prose `reason`, and omitted `fault_owner_units`. **The existing observation
confirmed the channel the controller ignores and hid the one it reads**, which is a sharper
statement of the defect than the one this record opened with (caesar_curia driver):

```
Recorded fault owners: camera_rig (layer 1), chamber_shell (layer 2) -- owners span
layers 1, 2; this finding routes to reviewed authority.
```

**It states the consequence of omission rather than the sufficiency of what is present**,
and names the correction path, because the read-back sits beside "Do not edit the script
further" and would otherwise read as *you are done*:

```
The controller reads only this list; a unit you named in reason and not here is invisible
to it. If this list is incomplete, call cannot_express_in_scope again now with the full set
-- the later call replaces this one.
```

That promise is **proven, not assumed**: `test_a_second_call_replaces_the_record_and_the_echo_says_so`
calls the handler twice and asserts the second record replaces the first. The caesar_curia
driver read the overwrite and the rounds-zeroing and rated it medium confidence without
running it; running it is the difference.

**It states no counterfactual.** The first draft ended *"a single-layer owner set is
dispatchable"*, and the hansa_silk_road driver's objection retired it: an echo that names
dispatchability as a property of the set the builder just chose teaches it that dropping an
owner buys a dispatch -- the incentive the description exists to remove, handed back at the
moment there are still turns to act on it. A test asserts the echo never contains
"dispatchable", "would", or "instead", for every owner shape.

Neither half carries the fix alone, and that was argued rather than assumed. The echo fires
*after* the decision: a builder that records one owner gets an accurate success message
about a wrong set, and nothing in it prompts revision. The description is what changes the
recorded set; the echo is what makes a mistaken set visible in time.

## Also: one exit-code table

Found while reading the truncation prose the hansa driver flagged.
`application/run_shot._MEANING` was a second copy of `observability.EXIT_DETAILS`, **and
they had already drifted** -- code 9 read "layer ran cleanly but its VERDICT was not a pass"
in one and "has no passing terminal publication" in the other, for one digit an operator
sees from either surface. `_MEANING` is now `{0: "ok", 1: "crashed", **EXIT_DETAILS}`.

Exit 3 read *"TRUNCATED -- raise the budget or split the layer"* for four distinct causes.
hansa hit it twice on a stalled stream -- `no SDK event for 360s after UserMessage`,
heartbeat `events=344` -- where neither prescribed action touches the problem. It now names
the record instead of prescribing a cure:

```
MODEL PHASE DID NOT COMPLETE — reports/summary.json terminal_cause says which:
max_turns_exhausted, model_budget_exhausted, model_session_failure, or
model_session_idle_timeout
```

Same shape as HIR-0226 one surface over: the record was right and the prose sent the
operator elsewhere.

## Validation

`src/tests/unit/test_fault_owner_surface.py` (9) and
`src/tests/unit/test_exit_code_meanings.py` (4). The handler's call site is **parsed, not
grepped** -- a call node with that callee inside `record_cannot_express` -- because a
substring check passes with the call deleted and its text left in a comment.

`test_composed_group_medium.py` gains the hansa driver's other objection as an assertion: a
medium-only composed group must carry its medium explicitly, because `_unit_raster_mode`
returns `"solid"` for a unit with no look capabilities and such a group declares none. It
renders EEVEE only because the medium branch returns first, which is branch order standing
in for an invariant.

One stub signature moved: `test_builder_stop_boundary.py` patched
`_composition_judge_unit` with `lambda *_args`, which the caller now invokes with
`medium=`. A fixture matching a changed signature, not a loosened assertion.

## What this does not fix

The description is a prompt change. It now has one mechanical test --
**no tool description may describe one of its own array fields in the singular**, which
scans every `*_SCHEMA` on the tool surface and had zero other offenders -- but the rest of
its assertions are string comparisons, and a future edit can weaken the prose without
failing them.

The two halves are not interchangeable and the ordering matters. The echo fires *after* a
decision the surface calls terminal, needs a second call nothing else advertises, and
depends on a model re-reading its own prose against a list; the description prevents the
wrong answer being given. **If only one shipped it should be the description**, which
inverts what this record's author first assumed (caesar_curia driver).

Nothing here makes a builder name an owner it has not identified. If the two-cause shape
recurs, per-cause `causes` becomes the answer and this record's rejection of it should be
revisited with the second instance in hand.

Reported by the caesar_curia driver; the prose-scan rejection, the echo rewording and the
medium-coupling assertion are the hansa_silk_road driver's; the counter-example that killed
the row-ownership check is the room_1046_opening driver's.
