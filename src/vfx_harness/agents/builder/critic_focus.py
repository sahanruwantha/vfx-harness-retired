"""Stage 3 — the build + critic loop for one PLAN LAYER."""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

import anyio
from PIL import Image

from vfx_harness.agents.builder.models import MAX_CANON_REPAIRS
from vfx_harness.agents.builder.state import _FOCUS_RENDER_LOCK
from vfx_harness.blender.session import BlenderSession
from vfx_harness.domain.brief import Shot
from vfx_harness.domain.contracts import active_for, load_document
from vfx_harness.evidence import scene_checks
from vfx_harness.evidence.claim_evidence import Observation, reconcile_observations
from vfx_harness.evidence.compare_panels import focus_signal, save_focus_sheet, validate_crop
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import authority_selection
from vfx_harness.orchestration.ledger import Milestone, load_layers, load_layers_from_path


def _snapshot_artifact(
    shot: Shot,
    selected_authority: authority_selection.ResolvedSelectedAuthority,
    name: str,
) -> Path:
    if selected_authority.plan is None:
        return shot.folder / name
    try:
        return selected_authority.artifact_paths[name]
    except KeyError as exc:
        raise ValueError(f"selected critic authority omits {name}") from exc


def _focus_references(
    shot: Shot,
    m: Milestone,
    allowed_frames: list[int] | tuple[int, ...] | set[int] | None = None,
    *,
    selected_authority: authority_selection.ResolvedSelectedAuthority | None = None,
) -> dict[int, str]:
    """Reference-bearing frames that can produce an aligned optical focus panel.

    Live layer review may inspect any judged frame.  A canonical *per-frame* review is
    different: its verdict is attributed to one frame and used by the transactional
    repair guard for that frame.  In that mode callers restrict this map so a defect at
    f120 cannot silently turn the recorded f40 verdict into a failure.
    """
    references = {int(m.frame): str(m.ref)}
    layer_id = str(m.id).split("@", 1)[0]
    try:
        selected = selected_authority or authority_selection.resolve_selected_authority(
            shot.folder
        )
        layer = load_layers_from_path(
            _snapshot_artifact(shot, selected, "layers.json")
        ).get(layer_id)
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        layer = None
    if layer is not None:
        references.update({int(frame): str(ref) for frame, ref in layer.judges})
    allowed = {int(frame) for frame in allowed_frames} if allowed_frames is not None else None
    return {
        frame: ref
        for frame, ref in references.items()
        if 1 <= frame <= shot.frames and (shot.folder / ref).is_file()
        if allowed is None or frame in allowed
    }


def _motion_strip_crop(crop: list[float], frames: list[int], source_frame: int) -> list[float]:
    """Map one strip-global crop into a single frame-local crop.

    A request crossing a panel seam is ambiguous by construction: there is no single
    Blender frame/reference pair that can be optically rerendered for it.
    """
    if not frames:
        raise ValueError("motion-strip focus requires strip frames")
    x0, y0, x1, y1 = crop
    count = len(frames)
    first = min(count - 1, int(x0 * count))
    last = min(count - 1, int(max(x0, x1 - 1e-9) * count))
    if first != last:
        raise ValueError("motion-strip focus crop crosses a panel boundary")
    if int(frames[first]) != int(source_frame):
        raise ValueError(f"motion-strip crop selects f{frames[first]}, not declared f{source_frame}")
    return [round(x0 * count - first, 6), y0, round(x1 * count - first, 6), y1]


