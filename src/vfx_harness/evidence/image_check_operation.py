"""VFX runtime image-payment operation, independent of model transport."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

from vfx_harness.domain.image_debts import (
    debts_from_dicts,
    normalize_evidence_id,
    reject_proposed_image_check,
    unpaid_image_contract_debts,
)
from vfx_harness.domain.work_units import read_document
from vfx_harness.evidence import runtime_check_publication
from vfx_harness.evidence.checks import (
    IMAGE_PAYMENT_SCHEMA,
    METRICS,
    Check,
    load_image_contract_payment_rows,
    verify_necessity,
)
from vfx_harness.evidence.image_payment_inputs import (
    _candidate_for_proposed_check,
    _refresh_unpaid_image_debts,
    _sha256_file,
)
from vfx_harness.orchestration.plan_authority import selected_artifact_path

DESCRIPTION = (
    "Record what you learned about VERIFYING this layer, as executable checks. You are the only"
    " stage with the built scene. Runtime evidence is evaluation-only and is not execution "
    "authority; do not read runtime_checks.json. Propose only an evidence gap you actually "
    "discovered.\nEach check must PASS on your render and FAIL on the state before your unit "
    "ran. The harness captures that adversary before the unit starts; you cannot select or "
    "manufacture it.\nafter_handle is the IMAGE EVIDENCE HANDLE returned by render_frame or an "
    "uncropped compare_frame at the owed frame. Raw paths are intentionally not accepted. For a"
    " multi-frame batch, put after_handle on each check; a batch-level after_handle is "
    "shorthand only when every check uses the same frame. Use the harness-selected medium and scale so "
    "it is settings-identical to the harness adversary. Survivors are appended to the "
    "runtime_checks.json evidence ledger; planner contracts remain immutable in checks.json. "
    "Propose few and real. When the active unit owes image-contract debts, each kept row must "
    "use an owed id with matching frame, property kind, and axis; a different id while debts "
    "remain is rejected naming requested vs owed."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "checks": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "metric": {"type": "string", "enum": sorted(METRICS)},
                    "op": {"type": "string", "enum": [">=", "<=", "band"]},
                    "lo": {"type": "number"},
                    "hi": {"type": "number"},
                    "frame": {"type": "integer"},
                    "axis": {"type": "string"},
                    "stage": {"type": "string", "enum": ["pre_grade", "post_grade", "any"]},
                    "ref": {"type": "string"},
                    "regions": {
                        "type": "object",
                        "description": (
                            "Literal metric operands: every "
                            "region_mean/min/p5/max/sigma/lit_pct/green_excess/lit_variance check requires exactly key "
                            "'r', e.g. {'r':[x0,y0,x1,y1]}; region_ratio requires keys 'a' and 'b'. Labels such as "
                            "'target', 'region', or a subject name are not operands."
                        ),
                        "additionalProperties": {
                            "type": "array",
                            "minItems": 4,
                            "maxItems": 4,
                            "items": {"type": "number"},
                        },
                    },
                    "note": {"type": "string"},
                    "after_handle": {
                        "type": "string",
                        "description": (
                            "Frame-local current-run immutable candidate handle; overrides the batch-level shorthand"
                        ),
                    },
                },
                "required": ["id", "metric", "op"],
                "additionalProperties": False,
            },
        },
        "after_handle": {
            "type": "string",
            "description": "Current-run immutable handle returned by render_frame/compare_frame",
        },
    },
    "required": ["checks"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ImageCheckResult:
    message: str
    is_error: bool = False
    kept_ids: tuple[str, ...] = ()
    unpaid_ids: tuple[str, ...] = ()


def validate_arguments(arguments: dict) -> None:
    errors = sorted(Draft202012Validator(SCHEMA).iter_errors(arguments), key=lambda e: e.json_path)
    if errors:
        raise ValueError("propose_checks: " + "; ".join(f"{e.json_path}: {e.message}" for e in errors))


def propose_checks(args, *, shot_dir, layer_id, comparison_state, selected_authority, prepare_and_publish):
    """Measure immutable candidate/adversary pairs; publish through the caller's VFX transaction."""
    validate_arguments(args)
    if not shot_dir:
        return ImageCheckResult("propose_checks needs a shot dir", is_error=True)
    root = Path(shot_dir)
    registry = comparison_state.get("image_artifacts") or {}
    # Every check must name the plate it is about, or the gate cannot re-run it. The
    # first version of this tool took `after`/`before` renders and never populated
    # `ref`, so four good builder checks landed in the runtime evidence ledger and all
    # four failed validation on plumbing rather than on merit.
    judge: dict[int, str] = {}
    first_ref = ""
    try:
        layers_path = (
            selected_artifact_path(root, "layers.json")
            if selected_authority is None
            else (
                root / "layers.json"
                if selected_authority.plan is None
                else selected_authority.artifact_paths["layers.json"]
            )
        )
        for lay in read_document(layers_path):
            if str(lay.get("id")) != str(layer_id):
                continue
            js = lay.get("judge") or []
            judge = {int(j["frame"]): j["ref"] for j in js if j.get("ref")}
            primary = lay.get("primary_judge")
            first_ref = next((j.get("ref", "") for j in js if j.get("frame") == primary), "")
    except (OSError, ValueError, KeyError, TypeError) as e:
        return ImageCheckResult(
            f"could not read judge refs from selected layers.json: {e!s}",
            is_error=True,
        )
    kept, lines = [], []
    debts = debts_from_dicts(comparison_state.get("image_debts"))
    unpaid = (
        unpaid_image_contract_debts(
            debts,
            load_image_contract_payment_rows(
                root,
                selected_authority=selected_authority,
            ),
        )
        if debts
        else ()
    )
    for d in args["checks"]:
        cid = normalize_evidence_id(d.get("id", "?"))
        debt_reject = reject_proposed_image_check(
            d,
            debts,
            unpaid=unpaid,
            registry=METRICS,
        )
        if debt_reject:
            lines.append(f"  REJECTED {cid:10} {debt_reject}")
            continue
        d = {**d, "id": cid}
        after_handle, after_record, handle_error = _candidate_for_proposed_check(d, args.get("after_handle"), registry)
        d.pop("after_handle", None)
        if handle_error or not isinstance(after_record, dict):
            lines.append(f"  REJECTED {cid:10} {handle_error}")
            continue
        after = root / str(after_record["path"])
        if not after.is_file() or _sha256_file(after) != after_record.get("sha256"):
            lines.append(
                f"  REJECTED {cid:10} candidate handle {after_handle!r} no longer matches its immutable artifact"
            )
            continue
        try:
            check_frame = int(d["frame"])
        except (KeyError, TypeError, ValueError):
            lines.append(f"  REJECTED {cid:10} frame is required for a runtime image payment")
            continue
        adversary_record = (comparison_state.get("image_adversaries") or {}).get(check_frame)
        if not isinstance(adversary_record, dict):
            lines.append(f"  REJECTED {cid:10} harness captured no pre-unit adversary at f{check_frame}")
            continue
        before = root / str(adversary_record["path"])
        if not before.is_file() or _sha256_file(before) != adversary_record.get("sha256"):
            lines.append(f"  REJECTED {cid:10} pre-unit adversary artifact is missing or changed")
            continue
        if int(after_record.get("frame", -1)) != check_frame:
            lines.append(
                f"  REJECTED {cid:10} candidate handle is f{after_record.get('frame')}, but this debt is f{check_frame}"
            )
            continue
        settings = ("mode", "scale", "resolution")
        mismatch = [key for key in settings if after_record.get(key) != adversary_record.get(key)]
        if mismatch:
            lines.append(
                f"  REJECTED {cid:10} candidate/adversary settings differ in "
                + ", ".join(mismatch)
                + "; recapture with the harness adversary's mode, scale and resolution"
            )
            continue
        try:
            ref_rel = (
                d.get("ref") or judge.get(int(d["frame"])) if d.get("frame") is not None else d.get("ref")
            ) or first_ref
            d = {**d, "ref": ref_rel}
            c = Check.from_dict(
                {
                    **d,
                    "layer": str(layer_id or d.get("layer", "")),
                    "lo": d.get("lo", float("-inf")),
                    "hi": d.get("hi", float("inf")),
                }
            )
            if not ref_rel:
                lines.append(f"  REJECTED {cid:10} no judge frame to name as its ref")
                continue
            v = verify_necessity(c, after, before)
        except (OSError, ValueError, KeyError, TypeError) as e:
            lines.append(f"  REJECTED {cid:10} {e!s}")
            continue
        if v.ok:
            # Free-form builder notes are never persisted as future execution
            # authority. Keep only executable fields plus harness-generated proof.
            safe = {k: value for k, value in d.items() if k != "note"}
            kept.append(
                {
                    **safe,
                    "layer": str(layer_id or d.get("layer", "")),
                    # Record the render this was proven against. A later attempt
                    # replaces the renders, and a proof that does not say which
                    # picture it came from cannot be told apart from a wrong one.
                    "proof": {
                        "ref": round(v.ref_value, 4),
                        "adversary": [round(x, 4) for x in v.bad_values[:1]],
                        "on": after_record["path"],
                    },
                    "payment": {
                        "schema": IMAGE_PAYMENT_SCHEMA,
                        "run_id": after_record["run_id"],
                        "unit_id": str(comparison_state.get("unit_id") or ""),
                        "unit_hash": str(comparison_state.get("unit_hash") or ""),
                        "parent_chain_hash": str(comparison_state.get("parent_chain_hash") or ""),
                        "candidate": {
                            key: after_record[key] for key in ("path", "sha256", "frame", "mode", "scale", "resolution")
                        },
                        "adversary": {
                            key: adversary_record[key]
                            for key in (
                                "path",
                                "sha256",
                                "frame",
                                "mode",
                                "scale",
                                "resolution",
                                "parent_chain_hash",
                            )
                        },
                    },
                    "origin": "builder",
                    # No prior layer means no adversary — the FIRST layer's checks
                    # are the least verified in the system, and saying so is the
                    # point. Silence here would let them count as adversaried.
                    **({"note": "[no prior-layer adversary: first layer]"} if not v.bad_values else {}),
                }
            )
            lines.append(
                f"  KEPT     {cid:10} {c.metric} {c.target()} · after "
                f"{v.ref_value:.4g}" + (f" · before {v.bad_values[0]:.4g}" if v.bad_values else "")
            )
        else:
            # Whole, and every reason. A rejection whose purpose is to teach the
            # legal threshold window was cut at 120 characters -- before the window
            # began -- and reduced to reasons[0], so a verdict carrying FRAGILE and
            # NOT NECESSARY showed one. The builder then re-proposed against advice
            # it had never been given (HIR-0244).
            lines.append(f"  REJECTED {cid:10} {v.why()}")
    if kept:
        prepare_and_publish(
            f"publish layer {layer_id} runtime image checks",
            lambda binding: runtime_check_publication.prepare_runtime_check_update(
                root,
                kept,
                authority_binding=binding,
            ),
        )
    _refresh_unpaid_image_debts(
        comparison_state,
        root,
        selected_authority=selected_authority,
    )
    remaining = comparison_state.get("unpaid_image_debts") or []
    tail = ""
    if remaining:
        tail = (
            "\nStill unpaid: "
            + ", ".join(str(row.get("id")) for row in remaining)
            + ". Candidate freeze will refuse until these ids are paid or "
            "cannot_express_in_scope records unpaid_image_debt."
        )
    return ImageCheckResult(
        f"{len(kept)} check(s) added to runtime_checks.json.\n" + "\n".join(lines) + tail,
        kept_ids=tuple(row["id"] for row in kept),
        unpaid_ids=tuple(str(row["id"]) for row in remaining),
    )
