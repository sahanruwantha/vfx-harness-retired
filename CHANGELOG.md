# Changelog

All notable changes to VFX Harness are recorded here. Detailed causal reasoning and validation
live in the linked Harness Improvement Records.

## Unreleased

### Changed

- Rematerialization writes a reverted overlay as the design base and selects only
  when the replacement publishes: a crash, truncation, or broken pipe leaves the
  previously selected view. Materialization sessions also deny Task/Agent
  ([HIR-0026](docs/improvements/HIR-0026-remat-revert-must-not-select-a-hole.md)).
  An exhausted session does not publish because a candidate file exists
  ([HIR-0027](docs/improvements/HIR-0027-max-turns-must-not-publish.md)).
  Structured `values.contract` adoption is last-write-wins for the selected bundle:
  a prior generation's row is inert, a later superseded or falsified row retires
  the id, and materialization copies the compiled binding set
  ([HIR-0028](docs/improvements/HIR-0028-structured-decisions-bind-the-selected-bundle.md)).
- The active work unit is compiled into one scope card — mutation surface, bound
  contracts, claims, judge frames, and `run_bpy` helper signatures — shared by
  kickoff, `CLAUDE.md`, and the `unit_scope` tool
  ([HIR-0025](docs/improvements/HIR-0025-unit-scope-was-a-translation-job.md)).
- An empty `path_clearance_min` obstacle selection is not a passing clearance: the 1e9
  sentinel never PASSes, and authoring refuses sentinel-scale or zero-floor bounds
  ([HIR-0024](docs/improvements/HIR-0024-empty-path-clearance-is-not-a-pass.md)).
- An audited `vfx units retry` of a unit whose executable rows already pass may mutate
  until the first in-session verdict: the convergence guard no longer treats a failed
  qualitative claim as sealed work
  ([HIR-0021](docs/improvements/HIR-0021-a-reopened-unit-may-do-the-work-its-retry-prescribes.md)).
- Materialization findings are pointer-addressed and returned together: the write-hook and
  `patch_materialization` operate on RFC 6901 locations in the candidate file, not a
  one-error-per-rewrite walk and not Glob of prior bundles
  ([HIR-0023](docs/improvements/HIR-0023-materialization-findings-are-pointer-addressed.md)).
- Made a semantic role one dotted token: `_bvfx_role` rejects commas and other
  non-token characters (CSV is not membership), contract selectors refuse the same
  punctuation at authoring, and `inspect_scene` / `check_scene` / `list_keyframes`
  address `role=` through the one matcher, naming present names and roles on a miss
  ([HIR-0022](docs/improvements/HIR-0022-a-comma-joined-role-was-stored-as-one-token.md),
  [HIR-0018](docs/improvements/HIR-0018-selector-diagnostics-say-both-sides.md)).