def _focus_requests(
    verdict: dict,
    axes: list[tuple[str, str]],
    *,
    focus_references: dict[int, str],
    motion_frames: list[int] | None = None,
) -> list[dict]:
    """Validate critic-selected crops before they can trigger renders or extra judging.

    A request is supplemental evidence, not an escape hatch from scoring the full frame:
    it must name an in-scope axis that is actually borderline/failing, be a genuine zoom
    rather than nearly the whole image, and use the public top-left coordinate convention.
    """
    allowed = {key for key, _desc in axes}
    scores = verdict.get("scores") or {}
    out, seen = [], set()
    for index, item in enumerate(verdict.get("focus_requests") or []):
        if not isinstance(item, dict) or len(out) >= 2:
            continue
        axis = str(item.get("axis") or "")
        score = scores.get(axis)
        if axis not in allowed or not isinstance(score, (int, float)) or score > 3:
            continue
        source = str(item.get("source") or "")
        try:
            source_frame = int(item.get("source_frame"))
        except (TypeError, ValueError):
            continue
        if source not in {"candidate_frame", "motion_strip"} or source_frame not in focus_references:
            continue
        try:
            requested_crop = list(validate_crop(item.get("region")))
            crop = (
                _motion_strip_crop(requested_crop, list(motion_frames or []), source_frame)
                if source == "motion_strip"
                else requested_crop
            )
            crop = list(validate_crop(crop))
        except ValueError:
            continue
        if crop[2] - crop[0] < 0.02 or crop[3] - crop[1] < 0.02:
            continue  # below this, even the capped optical render has too few real pixels
        max_span = 0.9 if source == "motion_strip" else 0.75
        if crop[2] - crop[0] > max_span or crop[3] - crop[1] > 0.75:
            continue
        raw_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(item.get("id") or "")).strip("_")
        panel_id = (raw_id or f"focus_{index + 1}")[:40]
        if panel_id in seen:
            panel_id = f"{panel_id}_{index + 1}"
        seen.add(panel_id)
        reason = " ".join(str(item.get("reason") or "").split())[:180]
        if not reason:
            continue
        out.append(
            {
                "id": panel_id,
                "axis": axis,
                "source": source,
                "source_frame": source_frame,
                "requested_crop": requested_crop,
                "crop": crop,
                "reference": focus_references[source_frame],
                "reason": reason,
            }
        )
    return out


async def _make_focus_panels(
    shot: Shot,
    m: Milestone,
    session: BlenderSession,
    requests: list[dict],
    candidate_rel: str = "candidate",
) -> list[dict]:
    """Optically rerender frame-local crops against that frame's matching reference."""
    panels = []
    rendered_other_frame = False
    try:
        async with _FOCUS_RENDER_LOCK:
            for item in requests[:2]:
                crop = item["crop"]
                source_frame = int(item["source_frame"])
                rendered_other_frame = rendered_other_frame or source_frame != int(m.frame)
                reference = shot.folder / item["reference"]
                fraction = max(crop[2] - crop[0], crop[3] - crop[1])
                res_pct = min(800, max(150, round(110 / fraction)))
                rendered = await anyio.to_thread.run_sync(
                    lambda c=crop, pct=res_pct, f=source_frame: session.render_full(
                        frame=f,
                        mode="eevee",
                        scale=0.5,
                        **{"pass": "beauty"},
                        shade="beauty",
                        crop=c,
                        res_pct=pct,
                    )
                )
                candidate_tag = re.sub(r"[^a-zA-Z0-9_-]+", "_", Path(candidate_rel).stem)[:60]
                dest = (run_artifacts.renders_dir(shot.folder)
                        / f"{candidate_tag}_focus_{item['id']}_f{source_frame}.jpg")
                dest.parent.mkdir(parents=True, exist_ok=True)
                meta = await anyio.to_thread.run_sync(
                    lambda r=rendered, c=crop, d=dest, ref=reference: save_focus_sheet(
                        r["image_path"], ref, c, d, ("side_by_side", "wipe")
                    )
                )
                if not meta["has_signal"]:
                    dest.unlink(missing_ok=True)
                    raise ValueError(f"focus {item['id']} is empty in both candidate and reference at f{source_frame}")
                panels.append(
                    {
                        **item,
                        "res_pct": res_pct,
                        "image_rel": str(dest.relative_to(shot.folder)),
                        "candidate_source_px": meta["candidate_source_px"],
                        "reference_crop_px": meta["reference_crop_px"],
                        "comparison_px": meta["comparison_px"],
                        "views": meta["views"],
                        "upscaled": meta["upscaled"],
                        "mean_abs_diff": meta["mean_abs_diff"],
                        "signal": meta["signal"],
                    }
                )
    finally:
        if rendered_other_frame:
            await anyio.to_thread.run_sync(
                lambda: session.run(f"bpy.context.scene.frame_set({int(m.frame)})", journal=False)
            )
    return panels


