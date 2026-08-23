---
id: HIR-0013
title: Move first-unit evidence design across the candidate boundary
status: proposed
introduced_in: unreleased
date: 2026-08-23
failure_class: first_layer_preproduction_before_any_authoritative_mutation
mechanism: root_layer_jit_and_phase_routed_evidence_tools
adr: ADR-0006
---

# Move first-unit evidence design across the candidate boundary

## Observation

Run `20260823T065933Z-844d62` validated ADR-0005's sparse whole-shot boundary but then spent
eleven minutes inside first-layer preproduction: nine recipe searches, three image-check batches,
three exploratory checks, eighteen rejected candidates, and no plan artifact. The only available
"adversaries" were sibling approval stills. The run was stopped and recorded as a generic
`failed`, exit 1, empty detail, making interruption indistinguishable from a crash.

## Root cause

The global contract still treated the first layer as special executable authority and exposed
image calibration before any candidate existed. Attempt caps bound the cost but do not move proof
to the side of the execution boundary where it can exist. Terminal observability also collapsed
distinct causes because the invocation wrapper serialized `str(KeyboardInterrupt())`.

## Mechanism

1. Permit dependency-root `jit_deferred` layers with empty upstream dependencies/outcomes.
2. Require global publication to contain no ready stages, checks, scene contracts, acceptance
   fingerprints, recipes, or spikes unless a separately recorded durable global exception needs
   executable adoption.
3. Remove `measure_ref`, `measure_check(s)`, recipes, and spikes from global draft/verify/repair
   capabilities. JIT materialization receives only due references and mechanism tools; candidate
   image checks remain a build/evidence responsibility.
4. Preserve fixed pre-mutation propositions and scope. Materialization must still fail closed on
   owned-requirement closure and required producing evidence where that evidence is executable
   without a candidate.
5. Publish typed terminal causes (`interrupted`, `max_turns_exhausted`, `gate_stalled`,
   `plan_budget_exhausted`, `usage_limit`, `process_error`) in status and summary.
6. Reduce global draft/verify ceilings to 12/6 turns. Layer materialization keeps its own
   budget because it is the first phase allowed to make implementation decisions.

## Acceptance

- A global bundle with all layers deferred and one dependency root passes the deterministic gate.
- A root layer materializes without a fabricated upstream outcome.
- Global planner roles cannot call image calibration, recipes, or spikes.
- A heterogeneous static fixture and a camera-driven fixture use the same contracts without
  subject vocabulary in core code.
- Interrupted runs are distinguishable from process failures in `summary.json`.
- The current shot reaches first authoritative scene evidence faster than the stopped-run
  baseline; do not mark this HIR accepted from unit tests alone.

## Implementation audit — 2026-08-23

Two defects were found auditing the landed mechanism before the live acceptance run:

1. `max_turns_exhausted` was unreachable in production. The classifier regex reads the
   collected assistant text, but the SDK reports max-turn termination only as
   `ResultMessage.subtype`, which reaches the transcript and never the signal — so a real
   exhaustion classified as unknown, retried one full session, and was mislabeled
   `session_stalled`. The unit test masked this by feeding the subtype as assistant prose.
   Fixed by `result_signal`: every planner session collector appends the result subtype to
   the signal handed to `run_session`, proven by a duck-typed `ResultMessage` test that
   exercises the collector contract rather than the regex alone.
2. The global repair addendum and verify kickoff still instructed roles to re-prove
   `measure_checks` proofs and evidence-check spikes — tools removed from every global
   role by this HIR. Rewritten to the sparse charter: ownership, citations, dependencies,
   and rejection of premature executable design.

Live run `20260823T082044Z-22d8ab` (first uninterrupted run under this mechanism) then
exposed a second hole in defect 1 and one budget miscalibration:

- The SDK also reports max turns by *raising* after the result message — the collected
  signal (subtype included) is discarded on the exception path, and the prose "Reached
  maximum number of turns (12)" did not match the old regex, so four full sessions were
  burned and the run was labeled `session_stalled`. The regex now covers both shapes and
  the exception-path test uses the exact SDK phrasing.
- The sparse boundary itself held end to end: no calibration, recipes, spikes, or
  whole-shot analysis anywhere in ~32 minutes. Every attempt instead hit the reduced
  12-turn draft ceiling while serially authoring the eight sparse artifacts through
  write-time validation repairs; the final attempt was roughly two turns short with all
  seven machine artifacts on disk. The 12-turn default assumed fewer authoring
  round-trips than the artifact surface requires; the next acceptance attempt uses the
  documented `--max-turns` override while the right default (batched writes vs. a higher
  ceiling) is decided from its evidence.

Run `20260823T085630Z-1c18c2` (`--max-turns 24`) advanced one boundary further:

- The draft wrote the full sparse surface and self-gated down to 3 blocking findings in
  30 minutes — zero fingerprints, zero checks, ten resolving citations. Two of the three
  findings were one root cause: `required_outcomes[].kind` rejected by a loader whose
  message named the constraint but not its accepted values, which a workspace-confined
  planner has no other way to discover. It burned the remaining turns guessing
  spellings. The loader error now enumerates
  `scene_contract`/`image_contract`/`semantic_diff`, with a regression test.
- The verify session exhausted its 6-turn ceiling before completing the audit-and-write
  contract (read brief, draft, and eight artifacts; gate; write the superseding plan).
  It was correctly labeled `max_turns_exhausted` on the first attempt with no retry burn
  — the widened classifier's first production confirmation. Six turns is structurally
  below verify's own contract; the next attempt raises it per invocation while the
  default is revisited with the run evidence.
- The path-scope hook denied a first write aimed at the shot root (the staged workspace
  manifest exposes the absolute shot path, which invites exactly this) and denied
  workspace-escape globs; isolation held in both directions at a cost of ~4 turns.

