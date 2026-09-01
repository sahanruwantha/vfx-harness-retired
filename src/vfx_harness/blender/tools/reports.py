"""Agent SDK tools over a warm BlenderSession.

`build_blender_tools(session)` returns an in-process MCP server and the list of
qualified tool names to put in `ClaudeAgentOptions.allowed_tools`. Render tools
return image content blocks, so the agent literally sees the frames it makes.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping
from pathlib import Path

from PIL import Image

from vfx_harness.agents.build_prompts import axis_feedback_groups
from vfx_harness.blender.tools.payment import _text
from vfx_harness.domain.image_debts import classify_cannot_express, debts_from_dicts, normalize_evidence_id
from vfx_harness.domain.work_units import read_document
from vfx_harness.evidence import checks as image_checks
from vfx_harness.evidence.scene_checks import FUNCTIONAL_KINDS, irreversible_deferred_subject_forecast_failures

SERVER_NAME = "blender"

# Renders are downscaled to JPEG before base64 so a single tool-result message stays
# well under the SDK's stdio buffer (a detailed 960px PNG base64s to >1MB and crashes
# it).
#
# 1024 was chosen against the stdio limit alone and is too small for the judgement it
# feeds. Claude tokenises images in 28x28 px patches: fine discrimination is measured at
# 0.00 accuracy for features spanning <=2 patches (56px) and 1.00 at >=4 patches (112px).
# At 1024px wide, this shot's hero facade piers span ~6px — a fifth of ONE patch, i.e.
# no representational slot in the encoder at all. 2048 doubles every feature's patch
# count and a 2048x1024 JPEG q85 base64s to ~300-450KB, comfortably inside the buffer.
# The side-by-side sheet is two images wide, so it needs its own ceiling or the same
# height that is right for ONE frame doubles the payload.
_SHEET_MAX_W = 3072


def _scene_completion_state(evidence: list[dict], layer_id: str, required_ids: set[str] | None = None) -> dict:
    """Separate healthy inherited inputs from authority to seal the current layer.

    Persistent upstream interfaces must pass, but they cannot prove that a downstream
    department has performed its own work.  Only an active contract owned by the current
    layer may close the live mutation gate.  Layers with image-only/subjective completion
    keep mutation open until they voluntarily hand off to the critic.

    `required_ids` are the SCENE contracts bound to the active unit's required claims
    (HIR-0048: image-contract debts are a separate card, not a scene-selector miss).
    A contract that is never evaluated — selector matched nothing, probe errored, frame
    group never ran — is neither a pass nor a failure, so presence-based sealing let a
    unit close while a required claim had no evidence at all. Completeness is therefore
    checked explicitly: absent required evidence blocks sealing exactly like a failure.
    """
    authoritative = [row for row in evidence if row.get("authoritative")]
    current = [row for row in authoritative if str(row.get("owner_layer") or "") == str(layer_id)]
    failures = [row for row in authoritative if not row.get("pass")]
    evaluated = {str(row.get("id")) for row in evidence}
    missing = sorted(set(required_ids) - evaluated) if required_ids else []
    return {
        "authoritative": authoritative,
        "current": current,
        "failures": failures,
        "missing": missing,
        "interfaces_ready": bool(authoritative) and not failures and not missing,
        "may_seal": bool(current) and not failures and not missing,
    }


def _deferred_subject_forecast_note(evidence: list[dict], contract_rows: list[dict] | None = None) -> str:
    """Teach partial producers which forecasts diagnose and which block freeze."""
    if not evidence:
        return ""

    blockers = list(irreversible_deferred_subject_forecast_failures(contract_rows or [], evidence))
    blocker_ids = {str(row.get("id")) for row in blockers}
    diagnostics = [row for row in evidence if str(row.get("id")) not in blocker_ids]

    sections: list[str] = []
    if blockers:
        sections.append(
            "\nDEFERRED SUBJECT FORECAST BLOCKERS — REQUIRED BEFORE FREEZE; these "
            "partial-subject rows do not pay the owner contract, but monotonic union "
            "geometry proves successors cannot repair them. Repair this producer now "
            "or call cannot_express_in_scope:\n"
            + "\n".join(
                f"  {row.get('id', '?')}: {row.get('metric')}={row.get('value')} "
                f"target {row.get('target')} — {row.get('note')}"
                for row in blockers[:6]
            )
        )
    if diagnostics:
        sections.append(
            "\nDEFERRED SUBJECT FORECASTS — DIAGNOSTIC ONLY; these partial-subject "
            "readings cannot pay acceptance and the dependency-complete producer will "
            "re-evaluate the final union:\n"
            + "\n".join(
                f"  {row.get('id', '?')}: {row.get('metric')}={row.get('value')} "
                f"target {row.get('target')} "
                f"({'within target' if row.get('pass') else 'outside target'})"
                for row in diagnostics[:6]
            )
        )
    return "".join(sections)


def _bound_static_frames(rows: list[dict], active_ids: set[str] | None, fallback_frame: int) -> list[int]:
    """Frames whose static contracts must be produced for the active evidence boundary."""
    if active_ids is None:
        return [int(fallback_frame)]

    frames: set[int] = set()
    for row in rows:
        if str(row.get("id")) not in active_ids or row.get("kind") in FUNCTIONAL_KINDS:
            continue
        declared = row.get("frames")
        if not isinstance(declared, (list, tuple)) or not declared:
            declared = [row.get("frame", fallback_frame)]
        frames.update(int(frame) for frame in declared)
    return sorted(frames or {int(fallback_frame)})


def _warn_suffix(r: dict) -> str:
    """Scene-state warnings from the worker, attached to the render they describe.

    These are conditions where the render looks entirely plausible while silently doing
    the opposite of what was asked — a sun inside a world volume being the one that cost
    five layer-2 attempts. There is nothing in the picture to prompt suspicion, so the
    warning has to travel with it.
    """
    w = r.get("warnings") or []
    return "".join(f"\n⚠ {x}" for x in w)


def _check_report(kind: str, r: dict) -> str:
    """A check's answer as text, with the ISSUES first.

    Deliberately not a JSON dump. The failure this class of check exists to catch is
    "the number was measured, written into a comment, and verified by nothing" — so
    the finding has to read as a finding, not as a payload to be re-derived.
    """
    issues = r.get("issues") or []
    if kind in {"visibility", "projection"}:
        head = "check visibility: " + ("OBSERVED ✅" if r.get("ok") else "ISSUES ✗")
        if kind == "projection":
            head = "check projection: " + ("OBSERVED ✅" if r.get("ok") else "ISSUES ✗")
    else:
        head = f"check {kind}: " + ("PASS ✅" if r.get("ok") else "ISSUES ✗")
    lines = [head]
    for i in issues:
        lines.append(f"  ✗ {i}")
    if kind == "visibility":
        lines.append(
            f"  canonical visible_fraction {r.get('visible_fraction')} "
            f"({r.get('visible_samples')} visible · {r.get('occluded_samples')} occluded "
            f"of {r.get('on_screen_samples')} on-screen surface samples; "
            f"{r.get('off_screen_samples')} off-screen)"
        )
        lines.append(
            "  no universal threshold applies here — use contract_result(id=...) for "
            "the active contract's authoritative PASS/FAIL."
        )
    elif kind == "framing":
        lines.append("  coordinates: [x0,y0,x1,y1], origin TOP-LEFT (x right, y down)")
        for fr in r.get("frames", []):
            lines.append(
                f"  f{fr.get('frame')}: bbox {fr.get('bbox')} · "
                f"w {fr.get('width')} h {fr.get('height')} · "
                f"centre {fr.get('centre')} · on-screen {fr.get('on_screen')}"
            )
    elif kind == "projection":
        lines.append(
            f"  f{r.get('frame')} through camera {r.get('camera')} · coordinates "
            "origin TOP-LEFT (x right, y down); off-frame values are preserved"
        )
        for point in r.get("points", []):
            lines.append(
                f"  world {point.get('world')} → screen {point.get('screen')} · "
                f"in-front {point.get('in_front')} · in-frustum {point.get('in_frustum')} "
                f"· clip-w {point.get('clip_w')}"
            )
        lines.append("  read-only projection probe — do not create marker meshes to measure points.")
    elif kind == "motion":
        holds = (
            f" · holds {r.get('leading_hold_segments', 0)} before/{r.get('trailing_hold_segments', 0)} after"
            if r.get("active_frame_span")
            else ""
        )
        span = r.get("peak_speed_span") or []
        peak = (
            f"f{span[0]}→f{span[1]}" if isinstance(span, list) and len(span) == 2 else f"f{r.get('peak_speed_frame')}"
        )
        lines.append(
            f"  max speed {r.get('max_speed')} u/f ({peak}) · "
            f"max |accel| {r.get('max_accel')} u/f^2 · "
            f"max |jerk| {r.get('max_jerk')} u/f^3 · "
            f"{'one unbroken move' if r.get('unbroken') else 'BROKEN move'}{holds}"
        )
    elif kind == "mesh":
        c = r.get("counts", {})
        lines.append(
            f"  {c.get('verts')} verts · {c.get('edges')} edges · {c.get('faces')} faces · {c.get('islands')} island(s)"
        )
        lines.append(
            f"  non-manifold total {c.get('nonmanifold_edges')} "
            f"(boundary {c.get('boundary_edges')} · branch {c.get('branch_edges')} "
            f"· wire {c.get('wire_edges')}) · loose {c.get('loose_verts')} · "
            f"degenerate {c.get('degenerate_faces')} · n-gons {c.get('ngons')} · "
            f"poles {c.get('poles')}"
        )
        if r.get("allow_boundary"):
            lines.append("  intentional open-shell boundaries allowed; branch/wire edges remain defects")
    elif kind == "scale":
        lines.append(f"  scale {r.get('scale')} · dimensions {r.get('dimensions')} · units {r.get('unit_system')}")
    elif kind == "passes":
        lines.append(
            f"  {r.get('channels')} channels · NaN {r.get('nan')} · "
            f"Inf {r.get('inf')} · negative {r.get('negative')} · "
            f"passes {r.get('passes_enabled')}"
        )
    elif kind in ("bbox", "subject_bbox"):
        lines.append(
            f"  bbox {r.get('bbox')} (0..1, origin TOP-LEFT; x right, y down) · "
            f"w {r.get('width')} h {r.get('height')} · centre {r.get('centre')}"
        )
        lines.append(
            "  hand this straight to render_pass(crop=…, res_pct=400) — an "
            "oracle crop measures far better than a guessed one."
        )
    if not issues and kind in ("framing", "motion", "mesh", "scale"):
        lines.append("  nothing to fix on this check — the numbers above are the record.")
    return "\n".join(lines)


# Metrics are computed at ONE fixed size for every comparison, whatever `scale` the
# render was made at. Previously both images were forced to height 512 AFTER the render
# had already been shrunk by `scale`, so the two travelled different resampling paths:
# the reference (always the full 1920x960 plate) was downscaled 0.53x and stayed sharp,
# while a render at the default scale=0.4 was 768x384 and got UPSCALED 1.33x. Detail and
# structure therefore read as systematically softer than they were, and the builder chased
# sharpness it already had. Worse, two calls at different scales were not comparable to
# each other, yet the builder varies scale freely between them — in one layer it compared
# the same frame at 0.6 and then at 1.0 while editing a node in between, making the
# reading uninterpretable. (This is the same artifact that produced a "detail 46% high"
# finding I had to retract; it was in the tool, not just the analysis.)
# 320, NOT 512, and the number matters. The smallest scale the builder uses (0.35 of a
# 1920x960 shot) renders 336px tall, so a 512 metric height would still UPSCALE the render
# while the full-res reference downscaled — the very asymmetry this is fixing, just moved.
# Measuring below every accepted render height means neither image is ever upscaled.
_METRIC_H = 320
# DISPLAY is a SEPARATE question from METRIC and the two must not be reconciled.
#
# _METRIC_H exists to stop resampling asymmetry: measure below every accepted render
# height so neither image is ever upscaled. Lower is SAFER there.
#
# _DISPLAY_H is what a vision model gets to look at, where lower is strictly worse.
# Claude tokenises in 28x28 px patches, so a feature must clear ~4 patches (112px) to be
# discriminable and is at chance below ~2 (56px). At 512px tall, this shot's hero tower
# facade piers land near 6px — 0.21 of one patch, five times below the floor. The render
# was already paid for at full resolution; shipping it shrunk is throwing away the only
# signal the judge has. 1024 is the height at which a 2:1 frame still passes Claude's
# high-resolution tier unresized (~2400 tokens, ~$0.012 a call).
#
# The reasoning for METRIC leaked into DISPLAY once already. They answer different
# questions; do not "unify" them.
_DISPLAY_H = 1024

# (frame, ref) -> the (mode, scale) it was last measured at, so a change is announced.
_LOOK_METRICS = {
    "hot_core",
    "halation",
    "halation_top",
    "halation_mid",
    "halation_bot",
    "points",
    "points_top",
    "points_mid",
    "points_bot",
    "chroma_spread",
}


_ROLE_MANIFEST = "RESULT = {o.name: str(o.get('bvfx_role') or '') for o in bpy.context.scene.objects}"


def _scope_offenders(manifest: dict, allowed: tuple[str, ...], baseline: set[str] | None = None) -> list[str]:
    """Objects whose semantic role falls outside the unit's declared authority.

    Reported on every call until fixed, never once-and-forgotten: builders create first
    and tag second, so suppressing a name after first sight hides an untagged object for
    the rest of the build — which is how an untagged curve reached canonical replay.

    ``baseline`` is the manifest at unit start (after priors replayed): prior layers'
    objects are THEIR authority, not this unit's violation. Without it every later unit
    was told to delete the previous layers' work (runs 20260825T022805Z/023xxx flagged
    layer 1's proxies against every layer-2 unit, advice that would have destroyed
    sealed geometry if followed)."""
    return [
        f"{name!r} role={str(role) or '<none>'}"
        for name, role in sorted(manifest.items())
        if name not in (baseline or set()) and not _role_in_scope(str(role), allowed)
    ]


def _role_in_scope(role: str, allowed: tuple[str, ...]) -> bool:
    """A namespace owns its dot-delimited descendants, never a similar sibling.

    Mirrors the canonical replay rule in `builder._scope_added_object_errors` so live
    feedback and the deterministic gate cannot disagree about what is in scope."""
    if not role:
        return False
    for pattern in allowed:
        if fnmatch.fnmatchcase(role, pattern):
            return True
        if not any(token in pattern for token in "*?[") and role.startswith(pattern + "."):
            return True
    return False


def _layer_feedback_policy(shot_dir: Path | None, layer_id: str | None) -> dict:
    """Derive comparison advice from the axes this layer can actually change."""

    axes: list[str] = []
    if shot_dir and layer_id and (shot_dir / "layers.json").is_file():
        try:
            for row in read_document(shot_dir / "layers.json"):
                if str(row.get("id")) == str(layer_id):
                    axes = [str(axis).lower() for axis in (row.get("owns") or [])]
                    break
        except (OSError, ValueError):
            axes = []

    groups = axis_feedback_groups([(axis, "") for axis in axes])
    return {
        "axes": axes,
        # Empty ownership is invalid in the strict contract and earns no speculative
        # look feedback. There is intentionally no legacy all-feedback fallback.
        "groups": sorted(groups),
        "look_actions": bool(groups),
    }


def preview_render_mode(look_actions: bool, requested: str | None, *, look_default: str) -> str:
    """Live preview default: Workbench when the unit owns no look, else the look default.

    Canonical EEVEE remains the sealed artifact. Run ``20260826T170413Z-ba2b4c`` created
    a temp sun to light an EEVEE verify on an executable-only camera unit. ``render_pass``
    already forces matcap when ``look_actions`` is false; ``render_frame`` and
    ``verify_change`` still defaulted to EEVEE/draft.
    """
    if requested:
        return str(requested)
    return look_default if look_actions else "solid"


def _comparison_lock_error(locks: dict, key: tuple, settings: tuple) -> str | None:
    """Lock one frame/crop's render settings for the duration of a build round."""
    previous = locks.get(key)
    if previous is None:
        locks[key] = settings
        return None
    if previous == settings:
        return None
    return (
        f"comparison settings are LOCKED for round {key[0]}: first call used "
        f"mode={previous[0]} scale={previous[1]} res_pct={previous[2]}; requested "
        f"mode={settings[0]} scale={settings[1]} res_pct={settings[2]}. Reuse the "
        f"first settings so before/after metrics remain comparable; settings may "
        f"change only after the next critic round begins"
    )


