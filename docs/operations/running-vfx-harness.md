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

`vfx preflight --strict` prints one secret-safe
`vfx-harness.environment-result/v2` JSON record. Use `--output <path>` to publish the
same record atomically when another process must consume it. Standalone preflight does
not create a shot run or advance shot authority. `vfx run` instead creates its structured
run before invoking strict preflight, so a failed probe can publish a run-scoped
`infrastructure_failure` stop before any paid stage starts.

Blender is resolved by running `--version` inside the same bubblewrap confinement the worker
uses, so a launcher that only works on the host is refused here with the sandbox's own
diagnostic. Snap installs are the common case: `/snap/bin/blender` needs snapd and capabilities
the confinement withholds, while the package's real binary `/snap/blender/current/blender` runs;
the resolver tries that path automatically, and `BLENDER_BIN` names any other real binary
(HIR-0173).

Stop if preflight fails. Authentication, Blender, or configuration failures can resemble an empty
successful agent session and must not be diagnosed as a VFX-quality problem.

When a run has already stopped on that typed infrastructure failure, repair the named
credential, configuration, or Blender installation externally. Then read the exact key from
`vfx inspect <shot> --run <run-id> --json` and reverify that source run:

```bash
.venv/bin/vfx recover-environment shots/<shot-id> --run-id <run-id> \
  --idempotency-key <key-from-legal-action>
```

This command never edits the environment. It writes `prepared` before probing, records a
durable `running` receipt while recovery is still absent, and commits only after the same
versioned probe surface fully passes. It first selects one exact direct receipt left by a crash
before pointer publication, without repeating that observation. A crash after the commit is
reconciled by key without a second probe. An identical stop in another run converges on the same
semantic transaction despite its different evidence locator. A terminal recovery receipt does
not exempt the next production run from strict preflight.

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
`runs/<run-id>/checkpoints/plans/bundles/<hash>/`, then stages the complete generation and its
gate-attested authority-state effect. One coordinator transaction selects `plans/current.json`
and installs every affected durable-state binding. A failure before pending selection leaves the
previous authority live and retains the candidate as diagnostic run evidence; a crash after
pending selection makes readers fail closed until exact roll-forward recovery completes.
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
`hypothesis_falsified` and stops for a validated replacement through the owning plan or
materialization boundary.

There is no separate state-movement step after plan or JIT publication. The global-plan and
materialization publishers stage semantic authority capsules, exact durable-state before/after
images, and the complete preservation/invalidation effect. The gate attests that proposal, then
one write-ahead authority-state transaction commits both the selected pointer and every affected
state binding. A crash leaves a pending intent that deterministic recovery can only roll forward
to those already staged bytes; it never reruns planning, Blender, render, or judgment (HIR-0171).

When any authority reader reports that this transaction is pending, run the public recovery
boundary before any other plan, build, or evaluation command:

```bash
.venv/bin/vfx recover-authority-state shots/<shot-id>
```

The command selects no alternative and performs no planning, Blender, render, critic, or model
work. It verifies the pending intent and every live member against their exact before/after
identities, rolls forward only the already staged successor, independently evaluates the
resulting commit, republishes the derived `shot.json` `accepted_build` member for the recovered
head, and prints one `vfx-harness.authority-state-recovery-result/v2` record. Its
`coordinator_head_ref`, `intent_ref`, selection token, state-member ids, and
`accepted_build_projection` (`republished` or `current`) are the deterministic recovery evidence.
Repeating it with no pending WAL is a state no-op and reports `already_current` for the same
verified head; the only write it may then make is republishing an accepted-build member left
stale by a death between head selection and that member's republication.

An unchanged completed unit may keep its immutable receipt across a transition that changes a
sibling and therefore the containing layer generation. Every contiguous coordinator edge must
preserve that exact unit capsule, unit digest, receipt digest, and source closure. A terminal
layer receipt is stricter: the complete layer capsule, all constituent receipts, predecessor
terminal bindings, and layer source closure must remain unchanged. If an intermediate generation
invalidates either receipt, a later A-like generation cannot recover it from history.

`vfx units replan` is retired. Do not call an internal `apply_replan`, reinitialize, hand-edit,
or delete durable work-unit state to make selected authority fit. `--discard-accepted` on a
reviewed rematerialization may authorize retiring accepted orphans, but it never authorizes an
out-of-band wipe. The complete `layers.json` hash is not a unit acceptance identity; schema-closed
semantic capsules and the immediate-predecessor binding decide preservation.