Run `20260823T093656Z-35a68c` (enum fix live) exposed the dependent-layer twin of
mechanism 1's root-layer rule — a jointly unsatisfiable pair hidden behind the enum
wall: the loader demanded non-empty `required_outcomes` whenever dependencies exist,
while `evidence_owners` is built from ready layers' stages and is therefore always empty
in an all-deferred document, rejecting every possible id as unknown upstream evidence.
The planner obeyed the prompt's "names the sealed outcomes it genuinely consumes" and
fabricated requirement ids as contract bindings until its budget died. Reconciled to the
ADR's own boundary: the dependency edge is global; the outcome binding is chosen at the
dependent layer's materialization. The loader now requires named outcomes only from
layers with ready dependencies, teaches the legal empty shape in its error, the prompt
matches, and an offline replay of the run's real `layers.json` (bindings emptied) loads
clean. Regression tests cover the empty-legal, deferred-reference, and ready-dependency
paths.

Run `20260823T095834Z-7040c2` (loader reconciled) surfaced the fourth and last known
jointly-unsatisfiable pair: `decision-adoption` demanded every structured satisfied
decision (A2's camera spine) be copied into a scene contract bound to a required unit
claim, while the schema-5 rules require `scene_checks.json` empty and publish no units to
bind to. The verify session, repairing honestly, was writing that forbidden scene
contract when its budget died. Reconciled on the same boundary as before: at global time
the gate now proves ownership — some deferred layer's reserved namespaces must cover
every role the decision mutates, else blocking — and `validate_materialization` enforces
the verbatim copy (exact `values.contract` fields plus `decision_id`, bound to a required
claim by the existing contract-claim rule) when the owning layer materializes. Bundles do
not freeze the decision ledger, so the validator takes an explicit `resolutions_path` and
`publish_materialization` passes the shot's durable `state/plan-resolutions.jsonl`.
Offline replay of the run's workspace shows zero decision-adoption findings and only
model-repairable artifact defects remaining.

Run `20260823T104236Z-270652` (adoption boundary reconciled) had no structural wall
left — its verify pass converged 27→7 findings, all model-repairable register rows
(motion-law clauses resolved as `decision` instead of `deferred_owner`) — and still
aborted, exposing a flow defect: a verify session that exhausts its budget raised
through `generate_plan_two_pass`, discarding the on-disk candidate before the
deterministic gate and repair rounds — the machinery built to converge exactly such
candidates — ever ran. Per this module's own doctrine (the loop converges against the
deterministic gate, not a critique pass), verify exhaustion now falls back to the seeded
canonical candidate plus whatever artifact repairs the dying verifier landed, and the
gate re-measures everything; any other terminal cause still propagates. The three
temporal-requirement hints also taught only contract kinds a sparse bundle cannot
publish; each now names the `deferred_owner` resolution path.

Materialization attempts 3–5 each removed one seam: (3) the unit/stage/claim schema is
undiscoverable in-workspace, so a warm write-time loop now feeds the full authoritative
validator back on every write of the target — it repaired one field-precise error per
write but exhausted 24 turns thirteen writes deep, so (4) the kickoff now embeds a
generic minimal-valid document example, collapsing the schema walk to a diff. Attempt 4
then hit a rule this HIR's own reconciliation had introduced: the combined validation
view hardcoded legacy schema 4, making the ready-dependency naming rule fire for a
still-deferred consumer whose immutable row deliberately holds empty outcomes — an error
with no legal fix. The rule now applies only to legacy schema-4 documents and the
combined view keeps the base schema. Attempt 5 passed in-session validation (three warm
round-trips) and then deadlocked at publish on a cross-document inconsistency the
publication gate never checked: `owned_requirements` listing decision-resolved register
rows, which closure demanded be bound and the publish check forbade binding. The first
fix tolerated the shape at consumption ("closed rows are skipped") — and was reverted on
review as exactly the failure mode this HIR exists to prevent: bending a core contract
to accommodate one shot's published artifact generalizes that shot's accident. The
standing resolution: owned means owed. The publication gate now verifies the inverse
ownership direction (an owned entry must be a `deferred_owner` row naming that layer —
replayed against the published workspace it flags exactly the twelve defective rows,
R1–R9/R67–R69, and nothing else), materialization fails closed on such bundles with an
explicit route to republication, both validators judge the same register source, and
scene-contract kind errors enumerate the supported registry. Bundle `a5692e9f` is
therefore defective authority: the remedy is republication through the general path, not
materializing around it.

First live materialization (`20260823T125746Z-9cd0b8`, post-publication) failed closed on
structural validation with every field mismatched: the kickoff named the selected bundle
only by hash, and the content-addressed bundle path under another run's checkpoints is
unguessable, so the session probed six plausible locations, was denied by the path
scope, and reconstructed its layer row from prose. The read grant had existed all along —
only the address was withheld, the same discoverability class as the enum defect. The
kickoff now embeds the exact global layer row verbatim plus the readable bundle root,
ledger, and outcomes paths, and the durable decision ledger joined the strict-read grants
(the validator enforces adoption from that exact file, so the session must be able to
read it).

Run `20260823T110844Z-6281c8` then published the first gate-clean sparse bundle
(`a5692e9f…`, pointer selected, `vfx evals plan` independently CLEAN: 8 deferred layers,
27 axes, 11/11 citations, zero contracts or fingerprints) in ~24.5 minutes and ~$5.74
against the $12.13/54-minute stopped baseline, with draft 24 / verify 12 turn ceilings
supplied per invocation. The 12/6 defaults, the batched-write alternative, and whether
verify should rewrite the plan at all remain open; this HIR's acceptance still requires
first authoritative scene evidence through root-layer materialization and the first
build unit.