def _comparison_mode_scale(args: dict, base_lock: tuple | None, *, look_actions: bool = True) -> tuple[str, float]:
    """Resolve omitted values from the round lock and typed look authority.

    A crop inherits the already-locked full-frame mode.  The first comparison uses
    Workbench solid for a look-less unit and EEVEE for a look-owning unit, matching
    the other live preview tools (HIR-0131).  An explicit mode remains authoritative.
    """
    default_mode = preview_render_mode(look_actions, None, look_default="eevee")
    mode = args.get("mode", base_lock[0] if base_lock else default_mode)
    scale = float(args["scale"] if "scale" in args else (base_lock[1] if base_lock else 0.4))
    return mode, scale


def _object_or_role_error(args: dict, tool: str) -> str | None:
    """Builder tools address roles; display names are the fallback, never both."""
    has_object = bool(str(args.get("object") or "").strip())
    has_role = bool(str(args.get("role") or "").strip())
    if has_object and has_role:
        return f"{tool}: pass role= or object=, not both"
    if not has_object and not has_role:
        return f"{tool}: requires role= (semantic selector) or object= (display name)"
    return None


def _check_args_error(kind: str, args: dict) -> str | None:
    requirements = {
        "visibility": ("frame",),
        "framing": (),
        "motion": ("frames",),
        "mesh": (),
        "scale": (),
        "passes": ("frame",),
        "bbox": ("frame",),
        "projection": ("frame", "points"),
    }
    missing = [name for name in requirements[kind] if args.get(name) is None]
    if kind == "framing" and args.get("frame") is None and not args.get("frames"):
        missing.append("frame or frames")
    if missing:
        return f"check_scene(kind={kind!r}) requires " + ", ".join(dict.fromkeys(missing))
    if kind == "motion" and len(args.get("frames") or []) < 2:
        return "check_scene(kind='motion') requires frames with at least 2 entries"
    if kind == "visibility" and args.get("samples") is not None:
        return "check_scene(kind='visibility') uses the canonical registry sampling policy; omit samples"
    if kind == "projection":
        points = args.get("points")
        if not isinstance(points, list) or not points:
            return "check_scene(kind='projection') requires a non-empty points array"
        if any(
            not isinstance(point, list)
            or len(point) != 3
            or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in point)
            for point in points
        ):
            return "check_scene(kind='projection') points must each be exactly [x, y, z] numbers in world space"
    elif kind != "passes":
        selector = _object_or_role_error(args, f"check_scene(kind={kind!r})")
        if selector:
            return selector
    return None


