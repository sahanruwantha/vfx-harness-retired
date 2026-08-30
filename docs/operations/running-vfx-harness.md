# Running VFX Harness

This is the canonical operating procedure for developers and coding agents. Run commands from
the repository root and use the public `.venv/bin/vfx` CLI. Every command that produces output
owns a structured run under the shot; do not invoke implementation modules to invent a different
workflow.

## 1. Establish the shot and environment

A shot lives at `shots/<shot-id>/` and requires:

```text
brief.md                 authored specification with frames/fps frontmatter
refs/                    authored visual references
```

Set credentials and model/runtime configuration in the repository `.env` or real environment.
When both Claude credentials are configured, the harness selects the subscription token
(`CLAUDE_CODE_OAUTH_TOKEN`) by default and withholds `ANTHROPIC_API_KEY` from the SDK; set
`VFXH_CREDENTIAL=api_key` to bill API credits instead. `VFXH_PLAN_MAX_TURNS` (default 24)
is the hard turn ceiling for one global plan session; `vfx plan --max-turns` overrides it per
invocation. Verification is separately bounded by `VFXH_PLAN_VERIFY_MAX_TURNS` (default 12).
Confirm Python dependencies and Blender before spending model budget:

```bash
.venv/bin/vfx preflight --strict
.venv/bin/ruff check src tests
.venv/bin/python -m pytest -q
```

Stop if preflight fails. Authentication, Blender, or configuration failures can resemble an empty
successful agent session and must not be diagnosed as a VFX-quality problem.

## 2. Create and gate the global plan

```bash
.venv/bin/vfx plan shots/<shot-id> --until-clean
.venv/bin/vfx evals plan shots/<shot-id>
```

Every global planning invocation creates an authored-input-only workspace at
`runs/<run-id>/scratch/plan-workspace/`. Draft, verify, repair, write-time hooks, and the
deterministic gate operate there; only `brief.md` and `refs/` are staged automatically. Prior
plans, contracts, questions, builds, and run output are not implicit planning input. Planner
writes outside this workspace are denied.

A gate-clean untagged `--until-clean` run freezes `plans/global.md`, its five execution
contracts, typed requirements/obligations/assumptions, every nested Markdown unit/evidence plan,
and authored-input provenance from that workspace into
`runs/<run-id>/checkpoints/plans/bundles/<hash>/`, then atomically
selects the complete generation through `plans/current.json`. A failed or interrupted run leaves
the previous pointer unchanged and retains its own candidate as diagnostic run evidence.
`--until-clean` exits 3 and records the run as failed if the gate stalls or exhausts its repair
budget with blocking findings. Planning status and summary records carry `outcome`,
`blocking_count`, and `plan_gate_report`; read `reports/plan_gate.json` for the reusable finding
set without rerunning the gate. Build and evaluation consumers resolve this pointer and fail
closed on incomplete or stale selected authority. Never copy a bundle back onto the shot-root
compatibility files by hand. An unresolved typed obligation or assumption blocks the dependency
boundary declared in its due gate.

`clean` means the plan is structurally executable: its requirements, DAG, owners, scopes,
contracts, decision strengths, and due boundaries close. It does not mean future scene-dependent
targets have already passed. The earliest producing unit evaluates those targets in the real
cumulative scene. A local miss stays inside bounded repair; a miss requiring new authority records
`hypothesis_falsified` and stops for transactional replanning.

When a newly published plan changes a layer DAG that already has durable work-unit state,
move that state through the explicit replan transaction before execution. Name the exact
immutable bundle that produced the old state; the command verifies its manifest and layers
hash against durable state, compares it with current selected authority, preserves only
unchanged/unaffected checkpoints, and atomically records the full supersession closure:

```bash
.venv/bin/vfx units replan shots/<shot-id> --layer <layer-id> \
  --base-run <old-plan-run-id> --base-bundle <old-plan-content-hash> \
  --owner <authority> --trigger "<why the DAG changed>" \
  --evidence <locator> [--evidence <locator> ...]
```

