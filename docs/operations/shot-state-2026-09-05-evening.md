# Shot and harness state at 2026-09-05 evening, main `3d4089e`

Supersedes `shot-state-2026-09-05.md`, which describes the morning at `261c5fa`. Written
to disk for the same reason: three sessions drove shots today and their live context does
not survive. Everything here is recoverable from the repository and the shot artifacts.

## What landed today, after `09daa94`

| commit | record | what |
|---|---|---|
| `d2228b3` | HIR-0217 | one work-unit record carries one role notation |
| `d2228b3` | HIR-0218 | a recorded vocabulary gap is read where it is written |
| `c8ffee5` | HIR-0219 | a metric declares the values it can produce |
| `ad577da` | — | HIR-0218's citation on four accumulated gaps |
| `2cfaa9f` | — | narrowed an over-claim: gap rows carry no layer field |
| `65d729e` | — | AGENTS.md: why opening the artifact beats being careful |
| `3d4089e` | — | require the shot folder where omitting it is silent |

`3d4089e` fixes an outage this session caused. Two shots lost complete candidates to it.

## The three defects, briefly

**HIR-0217.** HIR-0150 replaced absolute `mutates.roles` with a relative
`role_namespace`/`role_members` shape and left `control_roles` — the one other field whose
values must be drawn from that list — reading the old absolute form, undocumented. 13
refusals across 7 materializations on all three shots. Also: `patch_materialization` never
compiled the clustered dialect, and `MutationScope.parse` silently discarded
`role_namespace`/`role_members`, which is how a published unit came to mutate nothing.

**HIR-0218.** `escalate_vocabulary_gap` writes under `<shot>/state/plan-escalations/`; the
validator read the same relative path under the plan bundle, which is content-addressed and
has no `state/`. The read returned `{}` in every shot and every run, so the branch letting a
recorded gap close a structural-only requirement **had never executed**. Confirmed live: four
accumulated gaps read back correctly, and three requirements were refused padding closure in
one materialization.

**HIR-0219.** `transform_return_delta` is a magnitude, and two wholly negative bands over it
cleared every gate. Range-aware validation already existed, hand-written four times for four
kinds, so a fifth inherited none of it. Ranges now sit in the canonical registry. Metrics
that are genuinely signed declare none — a non-negative default would have refused
`radial_distance_trend` and `onset_order`, and did nearly refuse a real repaired row.

## The outage this session caused

`shot_folder` was made required on the two validators that resolve recorded gaps. Six
signatures had been widened where two were justified; the excess was reverted **by matching
the signature pattern rather than by naming the functions**, which caught
`inspect_materialization` — in the keep-set. The reinstated `None` default then masked the
one call site that never received the argument, and it died four frames deep in `Path(None)`.

Two hand-rolled textual sweeps reported every call site clean, because that call's body
contains the token `shot_folder` on the `resolutions_path` line without passing it.

Cost: hansa's rematerialization ($1.85, exit 3) and room's layer 2 ($1.70, a complete
candidate of 3 units / 24 contracts / 10 requirements it could not commit), on top of room's
$12.66 that morning from the same shared-tree practice.

Now enforced by three tests: two architecture tests that **parse** every call site and assert
the parameter carries `Parameter.empty`, and an integration test that drives
`finalize_materialization` through the tool a session actually calls.

## Shot state

- **hansa_silk_road** — layer 1 rematerialized clean on `3d4089e`; selected view `2c9f649c…`,
  23 contracts, zero unsatisfiable rows; `camera_path` reset to `pending`; building.
  The impossible `transform_return_delta` bands were not replaced by positive magnitudes —
  the materializer changed instrument to `object_property` plus `curve_derivative_max`,
  which restores the sign the intent needed. Original state preserved at
  `artifacts/_archive/hansa_silk_road-20260905T084619Z-hir0214-boot-refusal-fixture/`.
- **caesar_curia** — pinned worktree, layer 1 passed, layer 2 running, no errors in its
  transcripts as of this writing.