def _pixel_contract_gate(
    shot_dir: Path,
    layer_id: str,
    *,
    frame: int,
    ref: str,
    render: str | Path,
    evidence_ids: set[str] | None = None,
    selected_authority=None,
) -> tuple[bool, list[dict]]:
    """Is a scene-contract-complete candidate judgeable enough for a critic?"""

    if evidence_ids is not None and not evidence_ids:
        return True, []
    rows = image_checks.layer_evidence(
        shot_dir,
        layer_id,
        frame=frame,
        ref=ref,
        render=render,
        selected_authority=selected_authority,
    )
    if evidence_ids is not None:
        rows = [row for row in rows if str(row.get("id")) in evidence_ids]
    if evidence_ids is not None:
        observed = {str(row.get("id")) for row in rows}
        missing_ids = sorted(set(evidence_ids) - observed)
        missing = [
            {
                "id": evidence_id,
                "metric": "bound_image_evidence",
                "value": None,
                "target": "produced by the active unit",
                "pass": False,
                "origin": "harness",
                "authoritative": True,
            }
            for evidence_id in missing_ids
        ]
        selected = [*rows, *missing]
        return bool(selected) and not missing and all(row.get("pass") for row in rows), selected
    authoritative = [row for row in rows if row.get("authoritative")]
    if authoritative:
        return all(row.get("pass") for row in authoritative), authoritative
    image = Image.open(render).convert("L")
    mean = sum(image.getdata()) / max(1, image.width * image.height)
    row = {
        "id": "harness-frame-judgeable",
        "metric": "frame_mean",
        "value": round(mean, 4),
        "target": ">= 4",
        "pass": mean >= 4,
        "origin": "harness",
        "authoritative": True,
    }
    return bool(row["pass"]), [row]


