---
id: HIR-0174
title: Sessions receive their own artifacts — journal root, stamped drafts, data-block carriers, teaching rejections
status: accepted
introduced_in: unreleased
date: 2026-09-02
failure_class: session_context_starvation
mechanism: session_owned_evidence_roots_and_typed_closures
adr: null
---

# Sessions receive their own artifacts

The first real-shot run on the `vfx-harness.run/v2` generation
(`artifacts/room_1046_opening`, run `20260902T165518Z-004470`, layers 1–2) closed every
unit, but four harness mechanisms starved model sessions of artifacts the harness already
held, and each starvation was paid in turns, dollars, and a worse design decision. None
was a model failure; each is an owning-boundary defect.

## Observed failure

1. **Unit-plan preview blind to its own draft.** Every JIT unit-planning session
   (`1.aim_target`, `1.camera_path`, `2.exterior_facade`) called `publish_unit_plan`
   successfully, then `gate_preview` reported
   `[hierarchy] plans/units/<unit>.md — ready unit has no just-in-time plan although its
   state is 'planning'` with the remedy `run vfx plan <shot> --layer N --unit <id>`, a
   retired command form. Sessions burned one or two turns, hit `GATE PLATEAU`, and reported
   "not fixable from this plan"; the harness terminal gate then passed the same bytes.
2. **Camera-rig contracts unpassable.** `bvfx_camera_rig(role='camera.rig')` tags the
   pivot Empty `camera.rig` and the camera `camera.rig.camera`. The materialized
   `keyframe_schedule` and `object_property` rows on `data.lens` for role `camera.rig`
   evaluated the Empty too: `cam_rig 'data.lens' unreadable: 'NoneType' object has no
   attribute 'lens'`, `keyframe_schedule=2.0`. The builder retagged the pivot
   (`camera.rig.pivot` still matches the literal selector), then deleted the rig and built
   a single un-parented camera to pass (37 turns, $1.35). A roll-owning unit could not
   have escaped that way.
3. **Journals refused for every unit.** `journal unavailable (journal publication must
   stay under <run>/checkpoints/blender)` for both layer-1 units and again on layer 2;
   `checkpoints/journals/` stayed empty. The finalizer sessions were kicked off without a
   journal path and spent 12–16 discovery calls (`Glob runs/<run>/**`, reading the build
   transcript, a 22 KB materialization document, and an attempt to read harness source
   that the sandbox denied). The `camera_path` finalizer first asked the operator for
   evidence; only the Stop hook pushed it on. The finalize prompt itself names memory
   re-derivation as the drift mechanism.
4. **Policy rejection without teaching.** `probe_candidate` refused
   `obj = bpy.context.active_object` passed to `bvfx_role(obj, …)` with only
   `artifact bpy capability cannot escape a tracked attribute or simple alias`: no line,
   expression, capability, or legal form. The finalizer tried to read
   `src/vfx_harness/blender/artifact_execution.py` to learn the rule.

Two smaller starvations rode along. The first `compare_frame` of every unit on this
1280x640 shot warned `render is only 256px tall — below the 320px measurement height …
Re-render at a higher scale`: the default scale 0.4 was justified on one 1920x960 frame,
so the builder was told to guess a number the harness could derive. And every
executable-only unit ended with `NEVER CALLED: render_pass … the PROMPT is not landing`
although a look-less unit owes no raster, a false signal that points at prompt growth.

On layer 2 the dependency-complete producer (`2.exterior_massing`, which depends on
`2.exterior_facade`) was the payer of the camera-owned deferred rows
`subject-bbox-f38/f113/f175`; the runtime evaluated them in its authoritative set
(`13/14 pass · failing: subject-bbox-f38: bbox_height=0.7921 target 0.35..0.55`), but
its scope card and kickoff listed neither payments nor forecasts, so the builder read its
own failing row as "layer-1's non-owned contract" and moved on.

The same run then ended `failed` (`harness_defect`, 72 minutes) at the first two-round
unit: `2.exterior_massing` carried `massing-isolation-human` (`authority: human_required`,
evidence `human_decision`). Nothing in the runtime produces a human decision for a unit
claim, so the claim could never be paid; `_unit_requires_raster` treated it as raster
work, round 1 rendered a black EEVEE plate, the no-signal guard kept the critic off but
returned `mean 1.0 / pass False`, and the loop spent a $3.22 revision session that
could not light anything. Selecting the best round then called
`session.restore(checkpoints/blender/snapshot_2@exterior_massing_r1.blend)`; the
confinement mounts only the run's `scratch/blender` writable and declared shot inputs
readable, so the worker reported ENOENT for a file the parent had published seconds
earlier. That restore path had never executed under confinement.

