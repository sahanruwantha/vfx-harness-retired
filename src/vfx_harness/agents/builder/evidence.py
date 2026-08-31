"""Stage 3 — the build + critic loop for one PLAN LAYER."""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageChops, ImageStat

from vfx_harness.agents.build_prompts import (
    capability_feedback_groups,
)
from vfx_harness.agents.builder.evidence_scope import (
    _scene_contract_path,
    _scene_ids_active_at_declared_frames,
)
from vfx_harness.agents.builder.evidence_scope import (
    _scene_ids_active_on_layer as _scene_ids_active_on_layer,
)
from vfx_harness.agents.builder.pkg import builder_package
from vfx_harness.blender.session import BlenderSession
from vfx_harness.domain.brief import Shot
from vfx_harness.domain.contracts import load_document
from vfx_harness.domain.work_units import (
    geometry_vis_protection_ids,
    geometry_vis_protection_ids_for_unit,
    layer_active_visible_fraction_ids,
    plan_selector_declared,
)
from vfx_harness.evidence import scene_checks
from vfx_harness.evidence.checks import layer_evidence
from vfx_harness.evidence.scene_checks import (
    FUNCTIONAL_KINDS,
    deferred_subject_composition_forecast_ids_for_unit,
    deferred_subject_composition_ids,
    deferred_subject_composition_ids_for_unit,
    functional_evidence,
    irreversible_deferred_subject_forecast_failures,
)
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.log import (
    log,
)
from vfx_harness.orchestration.ledger import Milestone, load_layers

if TYPE_CHECKING:
    from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority

RENDER_CAPTURE_SCHEMA = "vfx-harness.canonical-render-capture/v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _capture_digest(payload: dict) -> str:
    try:
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("canonical render capture contains non-JSON or non-finite data") from exc
    return hashlib.sha256(encoded).hexdigest()


def _stash_render_with_receipt(
    session: BlenderSession,
    shot: Shot,
    m: Milestone,
    tag: str,
    scale: float = 0.5,
    *,
    mode: str = "eevee",
) -> tuple[str, dict]:
    """Render, copy, and attest the exact settings and PNG bytes used by a judge."""
    result = session.render_full(frame=m.frame, mode=mode, scale=scale)
    if not isinstance(result, dict):
        raise ValueError("Blender render did not return a typed capture receipt")
    if result.get("frame") != int(m.frame) or result.get("mode") != mode:
        raise ValueError(
            "Blender render receipt does not match the requested canonical frame/mode"
        )
    resolution = result.get("resolution")
    render_state = result.get("render_state")
    if (
        not isinstance(resolution, list)
        or len(resolution) != 3
        or not all(isinstance(value, int) and value > 0 for value in resolution)
        or not isinstance(render_state, dict)
    ):
        raise ValueError("Blender render receipt is missing exact resolution/render state")
    src = Path(str(result.get("image_path") or ""))
    if not src.is_file():
        raise ValueError("Blender render receipt names a missing candidate image")
    dest_dir = run_artifacts.renders_dir(shot.folder)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{m.id}_{tag}.png"
    shutil.copyfile(src, dest)
    payload = {
        "schema": RENDER_CAPTURE_SCHEMA,
        "frame": int(m.frame),
        "mode": mode,
        "scale": float(scale),
        "resolution": resolution,
        "render_state": render_state,
        "warnings": list(result.get("warnings") or ()),
        "png_sha256": _sha256(dest),
    }
    receipt = {**payload, "capture_digest": _capture_digest(payload)}
    return dest.relative_to(shot.folder).as_posix(), receipt


def _stash_render(
    session: BlenderSession,
    shot: Shot,
    m: Milestone,
    tag: str,
    scale: float = 0.5,
    *,
    mode: str = "eevee",
) -> str:
    """Render the judge frame (eevee) and copy it into the shot for the critic.
    Returns the path relative to the shot folder."""
    render_rel, _receipt = _stash_render_with_receipt(
        session,
        shot,
        m,
        tag,
        scale,
        mode=mode,
    )
    return render_rel