def _image_evidence_ids_at_frame(state: Mapping[str, object], frame: int) -> set[str] | None:
    """Exact image bindings owed at one plate, not the unit's cross-frame union."""
    debts = state.get("image_debts") or []
    if debts:
        return {
            str(row.get("id"))
            for row in debts
            if isinstance(row, Mapping) and row.get("id") and int(row.get("frame", -1)) == int(frame)
        }
    ids = state.get("active_image_evidence_ids")
    return set(ids) if ids is not None else None


def _unpaid_image_debt_note(state: dict) -> str:
    """Payment path for owed image-contract ids. Not a selector miss; not critic handoff."""
    unpaid = list(state.get("unpaid_image_debts") or [])
    if not unpaid:
        return ""
    listed = ", ".join(
        f"{row.get('id')} (f{row.get('frame')}, {row.get('property')}, {row.get('axis')})"
        for row in unpaid
        if isinstance(row, dict)
    )
    return (
        "\nIMAGE-CONTRACT DEBTS UNPAID: "
        + listed
        + ". Call propose_checks with those exact ids; frame, property kind, and axis "
        "must match. A role or control retag cannot produce them. compare_frame "
        "evaluates paid rows; unpaid debts are not critic handoff."
    )


def followup_after_scene_contracts_pass(state: dict) -> str:
    """What the builder may do after bound scene rows pass.

    Look ownership is not an image-contract set. Treating it as one told a
    look unit that 3/3 existence rows were a critic handoff and locked
    ``run_bpy`` before appearance iteration (HIR-0044). Unpaid image-contract
    debts are a compiled card, not a scene-selector miss (HIR-0048).
    """
    unpaid_note = _unpaid_image_debt_note(state)
    if unpaid_note:
        return "\nSCENE CONTRACTS PASS." + unpaid_note
    if state.get("look_unsettled"):
        return (
            "\nSCENE CONTRACTS PASS: executable existence/wiring is done. This unit "
            "still owns look and binds no image contract; run_bpy remains open. "
            "Use render_frame / compare_frame as observation, not as a mutation lock."
        )
    if state.get("image_evidence_required"):
        return (
            "\nSCENE CONTRACTS PASS: call one FULL-FRAME compare_frame now. "
            "This unit binds authoritative image evidence; a failed bound "
            "check reopens one repair. Further run_bpy edits are blocked "
            "until that comparison."
        )
    return (
        "\nCURRENT EXECUTABLE CONTRACTS PASS. This is read-back, not candidate freeze: "
        "finish every authored ticket and observe its effect, then end the build session "
        "to freeze the terminal candidate. A passing structural floor must not strand an "
        "intermediate or temporary edit. No beauty comparison is required."
    )