- **room_1046_opening** — layer 1 sealed (`lfc-04165ad6…`), layer 2 not started, holding on
  its user's instruction. Its layer 2 is the live test of HIR-0218 and of RESEARCH-0027.

## Open, all evidenced, none blocking a shot

1. **An unexpected exception in a tool handler reaches the model as an unstructured string.**
   `materialize_mcp.py:603` catches `(ValueError, OSError, json.JSONDecodeError)`; anything
   else escapes the typed boundary with no JSON pointer. Three citations today: an org
   entitlement error retried against a non-transient condition, room's five retries, hansa's
   twelve. Likely shape: an unexpected exception is a typed harness-defect stop that ends the
   phase, not a retryable tool-error string.
2. **`observed_turns` counts `AssistantMessage`s assuming one per turn; the SDK emits one per
   content block.** hansa measured `turns=64/42 cli_num_turns=32`, and
   `thinking(26)+tool_use(31)+text(7) = 64` exactly. The inflation scales with thinking, so it
   cannot be halved away, and the budget is inert — enforcement is the SDK's `--max-turns` on
   its own counter. `cost.jsonl` `turns` is the trustworthy figure. Contradicts HIR-0199 as
   written, and is an SDK-semantics assumption of the kind AGENTS.md says to pin and test.
3. **`summary.json` disagrees with a `cost.jsonl` that is correct, and plan rows null their
   own `phase`.** The standing assumption all day — and the earlier version of this list —
   was "console sums are the only trustworthy numbers". That is true of `summary.json` and
   **false of `cost.jsonl`**, which reproduces console totals to a hundredth of a cent and is
   queryable by `role`, `phase` and layer:

   ```
   caesar, all runs      console (pinned)  $46.7007      cost.jsonl  $46.7010
   summary.json vs cost.jsonl, per run:
     20260905T090459Z-95e903    4.4100  vs   8.0158     understates ~2x
     20260905T095921Z-f6b83e   57.2000  vs  21.9006     overstates ~2.6x
   ```

   Wrong in **both directions**, so it is not a scale factor to correct for. Separately,
   every plan row carries `phase: None` and identifies itself in `role`
   (`plan:plan`, `plan:verify`, `plan:layer`, `plan:materialize`), so a filter on `phase`
   returns nothing for exactly the phases anyone would query — `plan:plan + plan:verify`
   is `$1.1671` against a console figure of `$1.1670`. Both are cheaper to fix than the
   "spend accounting is unreliable" item this list previously carried. Found by the
   caesar_curia driver.

4. **Run manifests record no harness commit.** Two sessions independently could not attribute
   behaviour to a code version and reconstructed it from file mtimes. Note the obvious fix is
   wrong: `git rev-parse HEAD` would have reported `09daa94` for a process importing a mix of
   committed and uncommitted code — a confident wrong answer. The record wanted is a digest of
   the source **as loaded**, which also makes a mixed import detectable.
5. **RESEARCH-0027** — ownership coverage checks whether an owner can *measure* a row, never
   whether it can *cause* it. Now three instances in one layer of one plan.
6. **RESEARCH-0028** — a boot-time falsification is as determined as an in-run one and is not
   dispatched, because no envelope is minted at boot.
7. **Per-row contract findings carry no typed owner**, so a gate refusal on a row with a known
   `owner_layer` resolves plan-wide and the controller refuses it as reviewed.

Items 5, 6 and 7 are one shape: the harness holds information it declines to act on because
it is not in the field the mechanism reads.

## The rule this day earned

Every wrong conclusion today — across four sessions including this one — came from reading
the wrong artifact, and in every case the misleading evidence was **true about something
adjacent**. A prefix does show a string is absent from the prefix. A clean layer seal is
evidence the builder finalizer works. A discriminator returning ABSENT did discriminate,
before the file it keyed on landed in both trees. A textual sweep for `shot_folder` did match
the line that mentions it. A wait loop matching its own `pgrep` pattern was a real process at
0% CPU.

Care does not defend against accurate evidence answering a different question; opening the
artifact does. The edit-side corollary is in AGENTS.md as **shape is not identity**.