def _image_reproduction(live: str | Path, canonical: str | Path) -> dict:
    """Judgment-free answer to "did the script reproduce the accepted pixels?".

    Reproduction is not a second aesthetic review.  The old implementation asked the
    critic again and called a score delta determinism; an unchanged image had already
    measured a two-point critic spread.  EEVEE can move a few antialiased edge values
    across clean replays, so this uses a deliberately tight near-equality band rather
    than requiring a byte-identical PNG container.
    """
    a, b = Path(live), Path(canonical)
    result = {"live": str(a), "canonical": str(b), "match": False}
    if not a.is_file() or not b.is_file():
        result["reason"] = "missing image"
        return result
    with Image.open(a) as ia, Image.open(b) as ib:
        ia, ib = ia.convert("RGB"), ib.convert("RGB")
        result["size"] = [ia.width, ia.height]
        if ia.size != ib.size:
            result["reason"] = f"size mismatch {ia.size} vs {ib.size}"
            return result
        diff = ImageChops.difference(ia, ib)
        hist = diff.histogram()
        counts = [0] * 256
        for index, count in enumerate(hist):
            counts[index % 256] += count
        total = max(1, ia.width * ia.height * 3)
        cumulative = 0
        p99 = 255
        for value, count in enumerate(counts):
            cumulative += count
            if cumulative >= 0.99 * total:
                p99 = value
                break
        mae = sum(stat * n for stat, n in enumerate(counts)) / total
        changed = sum(counts[5:]) / total
        rms = sum(ImageStat.Stat(diff).rms) / 3
        result.update(mae=round(mae, 4), rms=round(rms, 4), p99=p99, changed_gt4=round(changed, 6))
        result["match"] = bool(mae <= 1.0 and p99 <= 3 and changed <= 0.005)
        if not result["match"]:
            result["reason"] = "pixel delta exceeds reproduction tolerance"
    return result