A typed `hypothesis_falsified` finding is evidence that new authority may be required, not a
state-mutation receipt. Stop, review the finding and any required human decision, then publish a
validated replacement through the owning global-plan or materialization boundary. That
publication derives and commits the state effect. No public receipt-backed adapter currently
consumes a finding to reopen an unchanged authority generation, and there is no automatic
controller that may infer such permission.

A `keyframe_schedule` empty-key or path miss is a build defect (key sample
path `P` as object `P`, `data.P`, or the data-block fcurve); do not treat it
as rematerialize-only INAPPLICABLE (HIR-0050).
A required `visible_fraction` claim is repaired by a camera unit or the
mutator of those roles, not a volume-only unit (HIR-0051). Rematerialize
the owning layer without `--discard-accepted`: the atomic transition preserves exact unchanged
unit bindings and supersedes the vis-owner closure (HIR-0052). Do not empty-base the layer.

The existing operator-only retry command can reopen a failed or interrupted unit after a
reviewed fix. It preserves the prior outcome and keeps dependants blocked until the retried
unit passes:

```bash
.venv/bin/vfx units retry shots/<shot-id> --layer <layer-id> --unit <unit-id> \
  --reason "<why retry is now valid>" --evidence <locator> [--evidence <locator> ...]
```

This command does **not** consume a `RetryExactUnitTarget`, verify its typed budget/evidence
preconditions, or emit a transaction receipt. It is therefore not the implementation of the
stop-envelope action and must not be called automatically from a stop classification.

### Release an orphaned pre-terminal layer finalization

If a builder process dies after creating a layer-finalization claim but before committing its
terminal receipt, a restart must first fail closed on that active claim. Confirm that no live
builder owns the shot, read the exact `active_claim.claim_id` from the layer's durable state, and
review the crash evidence. Then use the separate finalization-only boundary:

```bash
.venv/bin/vfx finalizations release shots/<shot-id> \
  --layer <layer-id> \
  --claim-id <lfc-...> \
  --reason "<why the dead finalizer may be abandoned>" \
  --evidence runs/<run-id>/<review-evidence-file> \
  --evidence runs/<run-id>/<additional-evidence-file>
```

Every evidence argument must name an existing non-empty file inside the shot. The command
requires the exact current selected generation, coordinator head, active claim, and complete
source-authorized unit receipt closure. It first snapshots each reviewed file create-only under
`state/layer-finalization-releases/evidence/<sha256>` and binds both its original locator and
immutable snapshot identity. It discovers every existing replay receipt for the claim and
captures only the complete ordered contiguous prefix from group zero. Each v2 release-evidence
row binds the canonical locator, file and semantic digests, group index, common planned count,
and v2 replay schema; any gap, duplicate, substitution, or inconsistent count refuses release.
The `vfx-harness.layer-finalization-release-request/v2` and
`vfx-harness.layer-finalization-release-receipt/v2` publish content-addressably, archive the
claim as `released`, and clear only the finalization claim. Their replay and review rows use
`vfx-harness.layer-finalization-release-evidence/v2`. They never reopen, invalidate, or rewrite a unit;
accepted unit completion receipts, checkpoints, scripts, attempt histories, and mutation scopes
remain byte-identical. The next build may mint a fresh claim only at the next monotone
finalization revision, and only after source-verifying the archived release receipt's exact
locator, file SHA-256, semantic digest, archive closure, and every immutable review snapshot.

Do not use this command when a terminal finalization receipt exists; reconcile the terminal
receipt's projections instead. A critic output without a sealed judgment-phase or terminal
receipt is archived only as review evidence and is not reusable exact-once judgment, so a fresh
claim may spend again. The exact same release command is idempotent and resolves the archived
immutable receipt and snapshots without rereading mutable original evidence. A changed reason,
original evidence locator, claim, selection, or active owner fails closed. Missing or changed
snapshots also fail closed. This is not `vfx units retry`, automatic resume, semantic replan,
authority widening, or finding consumption.

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

It performs just-in-time layer materialization, claim-owned unit planning, the deterministic
plan gate, bounded layer building, cumulative acceptance, and final rendering under one run ID.
Legacy distillation queue rows are inert: recipe publication remains disabled until it has an
immutable unit-completion receipt, a staged bounded diff, and post-spend authority revalidation.
It stops on the
first unaccepted boundary; do not force downstream work past it. The child boundary must publish
one immutable `vfx-harness.stop-envelope/v1`, and the whole-run status selects it by digest. A
bare nonzero child exit is a `harness_defect`, not evidence for retry, replan, or recovery. If
envelope publication or read-back fails, status records that the envelope is unavailable and
no action is authorized.

