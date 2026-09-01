"""The plan gate — the deterministic bar a plan must clear before anything is built on it.

The plan is the specification every later stage is judged against, and it was the only
artifact nothing checked. `ledger.load_layers` reads `layers.json` for shape; past that, a
plan could claim anything and the first thing to notice would be a layer thrashing against
a target that was never reachable.

The organising finding, measured across both shots and all four plan documents:

    spike citations      PRECISE and TRUE. `spike_05.out` line 9 is cited for "the default
                         is 100" and line 9 reads `volumetric_start/end: ... 100.0`.
    web research         0 URLs. Step 6 of the planner prompt requires "source links in
                         the ticket". Not one plan contains a link.
    ask_supervisor       never fired across two shots.
    [unknown] tags       0, in every document, and research is gated on that tag.

The difference is not diligence. `spike` WRITES A FILE to the lab directory; `WebSearch`
leaves nothing behind unless the model chooses to type a URL. Evidence a tool physically
deposits survives and stays checkable. Evidence the model is merely asked to record does
not. Every check here is therefore a check on an ARTIFACT, never on a claim about one.

The checks, each free and each with a known-bad fixture in the test suite:

    grounded      every stated fingerprint number re-derives from the plate it describes
                  (eval.grounding — 153/160 on the shipped plans; the 7 failures were one
                  broken metric, not an inventive planner)
    citations     every file a plan cites RESOLVES, and a cited line number exists in it.
                  Not fabrication — link rot: `server_to_hansa/plan.md` cites
                  `../barrel_roll/logs/plan_lab_draft/spike_05.out` five times and that
                  path stopped existing when the shot was archived. The claims are true and
                  a build agent reading them today gets nothing.
    evidence      a ticket claiming `✓spiked` must cite a lab file; a ticket claiming
                  research must leave a source link. A confidence tag is a self-assessment
                  and worth exactly the artifact under it.
    done-checks   do the plan's own checks WORK — is each satisfiable by the reference it
                  names, and does it REJECT a known-bad render? Grounding asks whether the
                  numbers are real; this asks whether the checks can fail anything. Both are
                  needed: this plan scored 95/95 on grounding while carrying a check its own
                  plate cannot pass. See checks.py — the contract, re-run by the gate.
    contracts     plans/global.md, the next just-in-time work-unit plan, layers.json,
                  acceptance.json, critic_axes.json and live-scene
                  contracts must agree: an axis a layer OWNS must exist in the rubric,
                  every ref must be real, and a scene fact must belong to a frame/axis its
                  layer actually judges. A layer owning an axis the critic does not score
                  cannot be judged on it, and nothing else in the pipeline notices.

Severity is either `blocking` (a build on this plan is aiming at something that is not
there) or `warn` (the plan is weaker than it claims, but buildable).

Exit codes: 0 clean · 3 at least one blocking finding
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vfx_harness.domain.authority_preview_records import (
    AUTHORITY_PREVIEW_REFERENCE_PATH,
)
from vfx_harness.domain.brief import load_shot
from vfx_harness.domain.work_units import ready_units
from vfx_harness.evaluation.layer_publications import (
    EvaluationLayerPublicationConflict,
    receipt_backed_layer_prefix,
)
from vfx_harness.evaluation.plan_gate.preview_authorization import (
    CandidatePreviewAuthorization,
    candidate_preview_authorization,
    candidate_preview_unit_authorization,
)
from vfx_harness.evaluation.plan_gate.types import (
    Finding,
    _global_executable_checks_apply,
)
from vfx_harness.evaluation.plan_gate.unit_deps import _check_unit_dependencies
from vfx_harness.orchestration.authority_capsule_resolution import (
    selected_layer_capsule_digest,
)
from vfx_harness.orchestration.authority_selection import (
    ResolvedSelectedAuthority,
    SelectedAuthorityResolutionError,
    resolve_selected_authority,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    require_matching_authority_selection_token,
)
from vfx_harness.orchestration.layer_outcome_paths import layer_outcome_locator
from vfx_harness.orchestration.layer_plans import (
    load_amendments,
    validate_work_unit_plan_authority,
    work_unit_plan_path,
)
from vfx_harness.orchestration.ledger import Layer, load_layers
from vfx_harness.orchestration.plan_consumer_view import PlanConsumerViewMarker
from vfx_harness.orchestration.selected_layer_chain import selected_layer_chain
from vfx_harness.orchestration.unit_completion_authorizations import (
    UnitCompletionAuthorization,
)
from vfx_harness.orchestration.unit_completion_state import (
    authorize_completed_units_for_layer,
)
from vfx_harness.orchestration.unit_state import (
    authorized_passed_unit_ids,
    validate_current,
)
from vfx_harness.orchestration.unit_state import load as load_unit_state


@dataclass(frozen=True, slots=True)
class _SelectedPublicationContext:
    """The live shot and exact selected generation behind one gate input folder."""

    shot_folder: Path
    selected_authority: ResolvedSelectedAuthority
    layers: tuple[Layer, ...]
    consumer_marker: PlanConsumerViewMarker | None = None


def _selected_publication_context(folder: Path) -> _SelectedPublicationContext | None:
    """Resolve selected publication authority, including a run-scoped gate snapshot.

    A planner workspace with no selected plan is an explicit offline candidate and has
    no accepted prefix.  A consumer view is different: its marker names the live shot
    and exact base selection whose prior publications the staged candidate consumes.
    """

    marker_path = folder / ".plan-consumer-view.json"
    consumer_marker: PlanConsumerViewMarker | None = None
    if marker_path.is_symlink():
        raise ValueError("plan consumer view marker must be a real regular file")
    if marker_path.exists() and not marker_path.is_file():
        raise ValueError("plan consumer view marker must be a real regular file")
    if marker_path.is_file():
        try:
            marker = PlanConsumerViewMarker.from_bytes(marker_path.read_bytes())
            consumer_marker = marker
            selected = resolve_selected_authority(marker.shot)
            require_matching_authority_selection_token(
                marker.base_selection,
                selected.selection_token,
            )
        except (
            AuthoritySelectionConflict,
            OSError,
            SelectedAuthorityResolutionError,
            ValueError,
        ) as exc:
            raise ValueError(
                f"plan consumer view cannot resolve its exact selected base authority: {exc}"
            ) from exc
        shot_folder = marker.shot
    else:
        try:
            selected = resolve_selected_authority(folder)
        except SelectedAuthorityResolutionError as exc:
            raise ValueError(f"selected plan authority is invalid: {exc}") from exc
        if selected.plan is None:
            return None
        shot_folder = folder

    if selected.plan is None:
        raise ValueError("selected publication context has no selected plan authority")
    shot = load_shot(shot_folder)
    try:
        ordered = selected_layer_chain(
            shot,
            selected_authority=selected,
        )
    except ValueError as exc:
        raise ValueError(f"selected layer publication chain is invalid: {exc}") from exc
    return _SelectedPublicationContext(
        shot_folder=shot_folder,
        selected_authority=selected,
        layers=ordered,
        consumer_marker=consumer_marker,
    )


def _completion_authorization_for_gate(
    folder: Path,
    *,
    publication_context: _SelectedPublicationContext | None,
    candidate_layer: Layer,
    selected_layer: Layer | None,
    state: dict,
) -> UnitCompletionAuthorization | None:
    """Resolve exact source-backed receipt authority for one gate candidate.

    A candidate preview may carry preserved execution receipts across a proposed
    transition, but only its typed transition reference can authorize those receipts.
    Without that reference, the current coordinator can authorize receipts solely for
    an exactly unchanged selected layer.
    """

    if publication_context is None:
        return None
    preview_path = folder / AUTHORITY_PREVIEW_REFERENCE_PATH
    if preview_path.exists() or preview_path.is_symlink():
        return candidate_preview_unit_authorization(
            folder,
            shot_folder=publication_context.shot_folder,
            selected_authority=publication_context.selected_authority,
            layer_id=str(candidate_layer.id),
            state=state,
        )
    if selected_layer is None or candidate_layer != selected_layer:
        return None
    layer_digest = selected_layer_capsule_digest(
        publication_context.shot_folder,
        str(candidate_layer.id),
        publication_context.selected_authority,
    )
    return authorize_completed_units_for_layer(
        publication_context.shot_folder,
        str(candidate_layer.id),
        candidate_layer.stages,
        expected_plan_hash=layer_digest,
        selected_authority=publication_context.selected_authority,
    )


def _check_hierarchical_plans(folder: Path) -> tuple[list[Finding], dict]:
    """Require plans for dependency-ready units and validate the feedback ledger."""

    out: list[Finding] = []
    legacy = folder / "plan.md"
    if legacy.is_file():
        out.append(
            Finding(
                "hierarchy",
                True,
                "plan.md",
                "legacy monolithic plan coexists with strict work-unit plans and can poison builder retrieval",
                "archive it outside the shot folder; strict planning has no compatibility "
                "fallback and builders consume only schema-declared unit plans",
            )
        )
    global_path = folder / "plans" / "global.md"
    if not global_path.is_file():
        return [
            *out,
            Finding(
                "hierarchy",
                True,
                "plans/global.md",
                "strict global plan is missing",
                "plan.md is not supported; run `vfx plan <shot>` to migrate",
            ),
        ], {}
    try:
        load_amendments(folder)
    except (OSError, ValueError) as exc:
        out.append(
            Finding(
                "hierarchy",
                True,
                "plan_amendments.jsonl",
                str(exc),
                "repair the JSONL record; invalid feedback cannot be ignored",
            )
        )
    dependency_findings = _check_unit_dependencies(folder)
    if dependency_findings:
        # ``load_layers`` raises on the first invalid layer. Returning its exception
        # here would force one paid repair round per layer for the same repeated defect.
        # The raw dependency pass is exhaustive, so one repair brief can fix the whole
        # document before typed loading and claim closure resume.
        return [*out, *dependency_findings], {"unit_plans_required": 0, "layers_passed": 0}
    try:
        layers = load_layers(load_shot(folder))
    except Exception as exc:
        return [*out, Finding("hierarchy", True, "layers.json", str(exc))], {}

    passed: set[str] = set()
    ordered_layers = tuple(layers.values())
    selected_by_id: dict[str, Layer] = {}
    preview_authorization: CandidatePreviewAuthorization | None = None
    try:
        publication_context = _selected_publication_context(folder)
    except ValueError as exc:
        out.append(
            Finding(
                "hierarchy",
                True,
                ".plan-consumer-view.json",
                str(exc),
                "refresh the gate snapshot from one current selected authority generation",
            )
        )
        return out, {"unit_plans_required": 0, "layers_passed": 0}
    if publication_context is not None:
        selected_by_id = {layer.id: layer for layer in publication_context.layers}
        ordered_layers = tuple(
            layers[layer.id]
            for layer in publication_context.layers
            if layer.id in layers
        )
        if set(selected_by_id) != set(layers):
            out.append(
                Finding(
                    "hierarchy",
                    True,
                    "layers.json",
                    "candidate layer ids do not match the exact selected global layer DAG",
                    "regenerate the consumer view from the current selected authority",
                )
            )
            return out, {"unit_plans_required": 0, "layers_passed": 0}

        preview_path = folder / AUTHORITY_PREVIEW_REFERENCE_PATH
        preview_exists = preview_path.exists() or preview_path.is_symlink()
        marker = publication_context.consumer_marker
        candidate_view_differs = False
        if marker is not None:
            selected_view = (
                publication_context.selected_authority.assertion.effective_view
            )
            candidate_view_differs = (
                selected_view is None
                or marker.view_source != selected_view.source
                or marker.view_digest != selected_view.digest
            )
        if candidate_view_differs and not preview_exists:
            out.append(
                Finding(
                    "hierarchy",
                    True,
                    AUTHORITY_PREVIEW_REFERENCE_PATH.as_posix(),
                    "unpublished candidate view lacks a typed authority-state "
                    "preview reference",
                    "regenerate the isolated consumer view from the current "
                    "candidate and selected base",
                )
            )
            return out, {"unit_plans_required": 0, "layers_passed": 0}
        if preview_exists:
            try:
                preview_authorization = candidate_preview_authorization(
                    folder,
                    shot_folder=publication_context.shot_folder,
                    selected_authority=publication_context.selected_authority,
                )
            except (OSError, TypeError, ValueError) as exc:
                out.append(
                    Finding(
                        "hierarchy",
                        True,
                        AUTHORITY_PREVIEW_REFERENCE_PATH.as_posix(),
                        f"candidate authority preview is not authorized: {exc}",
                        "regenerate the isolated consumer view from the current "
                        "candidate and selected base",
                    )
                )
                return out, {"unit_plans_required": 0, "layers_passed": 0}
            if preview_authorization is None:  # pragma: no cover - path existed
                raise AssertionError("candidate preview authorization disappeared")

        # A staged materialization may legitimately change the first unpublished layer.
        # Only the exact unchanged leading selected rows are candidates for carried
        # publication; the shared verifier then decides whether each is truly current.
        unchanged_selected: list[Layer] = []
        for candidate in ordered_layers:
            selected_layer = selected_by_id[candidate.id]
            if preview_authorization is not None:
                if (
                    preview_authorization.finalization_receipt(candidate.id)
                    is None
                ):
                    break
            elif candidate != selected_layer:
                break
            unchanged_selected.append(selected_layer)
        try:
            published = receipt_backed_layer_prefix(
                publication_context.shot_folder,
                unchanged_selected,
                publication_context.selected_authority,
            )
        except EvaluationLayerPublicationConflict as exc:
            out.append(
                Finding(
                    "hierarchy",
                    True,
                    layer_outcome_locator(exc.layer_id),
                    "selected prior layer lacks a current receipt-backed "
                    f"publication: {exc}",
                    "reconcile the exact terminal receipt projections before "
                    "planning downstream work",
                )
            )
            return out, {
                "unit_plans_required": 0,
                "layers_passed": exc.published_count,
            }
        passed = {str(layer.id) for layer in published}

    next_layer = next(
        (layer for layer in ordered_layers if str(layer.id) not in passed),
        None,
    )
    required = 0
    if next_layer is not None and not _global_executable_checks_apply(next_layer):
        # Deferred authority has no durable unit state until a pinned JIT overlay
        # materializes its full unit DAG. Requiring state here would invent a fake unit.
        return out, {"unit_plans_required": 0, "layers_passed": len(passed)}
    if next_layer is not None:
        try:
            state = load_unit_state(folder, str(next_layer.id))
            validate_current(state, str(next_layer.id), next_layer.stages)
        except ValueError as exc:
            out.append(
                Finding(
                    "hierarchy",
                    True,
                    f"state/work-units/layer_{next_layer.id}.json",
                    str(exc),
                    "publish a validated authority replacement; stale unit state cannot "
                    "authorize execution",
                    layer=str(next_layer.id),
                )
            )
            return out, {"unit_plans_required": 0, "layers_passed": len(passed)}
        unit_passed = {
            uid for uid, row in (state.get("units") or {}).items() if row.get("status") == "passed"
        }
        completion_authorization: UnitCompletionAuthorization | None = None
        if publication_context is not None and unit_passed:
            try:
                completion_authorization = _completion_authorization_for_gate(
                    folder,
                    publication_context=publication_context,
                    candidate_layer=next_layer,
                    selected_layer=selected_by_id.get(str(next_layer.id)),
                    state=state,
                )
            except (OSError, TypeError, ValueError) as exc:
                out.append(
                    Finding(
                        "hierarchy",
                        True,
                        AUTHORITY_PREVIEW_REFERENCE_PATH.as_posix(),
                        f"candidate authority preview is not authorized: {exc}",
                        "regenerate the isolated consumer view from the current "
                        "candidate and selected base",
                        layer=str(next_layer.id),
                    )
                )
                return out, {
                    "unit_plans_required": 0,
                    "layers_passed": len(passed),
                }

        sealed_producers = authorized_passed_unit_ids(
            state,
            next_layer.stages,
            completion_authorization=completion_authorization,
            allow_candidate=True,
        )
        ready = ready_units(
            next_layer.stages,
            unit_passed,
            sealed_producers=sealed_producers,
        )
        required = len(ready)
        all_units_sealed = sealed_producers == {
            str(unit.id) for unit in next_layer.stages
        }
        if not ready and not all_units_sealed:
            out.append(
                Finding(
                    "hierarchy",
                    True,
                    f"layer {next_layer.id} work-unit DAG",
                    "no work unit is ready although the layer has not passed",
                    "resolve a blocked dependency or publish a validated authority "
                    "replacement",
                    layer=str(next_layer.id),
                )
            )
        for unit in ready:
            path = work_unit_plan_path(folder, unit)
            if not path.is_file():
                # HIR-0016 made the unit plan a BUILD-time artifact: generated, stamped,
                # gated, and attested inside the build flow, with consumers refusing
                # unattested authority. Before that flow runs, absence is the DESIGNED
                # state — blocking here deadlocked the first unit plan of any fresh-id
                # layer on its sibling's equally-designed absence (run 0b6849), while
                # stale unattested files from a superseded generation satisfied the old
                # existence check. Only state/artifact drift blocks: a unit whose state
                # claims progress must have its plan on disk.
                status = str(
                    ((state.get("units") or {}).get(str(unit.id)) or {}).get("status")
                    or "pending"
                )
                out.append(
                    Finding(
                        "hierarchy",
                        status != "pending",
                        str(path.relative_to(folder)),
                        f"ready unit {next_layer.id}.{unit.id} has no just-in-time plan"
                        + ("" if status == "pending" else f" although its state is {status!r}"),
                        "the build flow generates and gate-attests it at kickoff"
                        if status == "pending"
                        else f"run `vfx plan {folder} --layer {next_layer.id} --unit {unit.id}`",
                    )
                )
            else:
                try:
                    # integrity only: the gate is the authority that PRODUCES the gate
                    # attestation, so it cannot require one to exist yet
                    validate_work_unit_plan_authority(folder, path, require_gate=False)
                except ValueError as exc:
                    out.append(Finding(
                        "hierarchy", True, str(path.relative_to(folder)), str(exc),
                        "regenerate the JIT unit plan from the selected global bundle",
                    ))
                    continue
                plan_text = path.read_text(encoding="utf-8", errors="replace").strip()
                if len(plan_text) < 200:
                    out.append(
                        Finding(
                            "hierarchy", True, str(path.relative_to(folder)), "unit plan is too small to be executable"
                        )
                    )
                elif plan_text.count("\n") + 1 > 160:
                    out.append(
                        Finding(
                            "hierarchy",
                            True,
                            str(path.relative_to(folder)),
                            "unit plan exceeds the strict 160-line execution-index limit",
                            "move evidence/history into machine contracts and sealed outcomes; "
                            "keep only scope, controls, tickets, contract ids, and the stop rule",
                        )
                    )
    return out, {"unit_plans_required": required, "layers_passed": len(passed)}