When execution emitted a typed plan finding, consume it directly so the transaction verifies its
bundle, DAG, unit, and dependency identities. A finding involving a hard constraint additionally
requires an explicit human approval locator:

```bash
.venv/bin/vfx units replan shots/<shot-id> --layer <layer-id> \
  --base-run <old-plan-run-id> --base-bundle <old-plan-content-hash> \
  --owner <authority> --trigger "executable hypothesis falsified" \
  --falsification state/work-units/hypothesis-falsifications/<record-id>.json \
  [--hard-constraint-approval <human-decision-evidence>]
```

Add `--preview` to validate the same authority and print the added, removed, changed, invalidated,
and preserved unit sets without publishing the state transaction.

Do not reinitialize, hand-edit, or delete stale work-unit state to make a new DAG fit.
When only the combined `layers.json` hash changed and this layer's unit IDs and
digests still match (a sibling rematerialization), `vfx build` adopts the new
hash and preserves statuses; do not empty-base-replan that layer (HIR-0040).
A JIT-layer finding's `plan_hash` is the selected view hash (durable
work-unit `plan_hash`), not sha256 of the sparse global `layers.json`.
`--preview` must show the finding's unit and affected closure as invalidated;
an unrelated passed sibling stays preserved (HIR-0049).
A `keyframe_schedule` empty-key or path miss is a build defect (key sample
path `P` as object `P`, `data.P`, or the data-block fcurve); do not treat it
as rematerialize-only INAPPLICABLE (HIR-0050).
A required `visible_fraction` claim is repaired by a camera unit or the
mutator of those roles, not a volume-only unit (HIR-0051). Rematerialize
the owning layer without `--discard-accepted`: matching accepted digests
stay, the vis-owner closure is superseded (HIR-0052). Do not empty-base
the layer, and do not consume a finding first unless the replacement
leaves that unit's digest unchanged.

When a deterministic failure has been fixed, or an in-flight planning/build/repair session was
interrupted, reopen the same unit through the audited retry transition. This preserves the prior
outcome and keeps dependants blocked until the retried unit passes:

```bash
.venv/bin/vfx units retry shots/<shot-id> --layer <layer-id> --unit <unit-id> \
  --reason "<why retry is now valid>" --evidence <locator> [--evidence <locator> ...]
```

If a retained gate-clean candidate predates a publication fix, promote it through a new,
model-free run instead of editing its immutable bundle or paying to author the same plan again:

```bash
.venv/bin/vfx plan shots/<shot-id> --promote-run <source-run-id>
.venv/bin/vfx evals plan shots/<shot-id>
```

Promotion accepts only a terminal clean planning run whose isolated workspace still matches the
current authored `brief.md` and `refs/`. It copies the complete authority surface into a fresh
run-owned workspace, applies the current deterministic gate, freezes a new bundle, and only then
atomically selects it. A rejected promotion leaves the existing pointer unchanged.

New acceptance fingerprints are typed `{metric_set, values}` records using
`vfx-harness.look-vector/v1`. Legacy prose remains readable, but a new plan must copy canonical
metric ids and values returned by `measure_ref`.

If planning raises client questions, inspect and answer them before the affected layer:

```bash
.venv/bin/vfx escalate shots/<shot-id>
.venv/bin/vfx escalate shots/<shot-id> --answer <id> "<decision>"
```

## 3. Run the production chain

The normal operation is the whole driver:

```bash
.venv/bin/vfx run shots/<shot-id> --rounds 2
```

It performs just-in-time layer planning, the deterministic plan gate, bounded layer building,
cumulative acceptance, final rendering, and queued distillation under one run ID. It stops on the
first unaccepted boundary; do not force downstream work past it.

Published global plans contain executable units only for Layer 1. A later layer is selected as a
typed `jit_deferred` boundary with upstream outcome dependencies, reserved semantic roles, and
an ownership-only requirement list. Each deferred requirement names exactly one owner layer and
due boundary without choosing contract kinds or moments. On
`vfx plan <shot> --layer <id>`, the planner first materializes and validates
that boundary into a bundle-pinned, content-addressed consumer view; only then does it create or
plan the first ready unit. If requirement closure, role scope, or global structure disagree,
planning fails closed and no durable unit state is created. Do not hand-author placeholder units or
edit `state/jit-layers/current.json`.