A fifth, observability-only gap made the diagnosis slower: build transcripts record the
`LIVE_BUILD` kickoff but not the `FINALIZE_SCRIPT` / `REPAIR_SCRIPT` prompts, so the
degraded finalizer prompt had to be reconstructed from the run log.

## Root cause

1. `publish_unit_plan` wrote plan bytes only. The consumer-view projection
   (`_project_jit_plans`) admits a unit plan only when its bundle-pinned integrity sidecar
   validates, and that sidecar was stamped by `generate.py` after the session. The
   preview therefore evaluated a view that structurally could not contain the session's
   artifact, and the hierarchy finding's remedy still named the retired `--unit` flag.
2. The probe evaluated `data.*` paths on every role-matching host. The helper and the
   evaluator contradicted each other: the harness recommends a two-object rig and then
   judges a data-block property on the object that has no data-block.
3. `BlenderSession.stage_journal` contained destinations against `self.snapshots`
   (`checkpoints/blender`, commit 4d8a683) while `unit_finalize` published to
   `checkpoints/journals` (commit 16f4351). A broad `except Exception: log("journal
   unavailable")` turned the refusal into a log line, so the finalize phase silently
   degraded instead of failing closed.
4. `validate_artifact_source` raised a fixed string with no node position or chain.

## Decision criteria