This is a stop boundary, not an automatic recovery loop. There is no public
`--until-accepted` controller or controller journal. The envelope's one typed action names the
only legal route. `recover_environment` is the sole key-consuming receipt-backed adapter and
requires explicit operator invocation after external repair; retry, amendment, replan,
engineering route, resume, and human-decision actions remain non-dispatchable. An operator may
use another existing reviewed command only when its independent authority and preconditions
apply; otherwise the stop remains terminal and is routed to its named human or engineering
owner.

Published global plans contain executable units only for Layer 1. A later layer is selected as a
typed `jit_deferred` boundary with upstream outcome dependencies, reserved semantic roles, and
an ownership-only requirement list. Each deferred requirement names exactly one owner layer and
due boundary without choosing contract kinds or moments. On
`vfx plan <shot> --layer <id>`, the planner materializes and validates
that boundary into a bundle-pinned, content-addressed consumer view. Successful publication
atomically selects that view and its independently evaluated durable-state transition. It does
not perform paid unit planning. `vfx build` first claims a dependency-ready unit,
then owns that unit's paid plan and build under the same exact attempt. `--unit` on `vfx plan` is
retired and points to `vfx build`. If requirement closure, role scope, or global structure disagree,
planning fails closed and no durable unit state is created. Do not hand-author placeholder units or
edit `state/jit-layers/current.json`.

After all exact current unit-completion receipts exist, every layer uses the same claimed
finalization boundary. The harness executes one fresh empty-scene replay for each planned
evaluation group and publishes `vfx-harness.layer-replay-receipt/v2` before any critic consumes
that group. A group binds its own claims/evidence ids, judge points, actual deterministic rows,
reference bytes, and either no raster or an exact `solid | eevee` mode/scale with render capture;
declared auxiliary captures such as motion montages are also hashed sources. Replay-stage failure
publishes a typed failed observation with no invented point, raster, payment, or critic rows.

The group receipts must form an ordered contiguous prefix beginning at zero.
`vfx-harness.layer-evaluation-receipt/v1` derives every group result and may pass only after all
planned groups execute; a failure terminates the prefix. The terminal
`vfx-harness.layer-finalization-receipt/v2` derives its status, canonical evidence, and projection
digest from that exact evaluation before debt, finding, outcome, or ledger projection. Current
layer publication reopens the external evaluation receipt, every external group receipt, replay
inputs and dependencies, references, rendered PNGs, auxiliary captures, layer script,
predecessor outcomes, and sealed revalidation sources. An embedded receipt or `passed` ledger row
cannot compensate for a missing or changed source.

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

Full `vfx accept` publishes `vfx-harness.acceptance-outcome/v1` for the exact selected
bundle and JIT view, accepted script chain, selected moments, and evidence bytes. When all
prerequisites are present but a selected moment fails, full acceptance persists its evidence,
returns `human_decision_required`, and does not let `--repair` mutate unit state. Full
`vfx render` re-resolves and re-hashes that passing outcome
before Blender starts. Missing, failed, or stale acceptance refuses. `vfx render --upto ...`
and `--force` are preview modes and default to the active run's `scratch/previews/`; only a
current full accepted render uses `deliverables/` by default.

## 4. Read output in the supported order

Never begin by recursively listing the shot or grepping every transcript.

```bash
.venv/bin/vfx inspect shots/<shot-id> --list-runs
.venv/bin/vfx inspect shots/<shot-id> --run <run-id> --json
```

For the latest run, read:

1. `runs/latest.json` — selected run ID and terminal state.
2. `runs/<run-id>/manifest.json` — schema, invocation, layout, and authority.
3. `runs/<run-id>/status.json` — `running`, `passed`, `failed`, `interrupted`, or `dry-run`;
   an unaccepted terminal status selects `reports/stop-envelope.json` and its exact digest.
   An interrupted v2 run selects `reports/interruption-receipt.json` and its satisfied
   `reports/interruption-receipt-evaluation.json` by exact digest; that status authorizes no
   transaction, and its reader re-evaluates the run-owned archive before trusting it (HIR-0172).
   Every run is the `vfx-harness.run/v2` generation: `status.json` is `run-status/v2` with only the
   selected record locators and digests, `reports/summary.json` carries `terminal_cause` and the
   plan `outcome`, and Ctrl-C or SIGTERM on a public command ends the run as `interrupted` with
   exit 130 or 143 and a source-verified receipt rather than a generic failure.
4. `runs/<run-id>/reports/summary.json` — decisions, findings, cost, and trajectory.
5. `runs/<run-id>/artifacts.json` — exact catalog for locating supporting detail.

