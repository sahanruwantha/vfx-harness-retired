"""Pinned materialization of one globally deferred layer."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from vfx_harness.evaluation import plan_gate
from vfx_harness.observability.provenance import atomic_write
from vfx_harness.orchestration.jit_materialization.schema import (
    CURRENT,
    OVERLAY_ARTIFACTS,
    STATE_DIR,
    VIEW_SCHEMA,
    MaterializedLayer,
    _document,
    _require_upstream_outcomes,
    _rows,
    _sha256,
    attest_materialization_finalization,
)
from vfx_harness.orchestration.jit_materialization.validate import validate_materialization
from vfx_harness.orchestration.ledger import load_layers_from_path
from vfx_harness.orchestration.unit_state import apply_replan, load


def selected_view_artifact(
    shot_folder: str | Path,
    name: str,
    bundle_hash: str,
    *,
    overlay_root: str | Path | None = None,
) -> Path | None:
    """Return a materialized consumer artifact.

    ``overlay_root`` is an unpublished view directory used as a remat design base.
    It is never the live pointer: a crash must not leave the shot on a hole the
    replacement never published.
    """
    if name not in OVERLAY_ARTIFACTS:
        return None
    if overlay_root is not None:
        path = Path(overlay_root) / name
        if not path.is_file():
            raise ValueError(f"overlay base is missing {name}")
        return path
    shot = Path(shot_folder)
    pointer = shot / CURRENT
    if not pointer.is_file():
        return None
    value = _document(pointer)
    if value.get("schema") != VIEW_SCHEMA:
        raise ValueError("selected JIT layer view is malformed")
    if value.get("bundle_hash") != bundle_hash:
        # A view pinned to another generation is superseded state, not authority for the
        # currently selected bundle — serve the bundle's own deferred artifact and let
        # the next materialization write this generation's view. Republication (run
        # 20260824T150358Z-3bc39c) used to leave every consumer — including the replan
        # transaction meant to reconcile the change — failing on the prior view.
        return None
    relative = (value.get("artifacts") or {}).get(name)
    expected = (value.get("hashes") or {}).get(name)
    if not isinstance(relative, str) or not isinstance(expected, str):
        raise ValueError(f"selected JIT layer view is missing {name}")
    path = shot / relative
    if not path.is_file() or _sha256(path) != expected:
        raise ValueError(f"selected JIT layer view artifact {name} is stale")
    return path


def _consumer_base_artifact(
    shot_folder: str | Path,
    name: str,
    bundle,
    *,
    overlay_root: str | Path | None = None,
) -> Path:
    """Resolve one materialization base from exactly the selected generation.

    A live JIT pointer may still name the previous plan generation immediately after
    global republication.  That view is superseded state, so the verified sparse bundle
    is the base until this generation publishes a materialized replacement.  Remat and
    ordinary materialization must share this rule; neither may turn the intentionally
    absent superseded-view result into ``Path(None)``.
    """
    selected = selected_view_artifact(
        shot_folder,
        name,
        bundle.content_hash,
        overlay_root=overlay_root,
    )
    return selected if selected is not None else bundle.root / name


def revert_materialization(
    shot_folder: str | Path, layer_id: str, *, select: bool = True
) -> Path | None:
    """Compose a view with one layer restored to global deferred authority.

    Re-materialization must design against GLOBAL authority, not against the view it is
    replacing. Without this, the discarded view's register — where this layer's owned
    requirements were already resolved concretely — is the base, and the replacement
    trips the owned-means-owed rule for requirements its predecessor closed. The layer's
    rows revert to the bundle's deferred authority; every other layer's materialization
    is preserved untouched.

    ``select=True`` (tests, explicit discard) writes the live pointer.
    ``select=False`` writes the overlay directory only: remat uses it as the design
    base and selects the replacement, or the previous pointer stays in force.
    """
    # plan_authority resolves selected materialized views through this package.
    from vfx_harness.orchestration.plan_authority import resolve_current  # noqa: PLC0415

    shot = Path(shot_folder).resolve()
    pointer_path = shot / CURRENT
    if not pointer_path.is_file():
        return None
    bundle = resolve_current(shot)
    layer_id = str(layer_id)

    def _bundle_doc(name: str) -> dict:
        return _document(bundle.root / name)

    view_docs = {
        name: json.loads(
            _consumer_base_artifact(shot, name, bundle).read_text(encoding="utf-8")
        )
        for name in OVERLAY_ARTIFACTS
    }
    # acceptance.json is a bare list; only materialization adds rows to it, so this
    # layer's fingerprints leave with its view.
    view_docs["acceptance.json"] = [
        row
        for row in view_docs["acceptance.json"]
        if not isinstance(row, dict) or str(row.get("layer") or "") != layer_id
    ]
    bundle_layers = {str(r.get("id")): r for r in _bundle_doc("layers.json")["layers"]}
    view_docs["layers.json"]["layers"] = [
        bundle_layers.get(layer_id, row) if str(row.get("id")) == layer_id else row
        for row in view_docs["layers.json"]["layers"]
    ]
    bundle_requirements = {
        str(r.get("id")): r for r in _bundle_doc("requirements.json")["requirements"]
    }
    view_docs["requirements.json"]["requirements"] = [
        bundle_requirements.get(str(row.get("id")), row)
        if str((row.get("resolution") or {}).get("owner_layer") or "") == layer_id
        or str(row.get("id")) in _owned_by(bundle_layers.get(layer_id))
        else row
        for row in view_docs["requirements.json"]["requirements"]
    ]
    for name, key in (("scene_checks.json", "contracts"), ("checks.json", "checks")):
        view_docs[name][key] = [
            row
            for row in view_docs[name][key]
            if str(row.get("owner_layer") or row.get("activates_at") or "") != layer_id
        ]
    still_materialized = sorted(
        str(row.get("id"))
        for row in view_docs["layers.json"]["layers"]
        if row.get("execution") != "jit_deferred"
    )
    view_hash = hashlib.sha256(
        json.dumps(view_docs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    view = shot / STATE_DIR / "views" / view_hash
    view.mkdir(parents=True, exist_ok=True)
    for name in OVERLAY_ARTIFACTS:
        atomic_write(view / name, json.dumps(view_docs[name], indent=2, sort_keys=True) + "\n")
    if not select:
        return view
    if not still_materialized:
        pointer_path.unlink()
        return None
    atomic_write(
        pointer_path,
        json.dumps(
            {
                "schema": VIEW_SCHEMA,
                "bundle_hash": bundle.content_hash,
                "view_hash": view_hash,
                "materialized_layers": still_materialized,
                "artifacts": {
                    name: (view / name).relative_to(shot).as_posix()
                    for name in OVERLAY_ARTIFACTS
                },
                "hashes": {name: _sha256(view / name) for name in OVERLAY_ARTIFACTS},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return pointer_path


def _owned_by(layer_row: dict | None) -> set[str]:
    jit = (layer_row or {}).get("jit") or {}
    return {str(rid) for rid in (jit.get("owned_requirements") or [])}


def _composed_documents(
    shot: Path, materialization_path: str | Path, *, overlay_root: str | Path | None = None
):
    """Validate a payload and compose the five overlaid consumer documents.

    Shared by publication (which writes the durable view + pointer) and the in-session
    candidate preview (which stages the same documents into a throwaway consumer view)
    — one composition, so the preview cannot diverge from what publication produces.
    Runs 20260825T015307Z and 034337Z each published on a preview that had shown the
    PRE-publication view: a false CLEAN, a retracted generation each."""
    # plan_authority resolves selected materialized views through this package.
    from vfx_harness.orchestration.plan_authority import resolve_current  # noqa: PLC0415

    shot = Path(shot).resolve()
    bundle = resolve_current(shot)
    global_layers = load_layers_from_path(bundle.root / "layers.json")
    payload = _document(Path(materialization_path))
    layer_id = str((payload.get("layer") or {}).get("id") or "")
    if layer_id not in global_layers:
        raise ValueError(f"JIT materialization names unknown layer {layer_id!r}")
    _require_upstream_outcomes(shot, global_layers[layer_id])
    base_layers = _consumer_base_artifact(
        shot, "layers.json", bundle, overlay_root=overlay_root
    )
    base_scene = _consumer_base_artifact(
        shot, "scene_checks.json", bundle, overlay_root=overlay_root
    )
    base_checks = _consumer_base_artifact(
        shot, "checks.json", bundle, overlay_root=overlay_root
    )
    base_requirements = _consumer_base_artifact(
        shot, "requirements.json", bundle, overlay_root=overlay_root
    )
    base_acceptance = _consumer_base_artifact(
        shot, "acceptance.json", bundle, overlay_root=overlay_root
    )
    materialized = validate_materialization(
        bundle.root,
        materialization_path,
        expected_bundle_hash=bundle.content_hash,
        base_layers_path=base_layers,
        base_scene_checks_path=base_scene,
        resolutions_path=shot / "state" / "plan-resolutions.jsonl",
        base_requirements_path=base_requirements,
    )
    return (
        bundle,
        materialized,
        {
            "layers.json": base_layers,
            "scene_checks.json": base_scene,
            "checks.json": base_checks,
            "requirements.json": base_requirements,
            "acceptance.json": base_acceptance,
        },
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
        if (
            resolution.get("kind") != "deferred_owner"
            or str(resolution.get("owner_layer") or "") != layer_id
        ):
            raise ValueError(
                f"requirement {requirement_id} is not owned by materialized layer {layer_id}"
            )
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
) -> None:
    """Overlay an UNPUBLISHED materialization candidate onto a prepared consumer view.

    The view then looks exactly as it would after publication — overlaid documents plus
    a synthetic hash-pinned pointer — so the deterministic gate previews the candidate's
    real consequences instead of the pre-publication world."""
    shot = Path(shot_folder).resolve()
    view = Path(view).resolve()
    bundle, materialized, bases = _composed_documents(
        shot, materialization_path, overlay_root=overlay_root
    )
    documents = _overlay_documents(materialized, bases)
    for name, document in documents.items():
        target = view / name
        if target.is_symlink() or target.exists():
            target.unlink()
        target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # the view's state/ is a symlink to the shot's; replace it with a copy whose
    # jit-layers pointer pins the candidate documents just written
    state_link = view / "state"
    if state_link.is_symlink():
        real_state = state_link.resolve()
        state_link.unlink()
        state_link.mkdir()
        if real_state.is_dir():
            for child in real_state.iterdir():
                if child.name != "jit-layers":
                    (state_link / child.name).symlink_to(child)
    pointer_dir = view / "state" / "jit-layers"
    pointer_dir.mkdir(parents=True, exist_ok=True)
    pointer = {
        "schema": VIEW_SCHEMA,
        "bundle_hash": bundle.content_hash,
        "view_hash": "candidate-preview",
        "materialized_layers": sorted(
            str(row.get("id"))
            for row in documents["layers.json"]["layers"]
            if row.get("execution") != "jit_deferred"
        ),
        "artifacts": {name: name for name in OVERLAY_ARTIFACTS},
        "hashes": {name: _sha256(view / name) for name in OVERLAY_ARTIFACTS},
    }
    (pointer_dir / "current.json").write_text(
        json.dumps(pointer, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _project_candidate_unit_state(shot, view, materialized)


def _project_candidate_unit_state(
    shot: Path,
    view: Path,
    materialized: MaterializedLayer,
) -> None:
    """Apply the exact state-backed replan to preview-local durable state.

    A replacement candidate necessarily names a different unit DAG before publication,
    while the selected shot state must continue to name the accepted predecessor until
    publication succeeds.  The terminal gate reads both surfaces.  Projecting through
    ``apply_replan`` inside the isolated consumer view lets it validate the same digest
    schema, preservation set, invalidation closure, and resulting unit identities that
    the outer transaction will apply, without mutating selected authority.
    """

    layer_id = str(materialized.layer.id)
    state = load(shot, layer_id)
    if not state:
        return
    old_plan_hash = str(state.get("plan_hash") or "")
    if not old_plan_hash:
        raise ValueError(
            f"layer {layer_id} candidate preview cannot project unit state without "
            "the durable predecessor plan_hash"
        )

    source_dir = shot / "state" / "work-units"
    preview_dir = view / "state" / "work-units"
    if preview_dir.is_symlink():
        preview_dir.unlink()
        preview_dir.mkdir(parents=True)
        for child in source_dir.iterdir():
            (preview_dir / child.name).symlink_to(child)
    else:
        preview_dir.mkdir(parents=True, exist_ok=True)
    source = source_dir / f"layer_{layer_id}.json"
    target = preview_dir / source.name
    if target.is_symlink() or target.exists():
        target.unlink()
    atomic_write(target, source.read_text(encoding="utf-8"))

    apply_replan(
        view,
        layer_id,
        (),
        materialized.layer.stages,
        old_plan_hash=old_plan_hash,
        new_plan_hash=_sha256(view / "layers.json"),
        owner="vfx-harness.candidate-preview",
        trigger="project unpublished materialization through the transactional replan",
        evidence=["scratch/candidate-materialization"],
        state_backed_base=True,
    )


def finalize_materialization_candidate(
    shot_folder: str | Path,
    materialization_path: str | Path,
    consumer_view: str | Path,
    *,
    overlay_root: str | Path | None = None,
):
    """Run the terminal gate and attest only its exact clean candidate revision.

    Local materialization validation, post-publication authority staging, projected
    unit-state reconciliation, the deterministic plan gate, and final attestation are
    one operation. Callers cannot observe CLEAN and then forget to attest the same
    bytes after a patch invalidated an earlier attestation.
    """

    shot = Path(shot_folder).resolve()
    candidate = Path(materialization_path)
    stage_candidate_view(
        shot,
        candidate,
        consumer_view,
        overlay_root=overlay_root,
    )
    result = plan_gate.run(Path(consumer_view), require_scene_checks=False)
    if result.clean:
        bundle, _materialized, _bases = _composed_documents(
            shot,
            candidate,
            overlay_root=overlay_root,
        )
        attest_materialization_finalization(
            candidate,
            bundle_hash=bundle.content_hash,
        )
    return result


def publish_materialization(
    shot_folder: str | Path,
    materialization_path: str | Path,
    *,
    overlay_root: str | Path | None = None,
) -> Path:
    """Validate and atomically select one cumulative materialized consumer view."""
    shot = Path(shot_folder).resolve()
    bundle, materialized, bases = _composed_documents(
        shot, materialization_path, overlay_root=overlay_root
    )
    base_layers = bases["layers.json"]
    base_scene = bases["scene_checks.json"]
    base_checks = bases["checks.json"]
    base_requirements = bases["requirements.json"]
    base_acceptance = bases["acceptance.json"]

    documents = _overlay_documents(materialized, bases)
    layers_doc = documents["layers.json"]
    scene_doc = documents["scene_checks.json"]
    checks_doc = documents["checks.json"]
    requirements_doc = documents["requirements.json"]
    acceptance_doc = documents["acceptance.json"]
    digest_payload = json.dumps(
        {
            "bundle": bundle.content_hash,
            "base_artifacts": {
                "layers.json": _sha256(base_layers),
                "scene_checks.json": _sha256(base_scene),
                "checks.json": _sha256(base_checks),
                "requirements.json": _sha256(base_requirements),
                "acceptance.json": _sha256(base_acceptance),
            },
            "layer": materialized.layer_row,
            "scene": materialized.scene_contracts,
            "image": materialized.image_contracts,
            "acceptance": materialized.acceptance,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    view_hash = hashlib.sha256(digest_payload).hexdigest()
    view = shot / STATE_DIR / "views" / view_hash
    view.mkdir(parents=True, exist_ok=True)
    for name, document in (
        ("layers.json", layers_doc),
        ("scene_checks.json", scene_doc),
        ("checks.json", checks_doc),
        ("requirements.json", requirements_doc),
        ("acceptance.json", acceptance_doc),
    ):
        atomic_write(view / name, json.dumps(document, indent=2, sort_keys=True) + "\n")
    pointer = {
        "schema": VIEW_SCHEMA,
        "bundle_hash": bundle.content_hash,
        "view_hash": view_hash,
        "materialized_layers": sorted(
            str(row.get("id"))
            for row in layers_doc["layers"]
            if row.get("execution") != "jit_deferred"
        ),
        "artifacts": {
            name: (view / name).relative_to(shot).as_posix() for name in OVERLAY_ARTIFACTS
        },
        "hashes": {name: _sha256(view / name) for name in OVERLAY_ARTIFACTS},
    }
    atomic_write(shot / CURRENT, json.dumps(pointer, indent=2, sort_keys=True) + "\n")
    return shot / CURRENT