def followup_after_image_gate_pass(state: dict, gate_rows: list) -> str:
    """Empty image-contract rows are not a critic handoff (HIR-0044)."""
    if state.get("look_unsettled"):
        return (
            "\nLOOK ITERATION OPEN: no bound image contract closed this plate. "
            "A 0/0 image-contract pass is not critic handoff; mutation remains legal."
        )
    unpaid = _unpaid_image_debt_note(state)
    if unpaid:
        return unpaid
    if not gate_rows:
        return (
            "\nNO BOUND IMAGE CONTRACTS on this comparison. 0/0 pass is not critic handoff; it does not certify look."
        )
    return (
        "\nCRITIC HANDOFF READY: current-layer scene and image "
        "contracts pass. Do not mutate again without critic-backed evidence."
    )


CANNOT_EXPRESS_SCHEMA = {
    "type": "object",
    "properties": {
        "contract_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "failing or contradictory contract ids",
        },
        "reason": {
            "type": "string",
            "description": "why no in-scope edit can pass, with the measured floor if any",
        },
        "fault_owner_units": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "optional upstream unit ids whose sealed outcome causes the measured floor; "
                "choose only from unit_scope.fault_owner_options"
            ),
        },
    },
    "required": ["contract_ids", "reason"],
}

CANNOT_EXPRESS_DESCRIPTION = (
    "Record that the failing contracts cannot be satisfied inside this unit's "
    "mutation scope or without contradicting another sealed contract. This STOPS "
    "further repair attempts and records a typed plan defect (hypothesis_falsified). "
    "Use it when interpolation, a child object, extra mutation, or an unpaid "
    "image-contract debt cannot legally pass — not for a fix you have not measured. "
    "Name the bare contract ids (no check: prefix). Distinct from ask_supervisor, "
    "which does not block. When executable evidence pins the floor to a sealed upstream "
    "unit, include its id from unit_scope.fault_owner_options so replan invalidates the "
    "semantic owner rather than only retrying this unit."
)