For a failed or interrupted run, resolve the selected stop envelope through the status and
validate its schema, digest, and run identity before taking action. `status.detail`,
`terminal_cause`, process exit code, `contract_gap`, max-turns text, and transcript prose are
diagnostic only. The envelope binds the stable cause, exact attempt evidence, one typed target,
state preconditions, evidence references, dispatch mode, and authoritative progress
postcondition. Schema validity does not make the action automatically dispatchable.

Then open only the necessary category:

- `reports/layers/` for a layer verdict and aggregated telemetry;
- `reports/plan_gate.json` for the final structured plan outcome and repair findings;
- `reports/stop-envelope.json` for the typed terminal cause and only legal route selected by
  an unaccepted status;
- `evidence/renders/` and `evidence/comparisons/` for visual proof;
- `logs/transcripts/` for prompts, tool calls, model output, and errors;
- `logs/console.log` for the chronological operator narrative;
- `checkpoints/` for resume/rollback material;
- `deliverables/` for published video or other final media;
- `scratch/` only for low-level debugging, never as accepted evidence by proximity.

## 5. Diagnose a failed or interrupted run

Use the status-selected stop envelope before deciding what to change:

```text
local_implementation_miss -> retry_exact_unit only when the exact typed retry target exists
authority_defect          -> stop for its named authority owner; reviewed replacement publication moves state atomically
harness_defect            -> route the exact defect packet and evidence to engineering
infrastructure_failure    -> external recovery; session resume only with a future fully sealed resume target and receipt
human_decision_required   -> escalate the exact typed question; automation does not answer it
```

Current owning classifiers cover structural global-plan rejection, structural JIT
materialization rejection, builder/composition hypothesis falsification, strict preflight,
and failed full-acceptance moments. Other terminal paths deliberately fall back to
`harness_defect`; never refine that fallback from a legacy label or exit code.

For one layer's action timeline:

```bash
.venv/bin/vfx inspect shots/<shot-id> --run <run-id> --layer <layer-id>
```

Automatic checkpointed-session resume is not currently a legal recovery transaction. The
legacy ledger row does not seal selected bundle/view, exact unit and unit-plan digest,
candidate, model session and phase, and durable write-ahead-log identity, and no public
transaction emits the required `prepared -> running -> terminal` receipt. Start a new run from
the fault-owning unit under the existing operator procedure. The manual `vfx units retry`
command above is not a typed resume or automatic retry transaction. Never copy a random old
render, snapshot, or script into the current run and call that a resume.

### Reconcile a run whose owner died

A run that still says `running` after its process is gone is reconciled only by acquiring its
recorded owner fence; nothing about the process id, the status age, or a quiet transcript has
authority:

```bash
.venv/bin/vfx reconcile shots/<shot-id> --run-id <run-id>
```

The command runs as its own owned run and prints one typed reconciliation result. `owner_live`
means the fence is still held and nothing was written; `owner_lost` means the released fence was
acquired and the run now selects an action-free `owner_lost` receipt with a satisfied evaluation;
`completed_prepared` means the owner had already published its receipt and evaluation and only
the status selection was missing; `already_terminal` returns the receipt or terminal status that
already exists. A legacy run without an owner claim is not reconcilable (HIR-0172).

## 6. Authority and editing rules

- Edit `brief.md` and `refs/` only to change authored intent.
- Edit plans/contracts through planning, amendment, or an explicit reviewed repair.
- Treat `build/` and `shot.json` as the current accepted deterministic chain and ledger.
- Never write, remove, or replace the live `<shot>/shot.json` or its `shot.json.lock` directly or
  through a generic durable-file helper. Canonical writes use the typed shot-ledger transport;
  isolated consumer or candidate copies must prove a physically different root. The
  `accepted_build` member of `shot.json` is the strict accepted-build index; it is derived by the
  harness at plan and JIT republication, layer finalization, acceptance, and authority-state
  recovery, and re-derived by every reader, so never edit it and never read it as proof without
  that re-derivation. The rest of the file is still a fenced legacy projection.
- Plan-consumer scratch views require Linux 5.17+ with unprivileged fanotify target-FID reporting
  and `openat2` on a local filesystem that exports file handles. An unsupported kernel, filesystem,
  or sandbox fails closed at the first consumer-view allocation; `vfx preflight --strict` does not
  yet probe this capability.
- Treat `state/` as durable cross-run operational state.
- Treat `runs/` as generated audit evidence. Do not hand-edit a run to make it pass.
- Ignore shot-root `logs/`, `renders/`, `.artifacts/`, `.snapshots/`, and `.versions/`; they are
  unsupported and have no decision authority.

After changing harness behavior, follow `improvement-lifecycle.md`: reproduce the cause, update an
HIR/ADR as needed, add regression evidence, run the full checks, and record the user-visible result
in the changelog.