def _geometry_protected_vis_ids(
    shot: Shot,
    layer,
    unit,
    frame: int | None = None,
    *,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> set[str]:
    """Lifecycle-active vis and deferred subject-composition ids a geometry unit must re-evaluate."""

    if unit is None or "geometry" not in getattr(unit, "provides", ()):
        return set()
    rows = load_document(
        _scene_contract_path(shot, selected_authority),
        "contracts",
    )
    stages = tuple(getattr(layer, "stages", ()) or ())
    if stages and any(getattr(item, "id", None) == getattr(unit, "id", None) for item in stages):
        protected = set(
            geometry_vis_protection_ids_for_unit(
                stages, unit, rows, str(layer.id), frame=frame
            )
        )
    else:
        # Diagnostic/backward callers without a typed layer DAG stay conservative.

        vis = layer_active_visible_fraction_ids(rows, str(layer.id), frame=frame)
        protected = set(geometry_vis_protection_ids(unit.provides, vis))
    scope = getattr(unit, "mutates", None)
    mutated = {
        str(item)
        for item in (
            *(getattr(scope, "roles", ()) or ()),
            *(getattr(scope, "dresses", ()) or ()),
        )
        if str(item)
    }
    by_id = {str(row.get("id")): row for row in rows if isinstance(row, dict) and row.get("id")}
    stages = tuple(getattr(layer, "stages", ()) or ())
    if stages:
        protected.update(
            deferred_subject_composition_ids_for_unit(
                rows, stages, unit, str(layer.id), frame
            )
        )
    else:
        # Backward diagnostic callers without a typed unit DAG keep the original exact
        # selector behavior; production always supplies the selected layer stages.

        for cid in deferred_subject_composition_ids(rows, str(layer.id), frame):
            roles = [
                str(item)
                for item in (by_id.get(cid) or {}).get("roles") or []
                if str(item)
            ]
            if roles and any(plan_selector_declared(role, mutated) for role in roles):
                protected.add(cid)
    return protected


def _geometry_protected_evidence(
    shot: Shot,
    layer,
    unit,
    session: BlenderSession,
    *,
    fallback_frame: int,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> tuple[set[str], list[dict]]:
    """Measure geometry-protected contracts at each contract's declared frame.

    The returned ids are canonical obligations.  The readings are additional evidence,
    not additional unit or layer judge frames.
    """
    if unit is None or layer is None:
        return set(), []
    protected = _geometry_protected_vis_ids(
        shot,
        layer,
        unit,
        selected_authority=selected_authority,
    )
    if not protected:
        return set(), []
    due = _scene_ids_active_at_declared_frames(
        shot,
        str(layer.id),
        protected,
        [int(fallback_frame)],
        selected_authority=selected_authority,
    )
    if not due:
        return set(), []

    rows = {
        str(row.get("id")): row
        for row in scene_checks.load_rows(shot.folder, selected_authority)
        if isinstance(row, dict) and row.get("id")
    }
    scheduled: dict[int, set[str]] = {}
    for cid in due:
        row = rows.get(cid) or {}
        declared = row.get("frames")
        if not isinstance(declared, (list, tuple)) or not declared:
            declared = [row.get("frame", fallback_frame)]
        for frame in declared:
            scheduled.setdefault(int(frame), set()).add(cid)
    evidence: list[dict] = []
    for frame, frame_ids in sorted(scheduled.items()):
        measured = scene_checks.layer_evidence(
            shot.folder,
            str(layer.id),
            frame=frame,
            session=session,
            selected_authority=selected_authority,
        )
        evidence.extend(
            {**row, "evidence_frame": int(frame)}
            for row in measured
            if str(row.get("id")) in frame_ids
        )
    return due, evidence


def _geometry_forecast_blocking_evidence(
    shot: Shot,
    layer,
    unit,
    units,
    session: BlenderSession,
    *,
    fallback_frame: int,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> tuple[set[str], list[dict]]:
    """Return only partial-union misses no successor geometry can repair."""
    if unit is None or layer is None:
        return set(), []

    rows = scene_checks.load_rows(shot.folder, selected_authority)
    forecast_ids = set(
        deferred_subject_composition_forecast_ids_for_unit(
            rows, tuple(units or ()), unit, str(layer.id)
        )
    )
    if not forecast_ids:
        return set(), []
    by_id = {
        str(row.get("id")): row
        for row in rows
        if isinstance(row, dict) and row.get("id")
    }
    scheduled: dict[int, set[str]] = {}
    for contract_id in forecast_ids:
        row = by_id[contract_id]
        declared = row.get("frames")
        if not isinstance(declared, (list, tuple)) or not declared:
            declared = [row.get("frame", fallback_frame)]
        for frame in declared:
            scheduled.setdefault(int(frame), set()).add(contract_id)
    measured: list[dict] = []
    for frame, frame_ids in sorted(scheduled.items()):
        measured.extend(
            {**row, "evidence_frame": int(frame)}
            for row in scene_checks.layer_evidence(
                shot.folder,
                str(layer.id),
                frame=frame,
                session=session,
                selected_authority=selected_authority,
            )
            if str(row.get("id")) in frame_ids
        )
    blockers = list(irreversible_deferred_subject_forecast_failures(rows, measured))
    return {str(row["id"]) for row in blockers}, blockers


def _fault_owner_options_for_unit(
    shot: Shot | None,
    layer,
    active_unit,
    *,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> list[dict]:
    """Same-layer ancestors plus earlier-layer camera providers (HIR-0127)."""
    fault_owner_options: list[dict] = []
    if active_unit is None or layer is None:
        return fault_owner_options
    units_by_id = {candidate.id: candidate for candidate in layer.stages}
    ancestors: set[str] = set()
    frontier = list(active_unit.depends_on)
    while frontier:
        candidate_id = frontier.pop()
        if candidate_id in ancestors:
            continue
        ancestors.add(candidate_id)
        candidate = units_by_id.get(candidate_id)
        if candidate is not None:
            frontier.extend(candidate.depends_on)
    seen: set[str] = set()
    for candidate in layer.stages:
        if candidate.id not in ancestors or candidate.id in seen:
            continue
        seen.add(candidate.id)
        fault_owner_options.append({
            "id": candidate.id,
            "title": candidate.title,
            "layer": str(layer.id),
            "roles": list(candidate.mutates.roles),
            "controls": list(candidate.mutates.controls),
        })
    if shot is None:
        return fault_owner_options
    try:

        all_layers = load_layers(shot, selected_authority=selected_authority)
    except (OSError, ValueError, KeyError, FileNotFoundError, json.JSONDecodeError):
        return fault_owner_options
    try:
        current = int(layer.id)
    except (TypeError, ValueError):
        return fault_owner_options
    for prior in all_layers.values():
        try:
            prior_id = int(prior.id)
        except (TypeError, ValueError):
            continue
        if prior_id >= current:
            continue
        for candidate in prior.stages:
            if "camera" not in (candidate.provides or ()) or candidate.id in seen:
                continue
            seen.add(candidate.id)
            fault_owner_options.append({
                "id": candidate.id,
                "title": candidate.title,
                "layer": str(prior.id),
                "roles": list(candidate.mutates.roles),
                "controls": list(candidate.mutates.controls),
            })
    return fault_owner_options


def _unit_evidence_ids(unit, frame: int) -> set[str] | None:
    """Exact evidence boundary for one work unit at one judge moment.

    Returning ``None`` preserves look-owning layer/composed evaluation. A unit
    returns a set even when empty so a malformed or missing binding cannot
    silently fall back to every contract in the parent layer. Look-less
    composition passes a fan-in unit so this is not ``None`` (HIR-0039).
    """
    if unit is None:
        return None
    return {
        binding.id
        for claim in unit.evaluation.claims
        if int(frame) in claim.moments
        for binding in claim.evidence
    }


def image_evidence_required_for(image_bindings, capabilities) -> bool:
    """Whether a unit must produce candidate-bound image evidence before sealing.

    Declared look ownership counts as much as an explicit image binding: geometry
    cannot certify how something reads, so run 20260823T154920Z closed a "reads as
    layered machined metal" claim with radial closure and sealed on scene contracts."""
    return bool(image_bindings) or bool(capabilities)


def look_unsettled_for(image_bindings, capabilities) -> bool:
    """Look ownership without bound image contracts must not lock live mutation.

    ``image_evidence_required_for`` is still true for look units (canonical still
    needs a critic). Using that same flag as a ``run_bpy`` lock treated 0/0 image
    rows as critic handoff (HIR-0044).
    """
    return bool(capability_feedback_groups(capabilities or ())) and not bool(image_bindings)


def _unit_requires_raster(
    shot: Shot,
    unit,
    *,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> bool:
    """Whether a bounded unit needs pixels to earn its verdict.

    Empty ``look_capabilities`` already keeps the critic off executable-only units,
    but the live and canonical loops historically rendered before reaching that
    decision. That is both wasted evidence and structurally impossible for a legal
    pre-camera control producer. Derive the raster boundary from typed authority and
    the canonical metric registry rather than from role, layer, or shot names
    (HIR-0114).
    """
    if unit is None:
        return True
    if tuple(getattr(unit, "look_capabilities", ()) or ()):
        return True
    required = [
        claim
        for claim in (getattr(getattr(unit, "evaluation", None), "claims", ()) or ())
        if getattr(claim, "required", False)
    ]
    if not required or any(
        getattr(claim, "authority", None) != "executable_required"
        for claim in required
    ):
        return True
    bindings = [
        binding
        for claim in required
        for binding in (getattr(claim, "evidence", ()) or ())
    ]
    if any(getattr(binding, "kind", None) != "scene_contract" for binding in bindings):
        return True

    # Some scene-contract rows are executable image instruments (transactional
    # render sweeps and frame statistics). Their domain comes from the one canonical
    # registry; a copied private list would drift exactly as camera requirements did.
    try:

        bound = {str(binding.id) for binding in bindings}
        return any(
            str(row.get("id")) in bound and row.get("kind") in FUNCTIONAL_KINDS
            for row in scene_checks.load_rows(shot.folder, selected_authority)
            if isinstance(row, dict)
        )
    except (OSError, ValueError, KeyError, TypeError):
        # A missing or malformed contract document will fail as executable evidence;
        # rasterizing cannot repair its authority.
        return False


def _unit_raster_mode(unit) -> str:
    """Use look-independent pixels when qualitative form is owed without look authority."""
    debt_medium = getattr(unit, "judgment_observation_medium", None)
    if debt_medium == "workbench_solid":
        return "solid"
    if debt_medium == "eevee":
        return "eevee"
    if unit is not None and not tuple(getattr(unit, "look_capabilities", ()) or ()):
        return "solid"
    return "eevee"


def _unit_completion_evidence_ids(unit) -> set[str] | None:
    """All evidence required before a bounded unit may stop mutating.

    Live iteration renders the primary judge for speed, but scene contracts are cheap and can
    probe every declared moment. Restricting the convergence guard to the primary frame lets a
    multi-moment unit seal before its other required claims have even been evaluated.
    """
    if unit is None:
        return None
    return {
        binding.id
        for claim in unit.evaluation.claims
        if claim.required
        for binding in claim.evidence
    }


def _unit_scene_evidence_ids(unit) -> set[str] | None:
    """Scene-contract ids for the live probe (HIR-0048).

    ``_unit_completion_evidence_ids`` includes image-contract debts. Mixing those
    into ``_scene_completion_state`` taught selector-miss copy and critic handoff.
    """
    if unit is None:
        return None
    ids: set[str] = set()
    for claim in unit.evaluation.claims:
        if not claim.required:
            continue
        for binding in claim.evidence:
            if binding.kind == "scene_contract":
                ids.add(binding.id)
    context = unit.evaluation.composition_context
    if context:
        ids.update(context.contract_ids)
    return ids


def _scope_unit_evidence(
    evidence: list[dict], unit, frame: int, extra_ids: set[str] | None = None
) -> list[dict]:
    """Keep only evidence explicitly bound by the active unit's moment."""
    ids = _unit_evidence_ids(unit, frame)
    if ids is None:
        return evidence
    if extra_ids:
        ids = set(ids) | {str(item) for item in extra_ids}
    return _scope_bound_evidence(evidence, ids)


def _scope_bound_evidence(
    evidence: list[dict], ids: set[str] | None
) -> list[dict]:
    """Apply one exact compiled evidence boundary to an observation stream.

    ``None`` is the intentional legacy/layer-wide authority. An empty set is a
    bounded unit with no matching rows and must stay empty. Candidate read-back,
    live evaluation, and canonical evaluation share this distinction so a sibling
    failure cannot become repair authority (HIR-0115).
    """
    if ids is None:
        return evidence
    return [
        row
        for row in evidence
        if str(row.get("id")) in ids or row.get("source") == "builder_state"
    ]


def _unit_evidence_ids_by_frame(
    shot: Shot,
    layer,
    unit,
    judges,
    *,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> dict[str, list[str]] | None:
    """Compile the exact candidate read-back boundary for each judged frame."""
    if unit is None:
        return None
    compiled: dict[str, list[str]] = {}
    for frame, _ref in judges:
        ids = set(_unit_evidence_ids(unit, int(frame)) or set())
        if layer is not None:
            with contextlib.suppress(OSError, ValueError, KeyError):
                ids.update(
                    _scene_ids_active_at_declared_frames(
                        shot,
                        str(layer.id),
                        _geometry_protected_vis_ids(
                            shot,
                            layer,
                            unit,
                            selected_authority=selected_authority,
                        ),
                        [int(frame)],
                        selected_authority=selected_authority,
                    )
                )
            with contextlib.suppress(OSError, ValueError, KeyError, json.JSONDecodeError):
                ids = _scene_ids_active_at_declared_frames(
                    shot,
                    str(layer.id),
                    ids,
                    [int(frame)],
                    selected_authority=selected_authority,
                )
        compiled[str(int(frame))] = sorted(ids)
    return compiled


def _render_evidence(
    shot: Shot,
    layer,
    m: Milestone,
    render_rel: str | None,
    session: BlenderSession,
    *,
    active_unit=None,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> list[dict]:
    if layer is None:
        return []
    evidence = []
    stage = (
        "post_grade"
        if any("grade" in str(axis).lower() for axis in (getattr(layer, "owns", ()) or ()))
        else "pre_grade"
    )
    if render_rel:
        try:

            evidence.extend(
                layer_evidence(
                    shot.folder,
                    str(layer.id),
                    frame=m.frame,
                    ref=m.ref,
                    render=render_rel,
                    stage=stage,
                    selected_authority=selected_authority,
                )
            )
        except Exception as exc:
            log(f"! image evidence unavailable: {str(exc)[:90]}", 1)
    try:

        evidence.extend(
            scene_checks.layer_evidence(
                shot.folder,
                str(layer.id),
                frame=m.frame,
                session=session,
                selected_authority=selected_authority,
            )
        )
        if render_rel:
            evidence.extend(
                functional_evidence(
                    shot.folder,
                    str(layer.id),
                    session=session,
                    selected_authority=selected_authority,
                )
            )
    except Exception as exc:
        log(f"! live-scene evidence unavailable: {str(exc)[:90]}", 1)
    evidence.extend(builder_package()._worklist_evidence(shot.folder, str(layer.id), active_unit))
    extra = set()
    if active_unit is not None:
        try:
            extra, protected_evidence = _geometry_protected_evidence(
                shot,
                layer,
                active_unit,
                session,
                fallback_frame=int(m.frame),
                selected_authority=selected_authority,
            )
            evidence.extend(protected_evidence)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            extra = set()
        blocker_ids, blocker_evidence = _geometry_forecast_blocking_evidence(
            shot,
            layer,
            active_unit,
            getattr(layer, "stages", ()),
            session,
            fallback_frame=int(m.frame),
            selected_authority=selected_authority,
        )
        extra.update(blocker_ids)
        evidence.extend(blocker_evidence)
    return _scope_unit_evidence(evidence, active_unit, int(m.frame), extra_ids=extra)


def _forecast_blocker_ids(evidence: list[dict]) -> set[str]:
    """Required ids promoted from the separate deferred-forecast evidence channel."""
    return {
        str(row.get("id"))
        for row in evidence
        if row.get("id")
        and row.get("source") == "deferred_subject_forecast_blocker"
        and not row.get("pass")
    }


def _reproduction_hint(row: dict) -> str:
    """The exact local invocation that re-measures a failing contract row.

    Guidance that arrives attached to the failure it explains gets used; the same
    guidance delivered ambiently does not (`vfx inspect` flags diagnostic tools that
    were never called on every measured layer)."""
    kind = str(row.get("metric") or row.get("kind") or "")
    roles = [token for token in (row.get("roles") or []) if token]
    objects = [name for name in (row.get("objects") or []) if name]
    subject = objects[0] if objects else "<role object>"
    addr = f"role='{roles[0]}'" if roles else f"object='{subject}'"
    frame = row.get("frame") or (row.get("frames") or [None])[0]
    if kind.startswith("bbox_"):
        return f"check_scene(kind='bbox', {addr}, frame={frame})"
    if kind == "keyframe_schedule":
        return f"list_keyframes({addr})"
    if kind in {"curve_derivative_max", "onset_order", "radial_distance_trend", "transform_return_delta"}:
        return f"check_scene(kind='motion', {addr}, frames=[…judged window…])"
    if kind == "mesh_vertex_count":
        return f"check_scene(kind='mesh', {addr})"
    if kind in {"path_clearance_min"}:
        return f"check_scene(kind='visibility', {addr}, frame={frame}) + run_bpy distance probe"
    return ""


def _scene_contract_issue(row: dict) -> str:
    """Turn one evidence row into a repair-facing issue string.

    A metric that cannot apply to the subject CLASS is a binding defect
    (``smooth_fraction`` on a camera rig). A ``keyframe_schedule`` path or
    key-set miss is a measured fail: the kind applies; the named RNA path is
    unkeyed or on another data_path (HIR-0050).
    """
    head = f"[check:{row['id']}]"
    if row.get("value") is None:
        # The evidence row already carries WHY it could not be measured; withholding
        # it left a probe of the live scene as the only way to learn that 50 objects
        # were in frame and the projection still returned nothing.
        why = str(row.get("error") or row.get("note") or "").strip()
        # Selector ambiguity/absence is the BUILDER's tagging to fix — run
        # 20260825T044518Z's repair read the blanket "needs re-materialization"
        # verdict for a 7-nodes-one-tag defect and deferred a fix that was one
        # retag away. Only a metric that cannot apply to the subject CLASS is a
        # binding defect.
        lowered = why.lower()
        metric = str(row.get("metric") or row.get("kind") or "")
        if metric == "keyframe_schedule":
            return (
                f"{head} executable contract fails: keyframe_schedule could not read "
                f"the named sample path (target {row.get('target')})"
                + (f" — {why}" if why else "")
                + ". Key that path on the object or its data-block; list_keyframes "
                "names every fcurve data_path present. This is this build's defect "
                "to fix, not a binding defect."
            )
        if "matched 0 nodes" in lowered or "matched no objects" in lowered:
            return (
                f"{head} {row.get('metric')} could not be measured: {why}. The "
                "contract's subject does not exist yet — CREATE it and tag it with "
                "the contract's role/control; this is this build's defect to fix."
            )
        if "matched" in lowered and "nodes" in lowered:
            return (
                f"{head} {row.get('metric')} could not be measured: {why}. The "
                "semantic selector must resolve to EXACTLY ONE node — remove the "
                "role/control tag from the duplicates so one node carries it; this "
                "is this build's tagging defect to fix, not a binding defect."
            )
        if "has no requested" in lowered and "socket" in lowered:
            return (
                f"{head} {row.get('metric')} could not be measured: {why}. When the "
                "contract names no socket, resolution looks for a socket literally "
                "named 'Value' (inputs then outputs) — tag a ShaderNodeValue whose "
                "output drives the quantity, or a node with a 'Value' socket; this "
                "is this build's tagging defect to fix, not a binding defect."
            )
        return (
            f"{head} contract is INAPPLICABLE to its subject: {row.get('metric')} "
            f"could not be measured on these roles (target {row.get('target')})"
            + (f" — {why}" if why else "")
            + ". This is a binding defect, not a build defect — the metric cannot "
            "apply to what the claim names; it needs re-materialization, not a repair."
        )
    hint = _reproduction_hint(row)
    note = str(row.get("note") or "").strip()
    return (
        f"{head} executable contract fails: {row.get('metric')} reads "
        f"{row.get('value')} against {row.get('target')}"
        + (f" — {note}" if note else "")
        + (f" · reproduce: {hint}" if hint else "")
    )