def _required_focus_requests(
    shot: Shot,
    layer_id: str,
    frame: int,
    axes: list[tuple[str, str]],
    *,
    layers: dict | None = None,
    selected_authority: authority_selection.ResolvedSelectedAuthority | None = None,
) -> list[dict]:
    """Load planner-declared optical evidence that must reach the first judge.

    Whole-frame vision cannot reliably grade a feature occupying a few encoder patches.
    Making the crop a contract property turns zooming from a critic-dependent recovery
    path into deterministic evidence acquisition for every shot.
    """

    selected = selected_authority or authority_selection.resolve_selected_authority(
        shot.folder
    )
    path = _snapshot_artifact(shot, selected, "checks.json")
    if not path.is_file():
        return []
    try:
        loaded = (
            layers
            if layers is not None
            else load_layers_from_path(
                _snapshot_artifact(shot, selected, "layers.json")
            )
        )
        reference_by_frame = {
            int(judge_frame): str(ref) for judge_frame, ref in loaded[str(layer_id)].judges
        }
    except (KeyError, FileNotFoundError, ValueError, json.JSONDecodeError):
        reference_by_frame = {}
    owned_axes = {str(key) for key, _description in axes}
    requests = []
    for row in load_document(path, "checks"):
        focus = row.get("focus")
        if not isinstance(focus, dict) or not focus.get("required"):
            continue
        if not active_for(row, layer_id, frame):
            continue
        axis = str(row.get("axis") or "")
        if axis not in owned_axes:
            continue
        reference = reference_by_frame.get(int(frame))
        if not reference or not (shot.folder / reference).is_file():
            raise ValueError(f"check {row.get('id')} requires focus at f{frame}, but that frame has no layer reference")
        try:
            crop = list(validate_crop(focus.get("crop")))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"check {row.get('id')} has an invalid required focus crop") from exc
        reason = " ".join(str(focus.get("reason") or row.get("note") or "").split())
        if not reason:
            raise ValueError(f"check {row.get('id')} required focus needs a reason")
        requests.append(
            {
                "id": str(focus.get("id") or row.get("id"))[:40],
                "axis": axis,
                "source": "candidate_frame",
                "source_frame": int(frame),
                "requested_crop": crop,
                "crop": crop,
                "reference": reference,
                "reason": reason[:180],
            }
        )
    if len(requests) > 2:
        raise ValueError("at most two required focus contracts may target one judge frame")
    return requests


def _claim_context(
    shot: Shot,
    m: Milestone,
    *,
    enabled: bool,
    active_unit=None,
    selected_authority: authority_selection.ResolvedSelectedAuthority | None = None,
) -> tuple[list[dict], dict[str, frozenset[str]], set[str]]:
    """Return only claims that are active at this exact judge moment.

    The critic sees a read-only manifest.  It may point at a claim, but only the harness
    resolves that claim to evidence and decides whether repair is authorized.
    """
    if not enabled:
        return [], {}, set()
    milestone_parts = str(m.id).split("@", 1)
    layer_id = milestone_parts[0]
    unit_id = milestone_parts[1] if len(milestone_parts) == 2 and not milestone_parts[1].startswith("f") else None
    if active_unit is not None:
        units = (active_unit,)
    else:
        try:
            if selected_authority is not None and selected_authority.plan is None:
                loaded_layers = load_layers_from_path(shot.folder / "layers.json")
            else:
                loaded_layers = load_layers(
                    shot,
                    selected_authority=selected_authority,
                )
            units = loaded_layers[layer_id].stages
        except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
            return [], {}, set()
    claims, bindings, qualified = [], {}, set()
    for unit in units:
        if unit_id is not None and unit.id != unit_id:
            continue
        for claim in unit.evaluation.claims:
            if int(m.frame) not in claim.moments:
                continue
            claims.append(
                {
                    "id": claim.id,
                    "proposition": claim.proposition,
                    "axis": claim.axis,
                    "property": claim.property,
                    "roles": list(claim.subject_roles),
                    "controls": list(claim.subject_controls),
                    "authority": claim.authority,
                    "evidence_ids": list(claim.binding_ids),
                }
            )
            bindings[claim.id] = frozenset(claim.binding_ids)
            if claim.authority == "qualified_qualitative_required":
                qualified.add(claim.id)
    return claims, bindings, qualified


