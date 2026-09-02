"""Pinned materialization of one globally deferred layer."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import vfx_harness.orchestration.jit_materialization.candidate_preview as candidate_preview
import vfx_harness.orchestration.jit_materialization.selection_commit as selection_commit
from vfx_harness.domain.authority_head_records import canonical_json_bytes
from vfx_harness.domain.plan_records import load_judgment_debt_catalog
from vfx_harness.evaluation import plan_gate
from vfx_harness.orchestration import plan_consumer_installed_projection
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    authority_selection_lock,
    durably_ensure_real_directory,
    require_matching_authority_selection_token,
)
from vfx_harness.orchestration.authority_state_transaction import (
    commit_prepared_authority_state_transition_locked,
)
from vfx_harness.orchestration.jit_materialization.errors import (
    MaterializationSelectionConflict,
)
from vfx_harness.orchestration.jit_materialization.overlay_base import (
    OVERLAY_BASE_FILE,
    overlay_base_bytes,
    overlay_store_digest,
    read_overlay_base,
)
from vfx_harness.orchestration.jit_materialization.proposal import (
    ProposedMaterializationView,
    serialized_documents,
    serialized_hashes,
)
from vfx_harness.orchestration.jit_materialization.publication_checks import (
    attested_transition_intent,
    require_attested_publication,
)
from vfx_harness.orchestration.jit_materialization.schema import (
    CURRENT,
    OVERLAY_ARTIFACTS,
    STATE_DIR,
    _document,
    _require_upstream_outcomes,
    _rows,
    _sha256,
    attest_materialization_finalization,
    materialization_base_selection,
    materialization_candidate_lock,
    materialization_candidate_revision,
    materialization_finalization_path,
    read_materialization_finalization,
)
from vfx_harness.orchestration.jit_materialization.transition import (
    PreparedMaterializationPublication,
    prepare_materialization_publication,
    prepare_materialization_publication_locked,
)
from vfx_harness.orchestration.jit_materialization.validate import validate_materialization
from vfx_harness.orchestration.jit_materialization.view_pointer import (
    canonical_view_hash,
)
from vfx_harness.orchestration.jit_materialization.view_store import (
    durably_install_or_flush_view_directory,
)
from vfx_harness.orchestration.ledger import load_layers_from_path
from vfx_harness.orchestration.plan_consumer_view import PlanConsumerViewMarker
from vfx_harness.orchestration.plan_consumer_view_mutation import (
    PlanConsumerViewMutationCapability,
    PlanConsumerViewMutationConflict,
    mutating_plan_consumer_view,
)
from vfx_harness.orchestration.plan_inputs import (
    PlanPublicationError,
    exact_planning_input_identity_digest,
    planning_input_identity_digest,
    require_exact_planning_input_identity,
)

# Kept as a private compatibility alias for callers that already import the publisher
# helper.  The contract itself lives beside the shared pointer parser used by every
# authority consumer.
_canonical_view_hash = canonical_view_hash
_replace_and_postverify_jit_pointer = selection_commit.replace_and_postverify_jit_pointer
_require_selected_jit_semantics = selection_commit.require_selected_jit_semantics
_resolve_authority_from_locked_heads = selection_commit.resolve_authority_from_locked_heads
_selected_authority = selection_commit.selected_authority
_project_candidate_authority_state = (
    candidate_preview.project_candidate_authority_state
)
_stage_candidate_publication_view = (
    candidate_preview.stage_candidate_publication_view
)


def revert_materialization(
    shot_folder: str | Path,
    layer_id: str,
    *,
    select: bool = True,
    selected_authority=None,
) -> Path | None:
    """Compose a view with one layer restored to global deferred authority.

    Re-materialization must design against GLOBAL authority, not against the view it is
    replacing. Without this, the discarded view's register — where this layer's owned
    requirements were already resolved concretely — is the base, and the replacement
    trips the owned-means-owed rule for requirements its predecessor closed. The layer's
    rows revert to the bundle's deferred authority; every other layer's materialization
    is preserved untouched.

    Selected reverts are retired: every JIT head mutation must carry a gated replacement
    and its atomic authority-state transition. ``select=False`` writes only an overlay
    design base; live authority remains unchanged until the replacement publishes.
    """
    shot = Path(shot_folder).resolve()
    if select:
        raise MaterializationSelectionConflict(
            "selected materialization revert is retired; prepare and gate a replacement "
            "with select=False, then publish that candidate atomically"
        )
    selected = (
        _selected_authority(shot)
        if selected_authority is None
        else selected_authority
    )
    if selected.plan is None:
        raise MaterializationSelectionConflict(
            "materialization revert requires selected global plan authority"
        )
    try:
        selected.plan.bundle.root.relative_to(shot)
    except ValueError as exc:
        raise MaterializationSelectionConflict(
            "materialization revert snapshot belongs to another shot"
        ) from exc
    effective = selected.assertion.effective_view
    if effective is None or effective.source != "jit":
        return None
    bundle = selected.plan.bundle
    base_selection = selected.selection_token
    layer_id = str(layer_id)

    def _bundle_doc(name: str) -> dict:
        return _document(bundle.root / name)

    view_docs = {
        name: json.loads(selected.artifact_paths[name].read_text(encoding="utf-8")) for name in OVERLAY_ARTIFACTS
    }
    # acceptance.json is a bare list; only materialization adds rows to it, so this
    # layer's fingerprints leave with its view.
    view_docs["acceptance.json"] = [
        row
        for row in view_docs["acceptance.json"]
        if not isinstance(row, dict) or str(row.get("layer") or "") != layer_id
    ]
    bundle_layers = {str(r.get("id")): r for r in _bundle_doc("layers.json")["layers"]}
    if layer_id not in bundle_layers:
        raise ValueError(f"cannot revert unknown global layer {layer_id!r}")
    view_docs["layers.json"]["layers"] = [
        bundle_layers.get(layer_id, row) if str(row.get("id")) == layer_id else row
        for row in view_docs["layers.json"]["layers"]
    ]
    bundle_requirements = {str(r.get("id")): r for r in _bundle_doc("requirements.json")["requirements"]}
    view_docs["requirements.json"]["requirements"] = [
        bundle_requirements.get(str(row.get("id")), row)
        if str((row.get("resolution") or {}).get("owner_layer") or "") == layer_id
        or str(row.get("id")) in _owned_by(bundle_layers.get(layer_id))
        else row
        for row in view_docs["requirements.json"]["requirements"]
    ]
    removed_definition_digests = {
        str(row.get("definition_digest") or "")
        for row in _rows(
            view_docs["requirements.json"],
            "judgment_debt_definitions",
            "requirements.json",
        )
        if str((row.get("seed") or {}).get("owner_layer") or "") == layer_id
    }
    view_docs["requirements.json"]["judgment_debt_definitions"] = [
        row
        for row in view_docs["requirements.json"]["judgment_debt_definitions"]
        if str((row.get("seed") or {}).get("owner_layer") or "") != layer_id
    ]
    view_docs["requirements.json"]["judgment_debt_activations"] = [
        row
        for row in _rows(
            view_docs["requirements.json"],
            "judgment_debt_activations",
            "requirements.json",
        )
        if str(row.get("payer_layer") or "") != layer_id
        and str(row.get("definition_digest") or "") not in removed_definition_digests
    ]
    for name, key in (("scene_checks.json", "contracts"), ("checks.json", "checks")):
        view_docs[name][key] = [
            row
            for row in view_docs[name][key]
            if str(row.get("owner_layer") or row.get("activates_at") or "") != layer_id
        ]
    view_hash = _canonical_view_hash(view_docs)
    payloads = serialized_documents(view_docs)
    store_digest = overlay_store_digest(
        view_hash=view_hash,
        bundle_hash=bundle.content_hash,
        base_selection=base_selection,
    )
    view = shot / STATE_DIR / "views" / store_digest
    view_members = dict(payloads)
    view_members[OVERLAY_BASE_FILE] = overlay_base_bytes(
        bundle_hash=bundle.content_hash,
        base_selection=base_selection,
    )
    durably_install_or_flush_view_directory(shot, view, view_members)
    load_judgment_debt_catalog(
        view / "requirements.json",
        selected_bundle_digest=bundle.content_hash,
    )
    return view


def _owned_by(layer_row: dict | None) -> set[str]:
    jit = (layer_row or {}).get("jit") or {}
    return {str(rid) for rid in (jit.get("owned_requirements") or [])}


def _composed_documents(
    shot: Path,
    materialization_path: str | Path,
    *,
    overlay_root: str | Path | None = None,
    selected_authority=None,
    resolutions_path: str | Path | None = None,
):
    """Validate a payload and compose the five overlaid consumer documents.

    Shared by publication (which writes the durable view + pointer) and the in-session
    candidate preview (which stages the same documents into a throwaway consumer view)
    — one composition, so the preview cannot diverge from what publication produces.
    Runs 20260825T015307Z and 034337Z each published on a preview that had shown the
    PRE-publication view: a false CLEAN, a retracted generation each."""
    shot = Path(shot).resolve()
    selected = (
        _selected_authority(shot)
        if selected_authority is None
        else selected_authority
    )
    if selected.plan is None:
        raise MaterializationSelectionConflict(
            "materialization composition requires selected global plan authority"
        )
    try:
        selected.plan.bundle.root.relative_to(shot)
    except ValueError as exc:
        raise MaterializationSelectionConflict(
            "materialization composition snapshot belongs to another shot"
        ) from exc
    bundle = selected.plan.bundle
    payload = _document(Path(materialization_path))
    candidate_base = materialization_base_selection(payload)
    try:
        require_matching_authority_selection_token(
            candidate_base,
            selected.selection_token,
        )
    except AuthoritySelectionConflict as exc:
        raise MaterializationSelectionConflict(f"materialization candidate base selection is stale: {exc}") from exc
    if overlay_root is None:
        bases = {name: selected.artifact_paths[name] for name in OVERLAY_ARTIFACTS}
    else:
        overlay_bundle, overlay_selection = read_overlay_base(overlay_root)
        if overlay_bundle != bundle.content_hash:
            raise MaterializationSelectionConflict("materialization overlay is pinned to another global bundle")
        try:
            require_matching_authority_selection_token(candidate_base, overlay_selection)
        except AuthoritySelectionConflict as exc:
            raise MaterializationSelectionConflict(
                f"materialization candidate and overlay name different bases: {exc}"
            ) from exc
        bases = {}
        for name in OVERLAY_ARTIFACTS:
            path = Path(overlay_root) / name
            if path.is_symlink() or not path.is_file():
                raise MaterializationSelectionConflict(f"materialization overlay base is missing real artifact {name}")
            bases[name] = path
    global_layers = load_layers_from_path(bundle.root / "layers.json")
    layer_id = str((payload.get("layer") or {}).get("id") or "")
    if layer_id not in global_layers:
        raise ValueError(f"JIT materialization names unknown layer {layer_id!r}")
    base_layers = bases["layers.json"]
    executable_layers = load_layers_from_path(base_layers)
    _require_upstream_outcomes(
        shot,
        global_layers[layer_id],
        executable_layers,
        selected_authority=selected,
    )
    base_scene = bases["scene_checks.json"]
    base_requirements = bases["requirements.json"]
    materialized = validate_materialization(
        bundle.root,
        materialization_path,
        expected_bundle_hash=bundle.content_hash,
        base_layers_path=base_layers,
        base_scene_checks_path=base_scene,
        resolutions_path=(
            Path(resolutions_path)
            if resolutions_path is not None
            else shot / "state" / "plan-resolutions.jsonl"
        ),
        base_requirements_path=base_requirements,
    )
    return (
        bundle,
        materialized,
        bases,
        selected.selection_token,
    )


def _overlay_documents(materialized, bases: dict) -> dict:
    """Apply one validated materialization to its base documents, in memory."""
    layer_id = str(materialized.layer.id)
    layers_doc = _document(bases["layers.json"])
    layers_doc["layers"] = [
        materialized.layer_row if str(row.get("id")) == layer_id else row
        for row in _rows(layers_doc, "layers", "layers.json")
    ]
    scene_doc = _document(bases["scene_checks.json"])
    scene_doc["contracts"] = [
        *_rows(scene_doc, "contracts", "scene_checks.json"),
        *materialized.scene_contracts,
    ]
    checks_doc = _document(bases["checks.json"])
    checks_doc["checks"] = [
        *_rows(checks_doc, "checks", "checks.json"),
        *materialized.image_contracts,
    ]
    requirements_doc = _document(bases["requirements.json"])
    for row in _rows(requirements_doc, "requirements", "requirements.json"):
        requirement_id = str(row.get("id") or "")
        contract_ids = materialized.requirement_bindings.get(requirement_id)
        decision = materialized.requirement_decisions.get(requirement_id)
        evidence_domains = materialized.requirement_evidence_domains.get(requirement_id)
        domain_bindings = materialized.requirement_domain_bindings.get(requirement_id)
        if not contract_ids and not decision:
            continue
        resolution = row.get("resolution") or {}
        if resolution.get("kind") != "deferred_owner" or str(resolution.get("owner_layer") or "") != layer_id:
            raise ValueError(f"requirement {requirement_id} is not owned by materialized layer {layer_id}")
        if contract_ids:
            row["resolution"] = {
                "kind": "contract",
                "ids": sorted(set(contract_ids)),
                "evidence_domains": list(evidence_domains or ()),
                "domain_bindings": list(domain_bindings or ()),
            }
        else:
            row["resolution"] = {
                "kind": "decision",
                "ids": [],
                "decision": decision["statement"],
                "decision_strength": decision["decision_strength"],
                "evidence_domains": list(evidence_domains or ()),
                "domain_bindings": list(domain_bindings or ()),
            }
    definitions = _rows(
        requirements_doc,
        "judgment_debt_definitions",
        "requirements.json",
    )
    by_debt_id = {str(row.get("debt_id") or ""): row for row in definitions}
    by_definition_digest = {str(row.get("definition_digest") or ""): row for row in definitions}
    for row in materialized.judgment_debt_definitions:
        debt_id = str(row.get("debt_id") or "")
        definition_digest = str(row.get("definition_digest") or "")
        prior = by_debt_id.get(debt_id) or by_definition_digest.get(definition_digest)
        if prior is not None:
            if prior != row:
                raise ValueError(
                    f"judgment debt {debt_id or definition_digest} conflicts with selected immutable authority"
                )
            continue
        definitions.append(row)
        by_debt_id[debt_id] = row
        by_definition_digest[definition_digest] = row
    activations = _rows(
        requirements_doc,
        "judgment_debt_activations",
        "requirements.json",
    )
    by_activated_definition = {str(row.get("definition_digest") or ""): row for row in activations}
    for row in materialized.judgment_debt_activations:
        definition_digest = str(row.get("definition_digest") or "")
        prior = by_activated_definition.get(definition_digest)
        if prior is not None:
            if prior != row:
                raise ValueError(
                    "judgment debt definition has conflicting exact payer activations: " + definition_digest
                )
            continue
        activations.append(row)
        by_activated_definition[definition_digest] = row
    acceptance_doc = json.loads(Path(bases["acceptance.json"]).read_text(encoding="utf-8"))
    if not isinstance(acceptance_doc, list):
        raise ValueError("acceptance.json must contain a list")
    acceptance_ids = {str(row.get("id")) for row in acceptance_doc if isinstance(row, dict)}
    duplicate_acceptance = sorted(
        str(row.get("id")) for row in materialized.acceptance if str(row.get("id")) in acceptance_ids
    )
    if duplicate_acceptance:
        raise ValueError("materialized acceptance ids already exist: " + ", ".join(duplicate_acceptance))
    acceptance_doc.extend(materialized.acceptance)
    return {
        "layers.json": layers_doc,
        "scene_checks.json": scene_doc,
        "checks.json": checks_doc,
        "requirements.json": requirements_doc,
        "acceptance.json": acceptance_doc,
    }


def stage_candidate_view(
    shot_folder: str | Path,
    materialization_path: str | Path,
    view: str | Path,
    *,
    overlay_root: str | Path | None = None,
    selected_authority=None,
) -> ProposedMaterializationView:
    """Overlay an UNPUBLISHED materialization candidate onto a prepared consumer view.

    The view then looks exactly as it would after publication — overlaid documents plus
    a synthetic hash-pinned pointer — so the deterministic gate previews the candidate's
    real consequences instead of the pre-publication world."""
    shot = Path(shot_folder).resolve()
    view = Path(view).resolve()
    selected = (
        _selected_authority(shot)
        if selected_authority is None
        else selected_authority
    )
    marker_path = view / ".plan-consumer-view.json"
    if marker_path.is_symlink() or not marker_path.is_file():
        raise MaterializationSelectionConflict(
            "candidate staging requires a real plan-consumer-view v3 marker"
        )
    try:
        marker = PlanConsumerViewMarker.from_bytes(marker_path.read_bytes())
        require_exact_planning_input_identity(
            view,
            expected_authored_inputs=marker.authored_inputs,
            expected_decision_inputs=marker.decision_inputs,
            where="materialization consumer snapshot",
        )
    except ValueError as exc:
        raise MaterializationSelectionConflict(str(exc)) from exc
    bundle, materialized, bases, base_selection = _composed_documents(
        shot,
        materialization_path,
        overlay_root=overlay_root,
        selected_authority=selected,
        resolutions_path=view / "state" / "plan-resolutions.jsonl",
    )
    if (
        marker.shot != shot
        or marker.bundle != bundle.root
        or marker.content_hash != bundle.content_hash
        or marker.base_selection != base_selection
    ):
        raise MaterializationSelectionConflict(
            "plan consumer view marker does not name the candidate's exact base selection"
        )
    documents = _overlay_documents(materialized, bases)
    payloads = serialized_documents(documents)
    artifact_hashes = serialized_hashes(payloads)
    view_hash = canonical_view_hash(documents)

    def stage_under_mutation(
        mutation_capability: PlanConsumerViewMutationCapability,
    ) -> tuple[bytes, bytes, PreparedMaterializationPublication]:
        for name, payload in payloads.items():
            # The installed view links each overlay member to the selected bundle
            # member; the candidate document replaces that verified link with a
            # real file rather than writing through it.
            if (
                plan_consumer_installed_projection.member_kind(
                    mutation_capability,
                    name,
                )
                == "symlink"
            ):
                plan_consumer_installed_projection.remove_symlink(
                    mutation_capability,
                    name,
                )
            plan_consumer_installed_projection.replace_regular_file(
                mutation_capability,
                name,
                payload,
            )
        staged_marker = PlanConsumerViewMarker(
            shot=marker.shot,
            bundle=marker.bundle,
            content_hash=marker.content_hash,
            base_selection=marker.base_selection,
            view_source="jit",
            view_digest=view_hash,
            artifact_hashes=artifact_hashes,
            authored_inputs=marker.authored_inputs,
            decision_inputs=marker.decision_inputs,
        )
        marker_payload = canonical_json_bytes(staged_marker.to_dict())
        plan_consumer_installed_projection.replace_regular_file(
            mutation_capability,
            ".plan-consumer-view.json",
            marker_payload,
        )
        # the view's state/ is a symlink to the shot's; replace it with a copy whose
        # jit-layers pointer pins the candidate documents just written
        state_kind = plan_consumer_installed_projection.member_kind(
            mutation_capability,
            "state",
        )
        if state_kind == "symlink":
            state_target = plan_consumer_installed_projection.read_symlink(
                mutation_capability,
                "state",
            )
            real_state = Path(state_target)
            if not real_state.is_absolute():
                real_state = view / real_state
            real_state = real_state.resolve()
            plan_consumer_installed_projection.remove_symlink(
                mutation_capability,
                "state",
            )
            plan_consumer_installed_projection.ensure_directory(
                mutation_capability,
                "state",
            )
            if real_state.is_dir():
                for child in real_state.iterdir():
                    if child.name != "jit-layers":
                        plan_consumer_installed_projection.create_symlink(
                            mutation_capability,
                            f"state/{child.name}",
                            child,
                        )
        elif state_kind is None:
            plan_consumer_installed_projection.ensure_directory(
                mutation_capability,
                "state",
            )
        elif state_kind != "directory":
            raise PlanConsumerViewMutationConflict(
                "candidate plan-consumer state must be absent, a real directory, "
                "or its registered predecessor symlink"
            )
        plan_consumer_installed_projection.ensure_directory(
            mutation_capability,
            "state/jit-layers",
        )
        candidate_payload = Path(materialization_path).read_bytes()
        publication = prepare_materialization_publication(
            shot,
            selected_before=selected,
            documents=documents,
            bundle_hash=bundle.content_hash,
            view_hash=view_hash,
            artifact_hashes=artifact_hashes,
            candidate_payload=candidate_payload,
            candidate_digest=hashlib.sha256(candidate_payload).hexdigest(),
        )
        _stage_candidate_publication_view(
            mutation_capability,
            view_hash,
            payloads,
        )
        pointer_payload = publication.pointer_payload
        plan_consumer_installed_projection.replace_regular_file(
            mutation_capability,
            "state/jit-layers/current.json",
            pointer_payload,
        )
        _project_candidate_authority_state(mutation_capability, publication)
        return marker_payload, pointer_payload, publication

    try:
        with mutating_plan_consumer_view(
            shot,
            view,
            marker,
        ) as mutation_capability:
            marker_payload, pointer_payload, publication = stage_under_mutation(
                mutation_capability
            )
    except PlanConsumerViewMutationConflict as exc:
        raise MaterializationSelectionConflict(str(exc)) from exc
    return ProposedMaterializationView(
        bundle=bundle,
        materialized=materialized,
        documents=documents,
        base_selection=base_selection,
        view_hash=view_hash,
        artifact_hashes=artifact_hashes,
        authored_inputs=marker.authored_inputs,
        decision_inputs=marker.decision_inputs,
        planning_inputs_digest=planning_input_identity_digest(
            marker.authored_inputs,
            marker.decision_inputs,
        ),
        consumer_marker_sha256=hashlib.sha256(marker_payload).hexdigest(),
        consumer_pointer_sha256=hashlib.sha256(pointer_payload).hexdigest(),
        publication=publication,
    )


def finalize_materialization_candidate(
    shot_folder: str | Path,
    materialization_path: str | Path,
    consumer_view: str | Path,
    *,
    overlay_root: str | Path | None = None,
    selected_authority=None,
):
    """Run the terminal gate and attest only its exact clean candidate revision.

    Local materialization validation, post-publication authority staging, projected
    unit-state reconciliation, the deterministic plan gate, and final attestation are
    one operation. Callers cannot observe CLEAN and then forget to attest the same
    bytes after a patch invalidated an earlier attestation.
    """

    shot = Path(shot_folder).resolve()
    candidate = Path(materialization_path)
    with materialization_candidate_lock(candidate):
        prior_finalization = materialization_finalization_path(candidate)
        if prior_finalization.is_symlink() or (prior_finalization.exists() and not prior_finalization.is_file()):
            raise MaterializationSelectionConflict("materialization finalization must be absent or a real regular file")
        if prior_finalization.is_file():
            prior_finalization.unlink()
        candidate_revision = materialization_candidate_revision(candidate)
        preview = stage_candidate_view(
            shot,
            candidate,
            consumer_view,
            overlay_root=overlay_root,
            selected_authority=selected_authority,
        )
        result = plan_gate.run(Path(consumer_view), require_scene_checks=False)
        if result.clean:
            if materialization_candidate_revision(candidate) != candidate_revision:
                raise MaterializationSelectionConflict("materialization candidate changed while its terminal gate ran")
            observed_hashes = {name: _sha256(Path(consumer_view) / name) for name in OVERLAY_ARTIFACTS}
            if observed_hashes != preview.artifact_hashes:
                raise MaterializationSelectionConflict(
                    "materialization candidate view changed while its terminal gate ran"
                )
            if (
                _sha256(Path(consumer_view) / ".plan-consumer-view.json") != preview.consumer_marker_sha256
                or _sha256(Path(consumer_view) / CURRENT) != preview.consumer_pointer_sha256
            ):
                raise MaterializationSelectionConflict(
                    "materialization consumer authority changed while its terminal gate ran"
                )
            try:
                require_exact_planning_input_identity(
                    Path(consumer_view),
                    expected_authored_inputs=preview.authored_inputs,
                    expected_decision_inputs=preview.decision_inputs,
                    where="materialization gated consumer snapshot",
                )
                with authority_selection_lock(shot, exclusive=False):
                    from vfx_harness.orchestration.authority_selection_heads import (  # noqa: PLC0415
                        read_authority_selection_heads,
                    )

                    heads = read_authority_selection_heads(shot)
                    require_matching_authority_selection_token(
                        preview.base_selection,
                        heads.token,
                    )
                    require_exact_planning_input_identity(
                        shot,
                        expected_authored_inputs=preview.authored_inputs,
                        expected_decision_inputs=preview.decision_inputs,
                        where="materialization finalization",
                    )
                    attest_materialization_finalization(
                        candidate,
                        bundle_hash=preview.bundle.content_hash,
                        base_selection=preview.base_selection,
                        proposed_view_hash=preview.view_hash,
                        proposed_artifact_hashes=preview.artifact_hashes,
                        planning_inputs_digest=preview.planning_inputs_digest,
                        consumer_marker_sha256=preview.consumer_marker_sha256,
                        publication_jit_pointer_sha256=(
                            preview.publication.pointer_sha256
                        ),
                        authority_transition_kind=(
                            preview.publication.transition_kind
                        ),
                        authority_transition_intent_ref=(
                            None
                            if preview.publication.transition is None
                            else preview.publication.transition.intent_ref
                        ),
                        authority_capsule_set_digest=(
                            preview.publication.capsule_set.capsule_set_digest
                        ),
                        authority_effects_digest=(
                            preview.publication.effects_digest
                        ),
                        authority_state_head_ref=(
                            preview.publication.authority_state_head_ref
                        ),
                        before_state_hashes=(
                            preview.publication.before_state_hashes
                        ),
                        after_state_hashes=(
                            preview.publication.after_state_hashes
                        ),
                    )
            except (PlanPublicationError, ValueError) as exc:
                raise MaterializationSelectionConflict(
                    f"materialization selection changed while its terminal gate ran: {exc}"
                ) from exc
        return result


def materialization_finalization_current(
    shot_folder: str | Path,
    materialization_path: str | Path,
    *,
    bundle_hash: str,
) -> bool:
    """Whether a strict finalization still names the current candidate and selection."""

    try:
        candidate = Path(materialization_path)
        finalization = read_materialization_finalization(candidate)
        if (
            finalization.bundle_hash != bundle_hash
            or finalization.candidate_revision != materialization_candidate_revision(candidate)
            or materialization_base_selection(_document(candidate)) != finalization.base_selection
        ):
            return False
        selected = _selected_authority(Path(shot_folder).resolve())
        return (
            selected.selection_token == finalization.base_selection
            and exact_planning_input_identity_digest(Path(shot_folder).resolve())
            == finalization.planning_inputs_digest
        )
    except (OSError, PlanPublicationError, ValueError):
        return False


def publish_materialization(
    shot_folder: str | Path,
    materialization_path: str | Path,
    *,
    overlay_root: str | Path | None = None,
) -> Path:
    """CAS-select exactly the cumulative materialized view that passed the gate."""
    shot = Path(shot_folder).resolve()
    candidate = Path(materialization_path)
    with materialization_candidate_lock(candidate):
        finalization = read_materialization_finalization(candidate)
        attested_intent = attested_transition_intent(shot, finalization)
        if finalization.candidate_revision != materialization_candidate_revision(candidate):
            raise MaterializationSelectionConflict("materialization candidate changed after terminal finalization")
        try:
            planning_inputs_digest = exact_planning_input_identity_digest(shot)
        except (OSError, PlanPublicationError, ValueError) as exc:
            raise MaterializationSelectionConflict(
                f"materialization planning inputs are unavailable: {exc}"
            ) from exc
        if planning_inputs_digest != finalization.planning_inputs_digest:
            raise MaterializationSelectionConflict(
                "materialization planning inputs changed after terminal finalization"
            )
        bundle, materialized, bases, base_selection = _composed_documents(
            shot,
            candidate,
            overlay_root=overlay_root,
        )
        if finalization.bundle_hash != bundle.content_hash or finalization.base_selection != base_selection:
            raise MaterializationSelectionConflict(
                "materialization finalization names another bundle or base selection"
            )
        documents = _overlay_documents(materialized, bases)
        view_hash = _canonical_view_hash(documents)
        payloads = serialized_documents(documents)
        artifact_hashes = serialized_hashes(payloads)
        if view_hash != finalization.proposed_view_hash or artifact_hashes != finalization.proposed_artifact_hashes:
            raise MaterializationSelectionConflict(
                "materialization finalization does not match the proposed publication view"
            )

        view = shot / STATE_DIR / "views" / view_hash
        durably_install_or_flush_view_directory(shot, view, payloads)
        load_judgment_debt_catalog(
            view / "requirements.json",
            selected_bundle_digest=bundle.content_hash,
        )

        pointer_path = shot / CURRENT
        try:
            with authority_selection_lock(shot, exclusive=True):
                from vfx_harness.orchestration.authority_selection_heads import (  # noqa: PLC0415
                    read_authority_selection_heads,
                )

                heads = read_authority_selection_heads(shot)
                require_matching_authority_selection_token(
                    finalization.base_selection,
                    heads.token,
                )
                live = _resolve_authority_from_locked_heads(shot, heads)
                if (
                    live.plan is None
                    or live.plan.bundle.content_hash != bundle.content_hash
                ):
                    raise ValueError(
                        "materialization publication base no longer resolves to its selected global bundle"
                    )
                if (
                    exact_planning_input_identity_digest(shot)
                    != finalization.planning_inputs_digest
                ):
                    raise ValueError(
                        "materialization planning inputs changed before JIT selection"
                    )

                durably_ensure_real_directory(shot, pointer_path.parent)
                candidate_payload = candidate.read_bytes()
                publication = prepare_materialization_publication_locked(
                    shot,
                    heads=heads,
                    selected_before=live,
                    documents=documents,
                    bundle_hash=bundle.content_hash,
                    view_hash=view_hash,
                    artifact_hashes=artifact_hashes,
                    candidate_payload=candidate_payload,
                    candidate_digest=hashlib.sha256(candidate_payload).hexdigest(),
                    prepared_at=(
                        None if attested_intent is None else attested_intent.prepared_at
                    ),
                )
                require_attested_publication(publication, finalization)
                if publication.transition is not None:
                    commit_prepared_authority_state_transition_locked(
                        shot,
                        publication.transition,
                    )
                verified_heads = read_authority_selection_heads(shot)
                if verified_heads.jit != publication.pointer:
                    raise ValueError(
                        "materialization publication did not select the attested JIT pointer"
                    )
                _require_selected_jit_semantics(
                    shot,
                    verified_heads,
                    publication.pointer,
                )
                if (
                    exact_planning_input_identity_digest(shot)
                    != finalization.planning_inputs_digest
                ):
                    raise ValueError(
                        "materialization planning inputs changed during JIT selection"
                    )
        except MaterializationSelectionConflict:
            raise
        except (PlanPublicationError, ValueError) as exc:
            raise MaterializationSelectionConflict(f"materialization publication selection conflict: {exc}") from exc
        return pointer_path