def record_cannot_express(comparison_state: dict | None, args: dict) -> dict:
    """Write a typed in-scope abstention onto the session the repair loop reads."""

    ids = [normalize_evidence_id(item) for item in (args.get("contract_ids") or []) if str(item).strip()]
    reason = str(args.get("reason") or "").strip()
    if not ids or not reason:
        return _text(
            "cannot_express_in_scope requires non-empty contract_ids and reason",
            is_error=True,
        )
    if comparison_state is None:
        return _text(
            "cannot_express_in_scope is not bound in this session",
            is_error=True,
        )
    requested_owners = sorted({str(item).strip() for item in args.get("fault_owner_units") or [] if str(item).strip()})
    options = {
        str(row.get("id")): row
        for row in comparison_state.get("fault_owner_options") or []
        if isinstance(row, dict) and row.get("id")
    }
    unknown_owners = sorted(set(requested_owners) - set(options))
    if unknown_owners:
        return _text(
            "unknown fault_owner_units "
            + ", ".join(unknown_owners)
            + "; legal upstream options: "
            + (", ".join(sorted(options)) or "(none)"),
            is_error=True,
        )
    debts = debts_from_dicts(comparison_state.get("image_debts"))
    classification = classify_cannot_express(ids, debts)
    comparison_state["cannot_express"] = {
        "contract_ids": ids,
        "reason": reason,
        "classification": classification,
        "fault_owner_units": requested_owners,
    }
    return _text(
        "Recorded cannot_express_in_scope for "
        + ", ".join(ids)
        + f" ({classification}). Do not edit the script further. The harness will stop "
        "remaining repairs and publish a typed plan defect. The finding does not reopen "
        "state; publish reviewed replacement authority through its owning boundary. "
        f"Reason: {reason}"
    )