- The session that produces an artifact must be able to observe it before the harness
  judges it (close the loop; query, don't recall).
- Rejections teach: contract, observed value, legal next action.
- A typed host-class rule beats a heuristic: an Empty can never carry a data-block
  property, so typing it out is a definition, not a tolerance; the closure must still keep
  a carrier so a typed-out host cannot hide a subject.
- Evidence capture is a phase boundary: a missing journal fails the finalize closed rather
  than degrading the prompt.
- No prompt wording, no `--unit` flag revival, no single-camera recommendation.

## General mechanism

1. `_publish_unit_plan_content` stamps the bundle-pinned integrity sidecar
   (`stamp_work_unit_plan`, no gate attestation) in the same publication; the factory
   threads the session's `selected_authority` into the tool. The consumer view now admits
   the draft, so `gate_preview` evaluates the real candidate. The hierarchy finding's
   remedy names the true actions: publish through the active session, or with no live
   session `vfx units retry … --reason … --evidence …` then `vfx build --layer N`. The
   plateau text says a finding on the session's own artifact is fixed by republishing.
2. The probe types the host closure for data-block paths: `_carriers(objects, paths)`
   keeps every host that owns a data-block (or reads an object-level alias), types out
   hosts with no data-block and names them in the note, and `keyframe_schedule` /
   `object_property` fail closed with `matched only hosts without a data-block …` when no
   carrier remains. A data-block that lacks the attribute is still a failing measurement.
   `data_block_carriers` in `evidence/scene_checks/validate.py` mirrors the rule for tests
   and the vocabulary definitions state it.
3. `BlenderSession` owns `journals = checkpoints/journals` and mints the destination
   through `journal_destination(name)`; `stage_journal` contains against that root and
   its refusal names the minter. `unit_finalize` takes the destination from the session
   and no longer catches capture failures: a refused or failed journal is a build failure
   at the finalize boundary (zero accepted calls remains a legal, logged empty journal).
4. `_alias_escape_message` names the line, the source segment, the resolved capability
   chain, the forms the policy tracks, and the legal replacements (`bpy.data.*.new`,
   `bpy.data.objects.get`, `bvfx_*` helper returns).
5. `_run_script_agent` journals its kickoff and continuation prompts
   (`role="kickoff"` / `"continuation"`, `mode=<MODE>_SCRIPT`). The script-agent
   plumbing moved from `prior.py` to `agents/builder/script_agent.py` at the module's
   existing section boundary; the package facade re-exports are unchanged.
6. `measurement_floor_scale(resolution_y)` derives the first comparison scale from the
   shot brief's frame height so the render reaches `_METRIC_H`; the tool factory reads
   the resolution once and `_comparison_mode_scale` uses it only when no round lock and
   no explicit scale exist.
7. `render_pass` applicability follows the unit's typed look feedback: an
   executable-only unit's unused `render_pass` is `not_applicable`, not a prompt defect.
9. `BlenderSession.restore` accepts a parent-published checkpoint (under the session's
   snapshot root) or its staged copy, re-stages the exact published bytes into the
   worker's publication scratch when the staged copy is absent or differs, and hands the
   worker that visible path; the result names the published checkpoint. Any other path
   is refused naming both roots.
10. `human_required` claim authority and `human_decision` evidence are retired from the
   work-unit claim vocabulary with a teaching rejection: the human domain is
   `approved_start` / `planner_start` judgment debt on the owning requirement
   (HIR-0124, HIR-0163). Raster rounds are therefore owed only by look capabilities,
   qualified qualitative claims, image bindings, or registry-declared functional image
   metrics.
8. The unit scope card compiles `deferred_subject_payments` from the same
   `deferred_subject_composition_ids_for_unit` closure the runtime requires, rendered as
   "deferred subject rows this unit pays or protects … REQUIRED BEFORE FREEZE"; the
   forecast section stays diagnostic-only. Kickoff and `unit_scope` share the renderer.

## Rejected patch-level alternatives

- Telling unit planners to ignore the self-referential finding (prompt wording for a
  mechanical defect; the preview would still evaluate the wrong view).
- Reviving `vfx plan --unit` as the remedy (a retired surface; unit plans publish only
  through the build's gate-attested transaction).
- Documenting "use a single camera object" (removes roll authority and contradicts the
  `camera-roll-rig` recipe and `bvfx_camera_rig`).
- Tagging the pivot outside the rig namespace (out of the unit's declared write scope;
  the literal selector still matches every dotted descendant).
- Widening journal containment to the whole run (any parent-owned path would publish
  evidence outside checkpoints; the session must mint the one destination).
- Keeping the broad handler and merely logging louder (detect-and-continue at a phase
  boundary).

## Validation

- `src/tests/unit/test_unit_plan_publication_stamp.py`: publication stamps the sidecar
  pinned to the selected bundle, passes the projection's `require_gate=False` check and
  fails `require_gate=True`; republication restamps; no-authority publication pins to the
  pointer.
- `src/tests/unit/test_keyframe_schedule.py`: `data_block_carriers` types out the Empty
  for `data.lens` and bare `lens`, keeps every host for `location`, keeps a mesh with a
  data-block that lacks the attribute, and the probe source types carriers before
  measuring.
- `src/tests/integration/test_real_blender_property_carriers.py` (real confined Blender):
  a rig with pivot `camera.rig` and child `camera.rig.camera` passes `keyframe_schedule`
  and `object_property` on `data.lens` with the pivot named as typed out; the injected
  failure (camera leaves the role) fails both rows closed naming the pivot.
- `src/tests/unit/test_blender_session_journal_root.py`: the session owns
  `checkpoints/journals`, publishes to the minted destination, refuses the snapshot root
  naming `journal_destination`, and `unit_finalize` takes the destination from the
  session without a broad handler (source ratchet).
- `src/tests/unit/test_artifact_execution_policy.py::test_alias_escape_rejection_names_line_capability_and_legal_forms`.
- `src/tests/unit/test_script_session_kickoff_transcript.py`: prompts are recorded before
  each query.
- `src/tests/unit/test_look_capabilities.py::test_first_comparison_scale_reaches_the_measurement_floor`
  and `src/tests/unit/test_tool_use_telemetry.py`.
- `src/tests/integration/test_real_blender_checkpoint_restore.py` (real confined Blender):
  snapshot, mutate, restore from the published path, the scene is back — the exact crash
  reproduction; `test_blender_session_journal_root.py` covers re-staging, stale-copy
  replacement, and refusals.
- `src/tests/unit/test_claim_vocabulary_retirement.py`: `human_required` and
  `human_decision` are refused with the judgment-debt teaching; the look-less no-critic
  pin in `test_look_capabilities.py` now uses a qualified qualitative claim.
- `src/tests/unit/test_unit_scope.py::test_early_geometry_producer_gets_nonpayable_deferred_bbox_forecast`:
  the partial producer keeps a diagnostic forecast and an empty payment list; the
  dependency-complete producer's card names the row as a required payment.
- Before the mechanism: the three unit-planning sessions of run 004470 each reported the
  self-referential finding; both layer-1 journals were refused; `cam-lens-schedule` read
  2.0 on the rig. The next real run on the same shot re-validates all three at the live
  boundary.

## Release and rollback

Unreleased. No schema changes. Rolling back restores the snapshot-root containment (and
the refused journals), the unstamped drafts, and the untyped closure; there is no data
migration in either direction. Journal files already under `checkpoints/journals/` remain
valid.

## Remaining limitations

- `bvfx_camera_rig` still documents its tagging only in its docstring; the compiled unit
  card's helper inventory carries that docstring, which is where builders read it.
- An explicit `scale` below the measurement floor still measures an upscaled plate; the
  guard's warning remains for that deliberate choice.
- Reconsider the carrier rule if a contract ever needs to assert that an Empty carries
  nothing: that is an `object_count` or role-closure proposition, not a `data.*` property.
