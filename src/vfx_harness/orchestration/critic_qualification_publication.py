"""Publish and reopen measured critic qualification; selection remains plan authority."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

from vfx_harness.domain import critic_qualification as records
from vfx_harness.domain.work_units.claims import Claim
from vfx_harness.evaluation import critic_calibration
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.plan_bundle_integrity import decode_json_object, is_digest, read_real_file

SUITE_SCHEMA = "vfx-harness.critic-calibration-suite/v1"
SET_SCHEMA = "vfx-harness.critic-calibration-set/v1"


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



def _measure_set(root: Path, reference: dict) -> tuple[dict, list[tuple[dict, dict, dict]]]:
    selection = _read(root, reference)
    _keys(selection, {"schema", "suite", "claim_id", "members"}, "critic calibration set")
    if selection["schema"] != SET_SCHEMA:
        raise ValueError("critic calibration requires an explicit profile-set request; migrate the single profile")
    for key in ("suite", "claim_id"):
        records.text(selection[key], f"critic calibration set.{key}")
    if not isinstance(selection["members"], list) or not selection["members"]:
        raise ValueError("critic calibration set requires nonempty measured members")
    members = []
    identities = set()
    for source in selection["members"]:
        records.reference(source, "critic calibration member")
        request, measured = _measure(root, source)
        if any(request[key] != selection[key] for key in ("suite", "claim_id")):
            raise ValueError("critic calibration member changes the selected suite or claim")
        identity = measured["native_invocation_sha256"]
        if identity in identities:
            raise ValueError("critic calibration set contains duplicate invocation profiles")
        identities.add(identity)
        members.append((source, request, measured))
    members.sort(key=lambda member: member[2]["native_invocation_sha256"])
    return selection, members


def _profile(request: dict, evaluation: dict, proof: dict) -> dict:
    profile = request["profile"]
    return {"judge_model": profile["configuration"]["model"], "prompt_sha256": profile["prompt_sha256"],
            "evidence_shape": profile["image_shape"],
            "native_invocation_sha256": evaluation["native_invocation_sha256"],
            "budgets": evaluation["budgets"], "metrics": evaluation["metrics"], "calibration_proof": proof}


def verify(root: Path, record: dict) -> None:
    """Reopen every member; a set cannot certify itself through stored flags or metrics."""
    records.validate_record(record)
    selection, members = _measure_set(root, record["selection"])
    if any(record[key] != selection[key] for key in ("suite", "claim_id")):
        raise ValueError("critic qualification set changes its selected suite or claim")
    if len(record["profiles"]) != len(members):
        raise ValueError("critic qualification set omits or invents a measured profile")
    for profile, (source, request, derived) in zip(record["profiles"], members, strict=True):
        proof = profile["calibration_proof"]
        if proof["request"] != source:
            raise ValueError("critic qualification member request differs from selected set")
        selected = _read(root, proof["evaluation"])
        if critic_calibration.fingerprint(selected) != critic_calibration.fingerprint(derived):
            raise ValueError("critic qualification evaluation differs from its measured sources")
        if critic_calibration.fingerprint(profile) != critic_calibration.fingerprint(_profile(request, derived, proof)):
            raise ValueError("critic qualification profile differs from measured results")
    _read(root, record["selection"])


def _verify_claim(root: Path, claim: Claim, record: dict) -> None:
    semantics = {key: value for key, value in asdict(claim).items() if key != "qualification"}
    if record["claim_id"] != claim.id:
        raise ValueError("critic qualification must measure the selected claim")
    if not any(row.kind == "qualification" and row.id == record["suite"] for row in claim.evidence):
        raise ValueError("selected claim does not bind the measured qualification suite")
    for profile in record["profiles"]:
        request = _read(root, profile["calibration_proof"]["request"])
        matching = [row for row in request["profile"]["scope"]["claims"] if row["id"] == claim.id]
        if (len(matching) != 1 or
                critic_calibration.fingerprint(matching[0]) != critic_calibration.fingerprint(semantics)):
            raise ValueError("measured qualification changes the selected claim's semantics or owner")


def read_selected(root: Path, claim: Claim, where: str) -> dict:
    """The filesystem boundary for a parsed claim's exact selected measured profile set."""
    q = claim.qualification
    _keys(q, {"suite", "artifact", "artifact_sha256"}, f"{where}.qualification")
    record = _read(root, {"path": q["artifact"], "sha256": q["artifact_sha256"]})
    verify(root, record)
    if record["suite"] != q["suite"]:
        raise ValueError(f"{where}.qualification suite differs from selected claim")
    _verify_claim(root, claim, record)
    return record


def bind_claim(root: Path, *, claim: Claim, artifact: dict, check_current: Callable[[], None]) -> Claim:
    """Bind an explicit measured set without publishing or changing claim semantics."""
    if (not isinstance(claim, Claim) or claim.qualification is not None or
            claim.authority != "qualified_qualitative_required" or claim.required is not True):
        raise ValueError("qualification binding requires an unqualified typed required qualitative claim")
    records.reference(artifact, "critic qualification artifact")
    selected = dict(artifact)
    check_current()
    record = _read(root, selected)
    verify(root, record)
    _verify_claim(root, claim, record)
    bound = replace(claim, qualification={"suite": record["suite"], "artifact": selected["path"],
                                         "artifact_sha256": selected["sha256"]})
    check_current()
    if artifact != selected:
        raise ValueError("qualification artifact selection changed; restart binding")
    read_selected(root, bound, "critic claim")
    check_current()
    if artifact != selected:
        raise ValueError("qualification artifact selection changed; restart binding")
    _read(root, selected)
    return bound


def publish(root: Path, *, request: dict, check_current: Callable[[], None]) -> dict:
    """Publish independently measured members; selected plan authority remains separate."""
    records.reference(request, "critic qualification selection")
    snapshot = dict(request)
    check_current()
    layout = run_artifacts.active(root)
    if layout is None:
        raise ValueError("critic qualification publication requires an active owning VFX run")

    def current():
        check_current()
        if request != snapshot:
            raise ValueError("critic qualification source selection changed; restart publication")
        active = run_artifacts.active(root)
        if active is None or active.root != layout.root:
            raise ValueError("critic qualification owning run changed; select the current publication owner")

    def reference(path):
        return {"path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(read_real_file(root, path, "critic qualification publication")).hexdigest()}

    current()
    selection, members = _measure_set(root, snapshot)
    invocation = f"qualification-{uuid4().hex}"
    profiles = []
    for index, (source, selected, measured) in enumerate(members):
        current()
        evaluation = layout.write_report(f"{invocation}-{index}-evaluation", measured)
        profiles.append(_profile(selected, measured, {"request": source, "evaluation": reference(evaluation)}))
    record = {"schema": records.SCHEMA, "suite": selection["suite"], "claim_id": selection["claim_id"],
              "selection": snapshot, "profiles": profiles}
    current()
    verify(root, record)
    result = reference(layout.write_report(invocation, record))
    current()
    verify(root, _read(root, result))
    current()
    return result