Useful bounded operations:

```bash
# Preview which layers would execute without calling models or Blender.
.venv/bin/vfx run shots/<shot-id> --from 1 --upto 3 --dry-run \
  --skip-accept --skip-render

# Re-run from the first invalid layer after fixing its cause.
.venv/bin/vfx run shots/<shot-id> --from <layer-id>

# Direct stages are supported and still create structured runs.
.venv/bin/vfx build shots/<shot-id> --layer <layer-id>
.venv/bin/vfx accept shots/<shot-id>
.venv/bin/vfx render shots/<shot-id>
```

`--force` is for a bounded debugging experiment only. Its results do not prove that an incomplete
or unaccepted chain is a deliverable.

## 4. Read output in the supported order

Never begin by recursively listing the shot or grepping every transcript.

```bash
.venv/bin/vfx inspect shots/<shot-id> --list-runs
.venv/bin/vfx inspect shots/<shot-id> --run <run-id> --json
```

For the latest run, read:

1. `runs/latest.json` — selected run ID and terminal state.
2. `runs/<run-id>/manifest.json` — schema, invocation, layout, and authority.
3. `runs/<run-id>/status.json` — `running`, `passed`, `failed`, `interrupted`, or `dry-run`.
4. `runs/<run-id>/reports/summary.json` — decisions, findings, cost, and trajectory.
5. `runs/<run-id>/artifacts.json` — exact catalog for locating supporting detail.

Then open only the necessary category:

- `reports/layers/` for a layer verdict and aggregated telemetry;
- `reports/plan_gate.json` for the final structured plan outcome and repair findings;
- `evidence/renders/` and `evidence/comparisons/` for visual proof;
- `logs/transcripts/` for prompts, tool calls, model output, and errors;
- `logs/console.log` for the chronological operator narrative;
- `checkpoints/` for resume/rollback material;
- `deliverables/` for published video or other final media;
- `scratch/` only for low-level debugging, never as accepted evidence by proximity.

## 5. Diagnose a failed or interrupted run

Use the status and summary before deciding what to change:

```text
preflight/config failure  -> correct environment; do not edit VFX logic
plan gate failure         -> repair plan/contracts; rerun the gate
builder evidence failure  -> inspect the owning layer report and cited evidence
hypothesis_falsified       -> publish amended authority; consume the typed finding with units replan
canonical replay failure  -> repair deterministic script/checkpoint mechanism
acceptance failure        -> route to the declared fault-owning layer
process interruption      -> inspect the last checkpoint, journal, and final transcript events
```

For one layer's action timeline:

```bash
.venv/bin/vfx inspect shots/<shot-id> --run <run-id> --layer <layer-id>
```

Resume a truncated direct builder session only when its ledger resume record names an existing
checkpoint and journal:

```bash
.venv/bin/vfx build shots/<shot-id> --layer <layer-id> --resume
```

Otherwise start a new run from the fault-owning layer. Never copy a random old render, snapshot,
or script into the current run and call that a resume.

## 6. Authority and editing rules

- Edit `brief.md` and `refs/` only to change authored intent.
- Edit plans/contracts through planning, amendment, or an explicit reviewed repair.
- Treat `build/` and `shot.json` as the current accepted deterministic chain and ledger.
- Treat `state/` as durable cross-run operational state.
- Treat `runs/` as generated audit evidence. Do not hand-edit a run to make it pass.
- Ignore shot-root `logs/`, `renders/`, `.artifacts/`, `.snapshots/`, and `.versions/`; they are
  unsupported and have no decision authority.

After changing harness behavior, follow `improvement-lifecycle.md`: reproduce the cause, update an
HIR/ADR as needed, add regression evidence, run the full checks, and record the user-visible result
in the changelog.