- Added `visible_fraction`, occlusion-true visibility evidence (camera-ray fraction of a
  subject's on-screen surface samples), and made every judge frame require it at
  materialization: a whole lookdev layer had been judged at frames where every subject sat
  behind a solid blockout disc, invisible to projection-only bbox rows
  ([HIR-0019](docs/improvements/HIR-0019-judge-frames-must-prove-visibility.md)).
- Made every selector and socket miss report both sides: node/control misses enumerate the
  semantic tags actually present in the searched graphs, socket misses enumerate the node's
  real sockets plus the literal-`'Value'` resolution rule, measured zeros carry the same
  enumeration as notes through evidence, probes, and verdicts. `probe_control` now renders
  the one canonical control resolver instead of a private near-copy, selectors match control
  and role tags either-of (a control tag no longer shadows a node's role), and an auto-socket
  response row sharing its selector with a socket-pinned sibling is refused at authoring and
  advisory at the gate
  ([HIR-0018](docs/improvements/HIR-0018-selector-diagnostics-say-both-sides.md)).

- Taught the plan gate the verified materialization lifecycle, added three evidence kinds
  (motion smoothness, persistent path clearance, parallax profile) with fail-closed vacuity
  linting, and gave planning/repair sessions decision-grade instruments — `evidence_vocabulary`,
  `gate_preview`, `probe_candidate` (with the `rig_contract` check), typed vocabulary-gap
  escalation, reproduction-carrying failures, and repair-session recipes. One hermetic fixture
  now drives a full authority generation end-to-end in the suite
  ([HIR-0017](docs/improvements/HIR-0017-lifecycle-aware-authority-and-agent-instruments.md)).
- Made `vfx units replan` able to express generation supersession under unit-first authority:
  when both bundles carry empty layer DAGs, the old identity is durable state's own recorded
  plan hash, state units absent from the new generation are superseded with audit ("orphaned"
  in the replan record), and retiring an accepted orphan requires `--discard-accepted` or a
  typed falsification record.
- Taught bundle resolution the `plans/ownership_mapping.json` member the publisher already seals,
  and made publication refuse any member resolution cannot read — the first mapping-carrying
  bundle published as clean and then failed closed for every consumer (the
  [HIR-0016](docs/improvements/HIR-0016-gate-attested-unit-plan-publication.md) writer/reader
  class at the bundle boundary).
- Made JIT unit-plan publication a two-phase transaction: the deterministic gate runs inside
  generation, a clean result earns a gate attestation in the authority sidecar (schema v2), a
  dirty result rolls the shot back, and every build-time consumer refuses unattested plans
  ([HIR-0016](docs/improvements/HIR-0016-gate-attested-unit-plan-publication.md)).
- Made every scene-contract probe row measure its declared frame with its own depsgraph, and
  replaced raw vertex projection with one frustum-clipped, fail-closed implementation shared by
  authoritative `bbox_*` evidence and the advisory framing checks
  ([HIR-0015](docs/improvements/HIR-0015-declared-frame-and-frustum-truth.md)).
- Made repository-root `.env` loading resolve the checkout root instead of `src/`.
- Made `vfx plan --until-clean` exit 3 and publish a failed run when blocking findings remain.
- Made the plan gate report every unknown work-unit dependency in one repair brief instead of
  revealing one invalid layer per paid repair round.
- Persisted final plan-gate authority in `reports/plan_gate.json` and terminal run metadata.
- Added warm-session validation for planner machine artifacts and a bounded read-only repair gate.
- Unified reference fingerprints under the typed `vfx-harness.look-vector/v1` metric registry.
- Added temporal scene contracts, rendered frame-delta evidence, and temporal claim coverage.
- Added projected-composition and mutation/fault-ownership coverage warnings.
- Added run-owned content-addressed plan bundles, atomic `plans/current.json` publication, and
  run-isolated repair snapshots as the first ADR-0004 migration slice.
- Declared global planner role capabilities so draft, verify, and repair can all patch artifacts
  and call the bounded deterministic gate while their context is warm.
- Moved global-plan authoring into an authored-input-only workspace owned by each run, preventing
  prior shot-root plans, contracts, questions, and run files from leaking into a fresh pass.
- Isolated generated output under `runs/<run-id>/` with stable log, report, evidence,
  checkpoint, scratch, and deliverable categories.
- Added manifest, status, summary, artifact-index, and latest-run metadata for machine readers.
- Added explicit run selection and run listing to `vfx inspect` while retaining legacy readers.
- Added model-free, gate-checked promotion of retained clean plan candidates into fresh
  run-owned immutable bundles.
- Added `vfx units replan` for fail-closed migration of durable work-unit state between an
  explicitly named old bundle and current selected plan authority.
- Added audited failed-unit retry transitions and made direct build runs fail when any requested
  work unit remains unaccepted.
- Redefined plan cleanliness as structural authority, added explicit decision strengths and typed
  `hypothesis_falsified` work-unit outcomes, and bound transactional replanning to those immutable
  executable findings while preserving hard-constraint approval.
- Routed terminal unit failures whose failing bound contracts are a decision's declared
  falsification path into the same typed `hypothesis_falsified` outcome, so an unreachable
  approved or planner start stops as replanning evidence instead of a generic unit failure.
- Scoped global-plan recipe selection, spikes, and numeric check calibration to Layer 1 and
  cross-layer DAG facts; later-layer execution detail now waits for its JIT pass and upstream
  checkpoints instead of being simulated before the first build.
- Replaced deferred-layer contract promises with ownership-only requirement registration.
  Global publication now rejects later-layer evidence design and fingerprints; JIT
  materialization closes each owned requirement with required producing evidence or a typed
  decision and extends the cumulative acceptance view.
- Bounded global-plan session economics: spikes now carry a session ceiling and one failed
  retry per hypothesis (with a contract-kind reference on invalid rows), reference
  fingerprints are computed once per run and reused across draft and verify, and
  repeated gate signatures stop the in-session edit loop, `VFXH_PLAN_MAX_TURNS` defaults to 24,
  and verification is separately bounded to 12 turns.
- Capped image-check calibration at an initial batch plus one repair batch per plan session;
  when the ceiling closes, both calibration tools refuse with instructions to drop unresolved
  optional image checks and proceed on executable scene contracts and build-time falsification.
- Made reference ingestion follow execution scope: global kickoff no longer embeds every future
  approval image, `measure_ref` refuses references outside ready units, and JIT materialization
  receives only its layer's judge references.
- Enforced spike eligibility at the tool boundary instead of prompt prose: adopted decision
  values, decision falsification paths, self-fulfilling existence/count/rendered-response
  contracts, and proxy lighting/visibility reads are refused deterministically, and budget
  identity keys the semantic hypothesis so renaming a contract cannot buy another attempt.
- Made the subscription token (`CLAUDE_CODE_OAUTH_TOKEN`) the default Claude credential when
  both are configured, withholding `ANTHROPIC_API_KEY` from the SDK unless
  `VFXH_CREDENTIAL=api_key` selects it; preflight reports the applied selection.
- Limited plan spikes to optional citation-integrity evidence: claimed spikes now freeze exact
  script, output, Blender identity, and contract rows, while unspiked composition work may proceed
  to its producing runtime unit.
- Moved the first layer across the JIT boundary: schema-5 global plans publish only the layer DAG,
  ownership, durable constraints, and blockers; dependency roots materialize without fictional
  upstream outcomes, and image-check calibration is unavailable until a real candidate exists.
- Added typed terminal causes for operator interruption, model-turn exhaustion, plan-gate stalls,
  plan-budget exhaustion, usage limits, and process errors in run status and summaries.
- Made build-time readings unable to overstate themselves: contract rows reject keys the
  harness ignores and must declare the frame they read; `onset_order` rejects selectors
  that compare a set against itself; every metric declares the evidence domain it can
  certify and every required claim declares the domain it asserts, so a count cannot
  close a timing claim; units declare look capabilities instead of having them guessed
  from axis names, and declared appearance ownership requires candidate-bound image
  evidence; required evidence that was never produced blocks sealing instead of passing
  by absence; finalization is bounded to the selected checkpoint's journal prefix; and
  mutation scope is checked against the active unit on every path, reported live on the
  call that violates it.
- Put global planning on an authoring diet: the model writes one compact ownership/DAG
  mapping and the harness mechanically generates clause ids, exact citations, the
  requirements register, all-deferred schema-5 layers with derived `owned_requirements`,
  routing axes, empty evidence documents, and a rendered `plans/global.md` on every
  mapping write, with enumerated validation errors fed back warm; writes outside the
  mapping are denied, the verifier audits the mapping inside the default 6-turn ceiling,
  and expansion from a valid mapping passes the deterministic gate by construction on
  heterogeneous fixture families.
- Folded the SDK session-result subtype into the collected failure signal and widened the
  classifier to the SDK's raised "maximum number of turns" phrasing, so a real max-turns
  termination is labeled `max_turns_exhausted` immediately instead of burning retry sessions
  and reporting `session_stalled`; aligned global repair/verify kickoffs with the sparse
  contract by removing instructions to re-prove calibration and spike evidence those roles can
  no longer produce.

See [HIR-0002](docs/improvements/HIR-0002-structured-run-output.md),
[HIR-0003](docs/improvements/HIR-0003-truthful-until-clean-planning.md),
[HIR-0004](docs/improvements/HIR-0004-checkout-root-environment-loading.md),
[HIR-0005](docs/improvements/HIR-0005-plan-gate-authority-and-warm-repair.md),
[HIR-0006](docs/improvements/HIR-0006-canonical-reference-fingerprints.md),
[HIR-0007](docs/improvements/HIR-0007-temporal-and-ownership-evidence-coverage.md),
[HIR-0008](docs/improvements/HIR-0008-transactional-plan-publication-foundation.md),
[HIR-0009](docs/improvements/HIR-0009-run-scoped-plan-authoring.md),
[HIR-0010](docs/improvements/HIR-0010-executable-plan-authority-and-due-gates.md),
[HIR-0011](docs/improvements/HIR-0011-build-time-plan-falsification.md),
[HIR-0012](docs/improvements/HIR-0012-sparse-global-publication-contract.md),
[HIR-0013](docs/improvements/HIR-0013-unit-first-evidence-materialization.md),
[HIR-0014](docs/improvements/HIR-0014-instruments-that-cannot-lie.md),
[ADR-0002](docs/decisions/ADR-0002-run-scoped-artifact-authority.md),
[ADR-0003](docs/decisions/ADR-0003-explicit-metric-and-temporal-evidence-identity.md),
[ADR-0004](docs/decisions/ADR-0004-transactional-plan-authority.md),
[ADR-0005](docs/decisions/ADR-0005-sparse-global-publication-contract.md),
and [ADR-0006](docs/decisions/ADR-0006-unit-first-evidence-materialization.md).

## 0.3.0 — 2026-08-21

### Changed

- Renamed the project, package, commands, and environment prefix from the former project name to
  VFX Harness, `vfx-harness`, `vfx_harness`, `vfx`, and `VFXH_*`.
- Renamed the GitHub repository to `sahanruwantha/vfx-harness`.
- Reorganized runtime modules by decision responsibility.
- Separated architecture, decisions, improvements, operations, and research documentation.
- Separated tracked evaluation definitions from generated evaluation evidence.
- Categorized tests as unit, contract, architecture, and integration guarantees.

See [HIR-0001](docs/improvements/HIR-0001-project-identity-and-repository-structure.md) and
[ADR-0001](docs/decisions/ADR-0001-repository-authority-boundaries.md).