def _filter_critic_issues(
    verdict: dict,
    evidence: list[dict] | None,
    *,
    claim_bindings: dict[str, frozenset[str]] | None = None,
    qualified_claims: set[str] | None = None,
) -> dict:
    """Reconcile typed critic observations against exact claim/evidence bindings.

    A passing check is a contradiction only for the property that explicitly owns it.
    An uncovered measurable defect is a plan defect (``contract_gap``), never an
    instruction for the builder to make an unplanned scene mutation.
    """
    parsed, parse_errors = [], []
    for index, raw in enumerate(verdict.get("observations") or []):
        try:
            parsed.append(Observation.parse(raw, f"observations[{index}]"))
        except ValueError as exc:
            parse_errors.append({"state": "protocol_error", "reason": str(exc), "observation": raw})
    if not verdict.get("pass") and not parsed and not parse_errors:
        parse_errors.append(
            {
                "state": "protocol_error",
                "reason": "a failing scorecard must include at least one typed observation",
                "observation": {},
            }
        )
    if verdict.get("pass") and parsed:
        parse_errors.append(
            {
                "state": "protocol_error",
                "reason": "a passing scorecard must not include blocking observations",
                "observation": {},
            }
        )
    reconciled = reconcile_observations(
        parsed,
        evidence or [],
        claim_bindings=claim_bindings,
        qualified_claims=qualified_claims or set(),
    )
    rows = reconciled["observations"] + parse_errors
    by_state = {
        state: [row for row in rows if row.get("state") == state]
        for state in ("actionable", "contradicted", "contract_gap", "unverified_qualitative", "protocol_error")
    }
    verdict["observation_reconciliation"] = rows
    verdict["issues"] = [
        str(row["observation"]["action"])
        for row in by_state["actionable"]
    ]
    verdict["contradicted_issues"] = [
        {
            "issue": row["observation"]["observation"],
            "check_ids": row.get("check_ids", []),
            "reason": row["reason"],
        }
        for row in by_state["contradicted"]
    ]
    verdict["contract_gaps"] = by_state["contract_gap"]
    verdict["unverified_observations"] = by_state["unverified_qualitative"]
    verdict["protocol_errors"] = by_state["protocol_error"]
    verdict["contract_gap"] = bool(by_state["contract_gap"])
    verdict["needs_human"] = bool(by_state["unverified_qualitative"])
    if not verdict.get("pass") and not verdict["issues"]:
        verdict["judge_conflict"] = bool(by_state["contradicted"] or by_state["protocol_error"])
    return verdict


def _audit_panel_citations(verdict: dict, focus_panels: list[dict] | None) -> dict:
    """Keep focus provenance honest; a model cannot cite a panel it was not shown."""
    valid = {str(panel.get("id")) for panel in (focus_panels or []) if panel.get("id")}
    invalid = []
    for index, meta in enumerate(verdict.get("observations") or []):
        if not isinstance(meta, dict):
            continue
        cited = [str(panel_id) for panel_id in (meta.get("panel_ids") or [])]
        bad = [panel_id for panel_id in cited if panel_id not in valid]
        if bad:
            invalid.append({"observation_index": index, "observation_id": meta.get("id"), "panel_ids": bad})
        meta["panel_ids"] = [panel_id for panel_id in cited if panel_id in valid]
    verdict["invalid_panel_citations"] = invalid
    return verdict


def _apply_evidence_gate(verdict: dict, evidence: list[dict] | None) -> dict:
    """Let proven planner contracts overrule a critic pass.

    Evidence is symmetric: a passing check prevents an invented measurable repair, and a
    failing authoritative check prevents a flattering visual score from shipping it.
    Builder-authored checks are shown to the critic but remain advisory because the author
    chose both the measurement and its band.
    """
    failures = [item for item in (evidence or []) if item.get("authoritative") and not item.get("pass")]
    verdict["evidence_failures"] = failures
    if not failures:
        return verdict
    issues = list(verdict.get("issues") or [])
    for item in failures:
        tag = f"[check:{item['id']}]"
        if any(str(issue).lstrip().startswith(tag) for issue in issues):
            continue
        detail = ""
        if item.get("open_items"):
            detail = "; unresolved: " + " | ".join(str(value) for value in item["open_items"][:3])
        issues.append(
            f"{tag} executable contract fails: {item.get('metric')} reads "
            f"{item.get('value')} against {item.get('target')}; correct the property "
            f"measured by this check{detail}"
        )
    verdict["issues"] = issues[:6]
    verdict["pass"] = False
    verdict["judge_conflict"] = False
    verdict["decided_by"] = "checks"
    return verdict


