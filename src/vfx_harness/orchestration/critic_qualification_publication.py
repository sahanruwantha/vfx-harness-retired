"""Publish and reopen measured critic qualification; selection remains plan authority."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from vfx_harness.evaluation import critic_calibration
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.plan_bundle_integrity import decode_json_object, is_digest, read_real_file

SUITE_SCHEMA = "vfx-harness.critic-calibration-suite/v1"


def _keys(value: object, keys: set[str], where: str) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{where} requires exactly {sorted(keys)}; select a complete calibration suite")


def _read(root: Path, reference: dict) -> dict:
    _keys(reference, {"path", "sha256"}, "critic qualification source")
    if not isinstance(reference["path"], str) or not is_digest(reference["sha256"]):
        raise ValueError("critic qualification source requires a path and SHA-256")
    payload = read_real_file(root, root / reference["path"], "critic qualification source")
    if hashlib.sha256(payload).hexdigest() != reference["sha256"]:
        raise ValueError("critic qualification source digest differs; reselect current authority")
    return decode_json_object(payload, "critic qualification source")


def _measure(root: Path, reference: dict) -> tuple[dict, dict]:
    request = _read(root, reference)
    _keys(request, {"schema", "suite", "claim_id", "profile", "budgets", "cases"}, "critic calibration suite")
    if request["schema"] != SUITE_SCHEMA or not isinstance(request["cases"], list):
        raise ValueError("critic calibration suite schema/cases are unsupported")
    cases = []
    for case in request["cases"]:
        _keys(case, {"id", "control", "expected", "trials", "baseline"}, "critic calibration case")
        if not isinstance(case["trials"], list):
            raise ValueError("critic calibration trials must be an array")
        for trial in case["trials"]:
            _keys(trial, {"run_root", "report", "report_sha256", "journal_records_sha256"}, "calibration trial")
        cases.append(critic_calibration.Case(
            case["id"], case["control"], case["expected"],
            tuple(critic_calibration.Trial(**trial) for trial in case["trials"]), case["baseline"],
        ))
    evaluation = critic_calibration.evaluate(
        root, suite=request["suite"], claim_id=request["claim_id"],
        invocation=critic_calibration.fingerprint(request["profile"]), cases=tuple(cases), budgets=request["budgets"],
    )
    if evaluation["within_budgets"] is not True:
        raise ValueError("critic calibration failed measured budgets or control checks; requalify the judge")
    return request, evaluation


def _artifact(request: dict, evaluation: dict, proof: dict) -> dict:
    profile = request["profile"]
    return {"schema": 1, "suite": request["suite"], "claim_id": request["claim_id"],
            "judge_model": profile["configuration"]["model"], "prompt": profile["prompt_sha256"],
            "evidence_shape": profile["image_shape"],
            "native_invocation_sha256": evaluation["native_invocation_sha256"],
            "passed": True, "budgets": evaluation["budgets"], "metrics": evaluation["metrics"],
            "calibration_proof": proof}


def verify(root: Path, record: dict) -> None:
    """Re-derive admission evidence; an embedded passed flag never proves measured qualification."""
    proof = record.get("calibration_proof")
    _keys(proof, {"request", "evaluation"}, "native qualification calibration_proof")
    request, derived = _measure(root, proof["request"])
    selected = _read(root, proof["evaluation"])
    if critic_calibration.fingerprint(selected) != critic_calibration.fingerprint(derived):
        raise ValueError("critic qualification evaluation differs from its sources; rerun the measured suite")
    if critic_calibration.fingerprint(record) != critic_calibration.fingerprint(_artifact(request, derived, proof)):
        raise ValueError("critic qualification artifact differs from measured results; republish qualification")
    _read(root, proof["request"])


def publish(root: Path, *, request: dict, check_current: Callable[[], None]) -> dict:
    """Publish a candidate credential in the owning run, without editing selected plans or claims.

    The owning caller derives the selected request and authority check. This function
    measures that request; it does not invent review approval for its labels or budgets.
    """
    _keys(request, {"path", "sha256"}, "critic qualification source")
    request_snapshot = dict(request)
    check_current()
    layout = run_artifacts.active(root)
    if layout is None:
        raise ValueError("critic qualification publication requires an active owning VFX run")

    def current():
        check_current()
        if request != request_snapshot:
            raise ValueError("critic qualification source selection changed; restart publication")
        active = run_artifacts.active(root)
        if active is None or active.root != layout.root:
            raise ValueError("critic qualification owning run changed; select the current publication owner")

    current()
    selected, measured = _measure(root, request_snapshot)
    current()
    invocation = f"qualification-{uuid4().hex}"
    evaluation_path = layout.write_report(f"{invocation}-evaluation", measured)

    def reference(path):
        return {"path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(read_real_file(root, path, "critic qualification publication")).hexdigest()}

    proof = {"request": request_snapshot, "evaluation": reference(evaluation_path)}
    record = _artifact(selected, measured, proof)
    current()
    verify(root, record)
    artifact_path = layout.write_report(invocation, record)
    result = reference(artifact_path)
    current()
    verify(root, _read(root, result))
    current()
    return result