def _repair_delta(pre: list, post: list) -> dict:
    """What a canonical repair round actually achieved, per judged frame.

    Two questions, and the loop used to get the second one wrong.

    `broke` — frames that PASSED before the repair and do not now. A repair that trades
    a passing frame for a failing one is not a fix, and the caller reverts on it.

    `progressed` — whether the round moved toward a pass at all. This used to compare the
    SUM of the failing frames' means, which is the wrong statistic for a conjunctive
    requirement: the layer passes only when EVERY judged frame clears the bar, so the
    binding constraint is the worst frame, while a sum rises whenever any one frame does.
    A four-frame layer going 1.0→2.0 on one frame and holding the rest scored as
    improvement and bought another round that could not lead to a pass. Layer 2 spent five
    attempts in that state — the sum crept up while the minimum sat still. Progress means
    the worst failing frame improved, or there are fewer failing frames than before.
    """
    was = {f: v.get("mean") for (f, _r), v in pre}
    now = {f: v.get("mean") for (f, _r), v in post}
    was_pass = {f for (f, _r), v in pre if v.get("pass")}
    failed = [f for (f, _r), v in pre if not v.get("pass")]
    still = [f for (f, _r), v in post if not v.get("pass")]
    # Only frames judged BOTH times can be compared. A frame missing from `post` has no
    # score to improve on, and treating its absence as 0 would read as a regression.
    common = [f for f in failed if f in now]
    was_worst = min((was.get(f) or 0) for f in common) if common else None
    now_worst = min((now.get(f) or 0) for f in common) if common else None

    def contract_failures(rows: list) -> int:
        return sum(
            1
            for (_frame_ref, verdict) in rows
            for item in (verdict.get("evidence") or [])
            if item.get("authoritative") and not item.get("pass")
        )

    was_contract = contract_failures(pre)
    now_contract = contract_failures(post)
    progressed = (
        (was_worst is not None and now_worst > was_worst)
        or len(still) < len(failed)
        or (was_contract > 0 and now_contract < was_contract)
    )
    return {
        "was": was,
        "now": now,
        "broke": sorted(f for (f, _r), v in post if f in was_pass and not v.get("pass")),
        "was_worst": was_worst,
        "now_worst": now_worst,
        "was_failing": len(failed),
        "now_failing": len(still),
        "was_contract_failures": was_contract,
        "now_contract_failures": now_contract,
        "progressed": progressed,
    }


def _repair_action(delta: dict, attempt: int, max_attempts: int | None = None) -> str:
    """Choose the transactional disposition of one canonical repair.

    A rejected patch is rolled back, but rejection is evidence about an approach—not a
    reason to throw away an unused repair attempt.  The old loop stopped immediately on
    regression, which made ``MAX_CANON_REPAIRS = 2`` misleading: Layer 4 used one
    geometric approach, regressed a protected frame, and was denied its second attempt.
    """
    max_attempts = MAX_CANON_REPAIRS if max_attempts is None else max_attempts
    rejected = bool(delta.get("broke")) or not bool(delta.get("progressed"))
    if not rejected:
        return "accept"
    return "rollback_retry" if attempt < max_attempts else "rollback_stop"


def _canonical_failing_ids(verdicts: list) -> set[str]:
    ids: set[str] = set()
    for _frame_ref, verdict in verdicts or []:
        for row in verdict.get("evidence") or []:
            if row.get("id") and row.get("authoritative") and not row.get("pass"):
                ids.add(str(row["id"]))
    return ids


def _unsatisfiable_pair_findings(
    shot: Shot,
    failing_ids: set[str],
    *,
    selected_authority: authority_selection.ResolvedSelectedAuthority | None = None,
) -> list[dict]:
    """Schedule vs smoothness pairs that failing evidence has already proved unsatisfiable."""

    if not failing_ids:
        return []
    try:
        selected = selected_authority or authority_selection.resolve_selected_authority(
            shot.folder
        )
        rows = load_document(
            _snapshot_artifact(shot, selected, "scene_checks.json"),
            "contracts",
        )
    except (OSError, ValueError, KeyError):
        return []
    return [
        pair
        for pair in scene_checks.schedule_smoothness_contradictions(rows)
        if {pair["schedule_id"], pair["smoothness_id"]} & failing_ids
    ]


def _image_optical_signal(path: Path) -> dict | None:
    if not path.is_file():
        return None

    with Image.open(path) as image:
        return focus_signal(image)


def _repair_change_summary(before: str, after: str, limit: int = 3200) -> str:
    """Small, prompt-safe account of a rejected script edit for the next repair agent."""
    changed = [
        line
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="")
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    text = "\n".join(changed)
    return text[:limit] + ("\n…" if len(text) > limit else "")
