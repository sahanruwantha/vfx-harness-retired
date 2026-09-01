"""Immutable canonical evaluator outcomes for claimed work-unit completion."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.authority_head_records import (
    AuthorityHeadRecordError,
    canonical_json_bytes,
    decode_canonical_json_object,
)
from vfx_harness.domain.run_ids import require_run_id
from vfx_harness.domain.unit_attempts import UnitAttemptClaim
from vfx_harness.domain.unit_evaluation_receipts import (
    UnitEvaluationReceipt,
)
from vfx_harness.domain.work_units import WorkUnit, canonical_unit_script_path
from vfx_harness.infrastructure.trusted_files import (
    TrustedFileBinding,
    TrustedFileError,
    TrustedFileNotFound,
    read_trusted_file,
    require_trusted_file_unchanged,
)
from vfx_harness.observability.prepared_publication import (
    FilePublicationConflict,
    PreparedFilePublication,
    commit_prepared_file,
    discard_prepared_file,
    prepare_file_update,
)
from vfx_harness.orchestration import plan_bundle_integrity, unit_state
from vfx_harness.orchestration.ledger import LedgerSaveConflict, ledger_lock
from vfx_harness.orchestration.unit_replay_inputs import (
    ExecutedReplayDependency as ExecutedReplayDependency,
)
from vfx_harness.orchestration.unit_replay_inputs import (
    ExecutedReplayInput,
    ReplayInputConflict,
    prepare_replay_inputs,
)
from vfx_harness.orchestration.unit_state_storage import now

_RECEIPT_DIRECTORY = Path("checkpoints/unit-evaluations")
_EVIDENCE_FAMILY_BY_SOURCE = {
    "interface_contract": "scene_contract",
    "scene_contract": "scene_contract",
    "image_contract": "image_contract",
    "semantic_diff": "semantic_diff",
    "qualification": "qualification",
    "human_decision": "human_decision",
}


class UnitEvaluationConflict(ValueError):
    """Canonical evaluator authority is absent, stale, or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class StoredUnitEvaluationReceipt:
    receipt: UnitEvaluationReceipt
    locator: str
    sha256: str


@dataclass(frozen=True, slots=True)
class PreparedUnitEvaluationReceipt:
    """Fully hashed and fsynced receipt awaiting a short authority commit."""

    shot: Path
    destination: Path
    receipt: UnitEvaluationReceipt
    locator: str
    sha256: str
    ledger_binding: TrustedFileBinding
    ledger_sha256: str
    source_identities: tuple[TrustedFileBinding, ...]
    publication: PreparedFilePublication | None
    existing_binding: TrustedFileBinding | None
    publication_authority: str


@dataclass(frozen=True, slots=True)
class PreparedUnitEvaluationCompletion:
    """Fully read evaluator inputs awaiting a short unit-state completion CAS."""

    stored: StoredUnitEvaluationReceipt
    script_sha256: str
    source_identities: tuple[TrustedFileBinding, ...]


def _receipt_path(folder: str | Path, claim: UnitAttemptClaim) -> Path:
    if not isinstance(claim, UnitAttemptClaim):
        raise UnitEvaluationConflict("unit evaluation requires an exact typed claim")
    try:
        require_run_id(claim.run_id, "unit evaluation claim run_id")
    except ValueError as exc:
        raise UnitEvaluationConflict(str(exc)) from exc
    shot = Path(folder).expanduser().absolute()
    return shot / "runs" / claim.run_id / _RECEIPT_DIRECTORY / f"{claim.claim_id}.json"


def evaluation_receipt_locator(claim: UnitAttemptClaim) -> str:
    """Return the sole shot-relative locator allowed for one claim's outcome."""

    try:
        require_run_id(claim.run_id, "unit evaluation claim run_id")
    except ValueError as exc:
        raise UnitEvaluationConflict(str(exc)) from exc
    return (
        Path("runs")
        / claim.run_id
        / _RECEIPT_DIRECTORY
        / f"{claim.claim_id}.json"
    ).as_posix()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_real_directory(path: Path, *, parents_from: Path) -> None:
    current = parents_from
    if current.is_symlink() or not current.is_dir():
        raise UnitEvaluationConflict(
            f"unit evaluation authority root must be a real directory: {current}"
        )
    for part in path.relative_to(parents_from).parts:
        current /= part
        if current.is_symlink():
            raise UnitEvaluationConflict(
                f"unit evaluation authority must not use symlink directories: {current}"
            )
        if current.exists() and not current.is_dir():
            raise UnitEvaluationConflict(
                f"unit evaluation authority directory is not a directory: {current}"
            )
        if not current.exists():
            current.mkdir()
            _fsync_directory(current.parent)


def _require_source_unchanged(binding: TrustedFileBinding) -> None:
    try:
        require_trusted_file_unchanged(binding, "unit evaluation causal input")
    except TrustedFileError as exc:
        raise UnitEvaluationConflict(
            "unit evaluation causal input changed before publication: "
            f"{binding.path} ({exc})"
        ) from exc


def _read_source_snapshot(shot: Path, path: Path, where: str):
    try:
        return plan_bundle_integrity.read_real_file_snapshot(shot, path, where)
    except plan_bundle_integrity.PlanPublicationError as exc:
        raise UnitEvaluationConflict(str(exc)) from exc


def _prepare_immutable_receipt(
    shot: Path,
    path: Path,
    raw: bytes,
    *,
    authority_binding: str,
) -> tuple[PreparedFilePublication | None, TrustedFileBinding | None]:
    """Prepare exact bytes through the shared descriptor-held publication CAS."""

    _ensure_real_directory(path.parent, parents_from=shot)
    try:
        existing = read_trusted_file(
            shot,
            path,
            "immutable unit evaluation receipt",
            require_nonempty=True,
        )
    except TrustedFileNotFound:
        existing = None
    except TrustedFileError as exc:
        raise UnitEvaluationConflict(str(exc)) from exc
    if existing is not None:
        if existing.payload != raw:
            raise UnitEvaluationConflict(
                f"immutable unit evaluation receipt conflicts with existing bytes: {path}"
            )
        return None, existing.binding

    def create_only(current: bytes | None) -> tuple[bytes, None]:
        if current is not None:
            raise FilePublicationConflict(
                f"immutable unit evaluation receipt appeared during preparation: {path}"
            )
        return raw, None

    try:
        prepared = prepare_file_update(
            shot,
            path,
            create_only,
            authority_binding=authority_binding,
        )
    except FilePublicationConflict as exc:
        raise UnitEvaluationConflict(str(exc)) from exc
    if prepared.publication is None:
        raise UnitEvaluationConflict(
            "immutable unit evaluation receipt preparation unexpectedly became a no-op"
        )
    return prepared.publication, None


def _canonical_rows(
    canonical_verdicts: Sequence[tuple[tuple[int, str], Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(canonical_verdicts):
        if (
            not isinstance(item, (tuple, list))
            or len(item) != 2
            or not isinstance(item[0], (tuple, list))
            or len(item[0]) != 2
            or not isinstance(item[1], Mapping)
        ):
            raise UnitEvaluationConflict(
                f"canonical evaluator verdict {index} has an invalid shape"
            )
        frame, ref = item[0]
        rows.append({"frame": frame, "ref": ref, "verdict": dict(item[1])})
    return rows


def _passing_evidence(
    unit: WorkUnit,
    layer_id: str,
    rows: Sequence[Mapping[str, Any]],
) -> tuple[tuple[str, str], ...]:
    observed: set[tuple[str, str]] = set()
    for row in rows:
        verdict = row.get("verdict")
        if not isinstance(verdict, Mapping) or verdict.get("pass") is not True:
            raise UnitEvaluationConflict(
                "unit evaluation cannot publish a non-passing canonical verdict"
            )
        evidence = verdict.get("evidence")
        if not isinstance(evidence, list):
            raise UnitEvaluationConflict(
                "unit evaluation canonical verdict is missing its evidence rows"
            )
        for item in evidence:
            if (
                not isinstance(item, Mapping)
                or not item.get("id")
                or item.get("pass") is not True
                or item.get("authoritative") is not True
            ):
                continue
            family = _EVIDENCE_FAMILY_BY_SOURCE.get(str(item.get("source") or ""))
            if family is not None:
                observed.add((family, str(item["id"])))
    required = {
        (binding.kind, binding.id)
        for claim in unit.evaluation.claims
        if claim.required
        for binding in claim.evidence
    }
    missing = sorted(required - observed)
    if missing:
        raise UnitEvaluationConflict(
            f"canonical evaluator did not produce passing required evidence: {missing}"
        )
    return tuple(sorted({*required, ("replay", f"{layer_id}.{unit.id}")}))


def _ledger_outcome(
    slot: Mapping[str, Any],
    *,
    final_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    rounds = slot.get("rounds")
    if not isinstance(rounds, list):
        raise UnitEvaluationConflict("unit evaluation ledger has no canonical rounds")
    canonical = [dict(row) for row in rounds if isinstance(row, Mapping) and row.get("kind") == "canonical"]
    if len(canonical) < len(final_rows):
        raise UnitEvaluationConflict(
            "unit evaluation ledger does not contain every final canonical verdict"
        )
    final_ledger = canonical[-len(final_rows):]
    for index, (stored, row) in enumerate(zip(final_ledger, final_rows, strict=True)):
        verdict = row["verdict"]
        expected_render = str(verdict.get("render") or "")
        if stored.get("pass") is not True or verdict.get("pass") is not True:
            raise UnitEvaluationConflict(
                f"unit evaluation cannot publish non-passing canonical verdict {index}"
            )
        if (
            stored.get("evidence") != verdict.get("evidence")
            or str(stored.get("render") or "") != expected_render
            or stored.get("decided_by", "critic")
            != verdict.get("decided_by", "critic")
            or bool(stored.get("judge_conflict"))
            or bool(stored.get("contract_gap"))
        ):
            raise UnitEvaluationConflict(
                f"unit evaluation ledger canonical round {index} does not match evaluator output"
            )
    return {
        "run_id": slot.get("run_id"),
        "attempt": slot.get("attempt"),
        "status": slot.get("status"),
        "script": slot.get("script"),
        "unit_hash": slot.get("unit_hash"),
        "artifact_unit_hash": slot.get("artifact_unit_hash"),
        "best": slot.get("best"),
        "canonical_rounds": final_ledger,
    }


def _claimed_milestone_slot(
    ledger_root: Mapping[str, Any],
    *,
    layer_id: str,
    unit: WorkUnit,
    claim: UnitAttemptClaim,
) -> tuple[str, dict[str, Any]]:
    """Select the sole durable layer/unit slot naming the exact build claim."""

    milestones = ledger_root.get("milestones")
    if not isinstance(milestones, Mapping):
        raise UnitEvaluationConflict(
            "unit evaluation durable ledger has no milestones object"
        )
    expected_script = canonical_unit_script_path(layer_id, unit.id)
    candidate_ids = tuple(dict.fromkeys((layer_id, f"{layer_id}@{unit.id}")))
    candidates: list[tuple[str, dict[str, Any]]] = []
    for milestone_id in candidate_ids:
        raw = milestones.get(milestone_id)
        if raw is None:
            continue
        if not isinstance(raw, Mapping):
            raise UnitEvaluationConflict(
                "unit evaluation durable milestone slot must be an object: "
                f"milestones[{milestone_id!r}]"
            )
        candidates.append((milestone_id, dict(raw)))

    expected_identity = {
        "run_id": claim.run_id,
        "attempt": claim.attempt_revision,
        "script": expected_script,
        "unit_hash": claim.unit_digest,
        "artifact_unit_hash": claim.unit_digest,
    }
    exact = [
        (milestone_id, slot)
        for milestone_id, slot in candidates
        if all(slot.get(field) == value for field, value in expected_identity.items())
    ]
    if len(exact) != 1:
        observed = {
            milestone_id: {
                field: slot.get(field) for field in expected_identity
            }
            for milestone_id, slot in candidates
        }
        raise UnitEvaluationConflict(
            "unit evaluation durable ledger must contain exactly one claimed milestone "
            f"slot for {layer_id}.{unit.id}; expected={expected_identity}; "
            f"observed={observed}"
        )
    return exact[0]


def _require_local_slot_matches_durable(
    ledger_slot: Mapping[str, Any],
    durable_slot: Mapping[str, Any],
    *,
    milestone_id: str,
) -> None:
    if not isinstance(ledger_slot, Mapping):
        raise UnitEvaluationConflict(
            "unit evaluation caller ledger slot must be an object"
        )
    try:
        local_bytes = canonical_json_bytes(ledger_slot)
        durable_bytes = canonical_json_bytes(durable_slot)
    except AuthorityHeadRecordError as exc:
        raise UnitEvaluationConflict(str(exc)) from exc
    if local_bytes != durable_bytes:
        raise UnitEvaluationConflict(
            "unit evaluation caller ledger slot does not match descriptor-read durable "
            f"milestone {milestone_id!r}; reload shot.json before publishing"
        )


def _verify_receipt_for_unit(
    receipt: UnitEvaluationReceipt,
    unit: WorkUnit,
    *,
    layer_id: str,
    claim: UnitAttemptClaim,
) -> None:
    if receipt.claim != claim:
        raise UnitEvaluationConflict("unit evaluation receipt belongs to another claim")
    expected_script = canonical_unit_script_path(str(layer_id), unit.id)
    if receipt.script_path != expected_script:
        raise UnitEvaluationConflict(
            "unit evaluation receipt names a non-canonical replay script"
        )
    if claim.layer_id != str(layer_id) or claim.unit_id != unit.id:
        raise UnitEvaluationConflict("unit evaluation claim belongs to another work unit")
    if claim.unit_digest != unit_state.unit_digest(unit):
        raise UnitEvaluationConflict("unit evaluation claim has a stale work-unit digest")
    expected_frames = {point.frame for point in unit.evaluation.judges}
    observed_frames = {int(row["frame"]) for row in receipt.canonical}
    if observed_frames != expected_frames:
        raise UnitEvaluationConflict(
            "unit evaluation canonical frames do not match the unit judge set; "
            f"expected={sorted(expected_frames)}; observed={sorted(observed_frames)}"
        )
    derived = _passing_evidence(unit, str(layer_id), receipt.canonical)
    if receipt.passed_evidence != derived:
        raise UnitEvaluationConflict(
            "unit evaluation passed evidence is not derived from its canonical verdicts"
        )
    ledger = receipt.ledger_outcome
    if (
        ledger.get("run_id") != claim.run_id
        or ledger.get("status") != "passed"
        or ledger.get("script") != expected_script
        or ledger.get("unit_hash") != claim.unit_digest
        or ledger.get("artifact_unit_hash") != claim.unit_digest
    ):
        raise UnitEvaluationConflict(
            "unit evaluation ledger outcome is stale for the exact claim and script"
        )
    _ledger_outcome(
        {"rounds": ledger.get("canonical_rounds")},
        final_rows=receipt.canonical,
    )


def prepare_unit_evaluation_receipt(
    folder: str | Path,
    layer_id: str,
    unit: WorkUnit,
    claim: UnitAttemptClaim,
    *,
    result: str,
    canonical_verdicts: Sequence[tuple[tuple[int, str], Mapping[str, Any]]],
    ledger_slot: Mapping[str, Any],
    replay_inputs: Sequence[ExecutedReplayInput],
    candidate_path: str | None = None,
) -> PreparedUnitEvaluationReceipt:
    """Hash causal bytes and fsync a same-parent receipt before taking state locks."""

    shot = Path(folder).expanduser().absolute()
    rows = _canonical_rows(canonical_verdicts)
    script_path = canonical_unit_script_path(str(layer_id), unit.id)
    script = shot / script_path
    script_snapshot = _read_source_snapshot(
        shot,
        script,
        f"work-unit {layer_id}.{unit.id} evaluated canonical replay script",
    )
    try:
        replay, replay_identities = prepare_replay_inputs(shot, replay_inputs)
    except ReplayInputConflict as exc:
        raise UnitEvaluationConflict(str(exc)) from exc
    if (
        replay[-1].script_path != script_path
        or replay[-1].script_sha256 != script_snapshot.sha256
    ):
        raise UnitEvaluationConflict(
            "unit evaluation replay closure must end with the exact canonical unit script"
        )
    candidate_sha256 = None
    candidate = None
    if candidate_path:
        candidate = shot / candidate_path
        candidate_snapshot = _read_source_snapshot(
            shot,
            candidate,
            f"work-unit {layer_id}.{unit.id} evaluated candidate",
        )
        candidate_sha256 = candidate_snapshot.sha256
    ledger_path = shot / "shot.json"
    ledger_snapshot = _read_source_snapshot(
        shot,
        ledger_path,
        "unit evaluation durable ledger",
    )
    try:
        ledger_root = plan_bundle_integrity.decode_json_object(
            ledger_snapshot.payload,
            "unit evaluation durable ledger",
        )
    except plan_bundle_integrity.PlanPublicationError as exc:
        raise UnitEvaluationConflict(str(exc)) from exc
    milestone_id, durable_slot = _claimed_milestone_slot(
        ledger_root,
        layer_id=str(layer_id),
        unit=unit,
        claim=claim,
    )
    _require_local_slot_matches_durable(
        ledger_slot,
        durable_slot,
        milestone_id=milestone_id,
    )
    ledger = _ledger_outcome(durable_slot, final_rows=rows)
    receipt = UnitEvaluationReceipt.mint(
        claim=claim,
        script_path=script_path,
        script_sha256=script_snapshot.sha256,
        replay_inputs=replay,
        result=result,
        canonical=rows,
        ledger_outcome=ledger,
        passed_evidence=_passing_evidence(unit, str(layer_id), rows),
        candidate_path=candidate_path,
        candidate_sha256=candidate_sha256,
        published_at=now(),
    )
    _verify_receipt_for_unit(receipt, unit, layer_id=str(layer_id), claim=claim)
    path = _receipt_path(shot, claim)
    raw = canonical_json_bytes(receipt.as_dict())
    publication_authority = f"unit-evaluation:{receipt.receipt_digest}"
    publication, existing_binding = _prepare_immutable_receipt(
        shot,
        path,
        raw,
        authority_binding=publication_authority,
    )
    identities = [*replay_identities, script_snapshot.binding]
    if candidate is not None:
        identities.append(candidate_snapshot.binding)
    return PreparedUnitEvaluationReceipt(
        shot=shot,
        destination=path,
        receipt=receipt,
        locator=evaluation_receipt_locator(claim),
        sha256=hashlib.sha256(raw).hexdigest(),
        ledger_binding=ledger_snapshot.binding,
        ledger_sha256=ledger_snapshot.sha256,
        source_identities=tuple(dict.fromkeys(identities)),
        publication=publication,
        existing_binding=existing_binding,
        publication_authority=publication_authority,
    )


def commit_unit_evaluation_receipt(
    prepared: PreparedUnitEvaluationReceipt,
) -> StoredUnitEvaluationReceipt:
    """Publish a prepared evaluator outcome after a caller wins its authority CAS."""

    if not isinstance(prepared, PreparedUnitEvaluationReceipt):
        raise UnitEvaluationConflict("unit evaluation commit requires a prepared receipt")
    if prepared.ledger_binding.path != prepared.shot / "shot.json":
        raise UnitEvaluationConflict(
            "prepared unit evaluation receipt is not bound to its durable shot.json"
        )
    try:
        with ledger_lock(
            prepared.ledger_binding.path,
            exclusive=False,
            blocking=False,
        ):
            _require_source_unchanged(prepared.ledger_binding)
            for identity in prepared.source_identities:
                _require_source_unchanged(identity)
            if prepared.existing_binding is not None:
                _require_source_unchanged(prepared.existing_binding)
            elif prepared.publication is not None:
                commit_prepared_file(
                    prepared.publication,
                    authority_binding=prepared.publication_authority,
                )
            else:
                raise UnitEvaluationConflict(
                    "unit evaluation preparation has neither existing nor staged authority"
                )
            return StoredUnitEvaluationReceipt(
                receipt=prepared.receipt,
                locator=prepared.locator,
                sha256=prepared.sha256,
            )
    except LedgerSaveConflict as exc:
        discard_prepared_file(prepared.publication)
        raise UnitEvaluationConflict(
            "unit evaluation durable ledger is busy; retry from current shot.json"
        ) from exc
    except FilePublicationConflict as exc:
        discard_prepared_file(prepared.publication)
        raise UnitEvaluationConflict(str(exc)) from exc
    except BaseException:
        discard_prepared_file(prepared.publication)
        raise


def finalize_committed_unit_evaluation_receipt(
    prepared: PreparedUnitEvaluationReceipt,
    committed: StoredUnitEvaluationReceipt,
) -> StoredUnitEvaluationReceipt:
    """Durably flush and read back a visible receipt after releasing attempt locks."""

    if not isinstance(prepared, PreparedUnitEvaluationReceipt) or not isinstance(
        committed,
        StoredUnitEvaluationReceipt,
    ):
        raise UnitEvaluationConflict(
            "unit evaluation finalization requires prepared and committed receipts"
        )
    if prepared.publication is None:
        _fsync_directory(prepared.destination.parent)
    observed = load_unit_evaluation_receipt(prepared.shot, prepared.receipt.claim)
    if observed != committed:
        raise UnitEvaluationConflict(
            "committed unit evaluation receipt changed before durable read-back"
        )
    return observed


def discard_prepared_unit_evaluation_receipt(
    prepared: PreparedUnitEvaluationReceipt,
) -> None:
    """Remove an uncommitted same-parent staging file."""

    if isinstance(prepared, PreparedUnitEvaluationReceipt):
        discard_prepared_file(prepared.publication)


def publish_unit_evaluation_receipt(
    folder: str | Path,
    layer_id: str,
    unit: WorkUnit,
    claim: UnitAttemptClaim,
    *,
    result: str,
    canonical_verdicts: Sequence[tuple[tuple[int, str], Mapping[str, Any]]],
    ledger_slot: Mapping[str, Any],
    replay_inputs: Sequence[ExecutedReplayInput],
    candidate_path: str | None = None,
) -> StoredUnitEvaluationReceipt:
    """Convenience transaction for non-concurrent callers and fixtures."""

    prepared = prepare_unit_evaluation_receipt(
        folder,
        layer_id,
        unit,
        claim,
        result=result,
        canonical_verdicts=canonical_verdicts,
        ledger_slot=ledger_slot,
        replay_inputs=replay_inputs,
        candidate_path=candidate_path,
    )
    try:
        committed = commit_unit_evaluation_receipt(prepared)
        return finalize_committed_unit_evaluation_receipt(prepared, committed)
    except BaseException:
        discard_prepared_unit_evaluation_receipt(prepared)
        raise


def load_unit_evaluation_receipt(
    folder: str | Path,
    claim: UnitAttemptClaim,
) -> StoredUnitEvaluationReceipt:
    """Read the one canonical immutable evaluator outcome for an exact claim."""

    shot = Path(folder).expanduser().absolute()
    path = _receipt_path(shot, claim)
    try:
        raw = plan_bundle_integrity.read_real_file(
            shot,
            path,
            f"work-unit evaluation receipt {claim.claim_id}",
        )
        value = decode_canonical_json_object(raw, f"work-unit evaluation receipt {path}")
    except (
        AuthorityHeadRecordError,
        plan_bundle_integrity.PlanPublicationError,
        OSError,
        ValueError,
    ) as exc:
        raise UnitEvaluationConflict(str(exc)) from exc
    try:
        receipt = UnitEvaluationReceipt.parse(value, f"work-unit evaluation receipt {path}")
    except ValueError as exc:
        raise UnitEvaluationConflict(str(exc)) from exc
    if receipt.claim != claim:
        raise UnitEvaluationConflict(
            f"work-unit evaluation receipt {path} belongs to another claim"
        )
    return StoredUnitEvaluationReceipt(
        receipt=receipt,
        locator=evaluation_receipt_locator(claim),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def require_unit_evaluation_receipt(
    stored: StoredUnitEvaluationReceipt,
    unit: WorkUnit,
    checkpoint: Mapping[str, Any],
    *,
    layer_id: str,
    claim: UnitAttemptClaim,
) -> UnitEvaluationReceipt:
    """Bind evaluator output to the exact script and frozen checkpoint being accepted."""

    if not isinstance(stored, StoredUnitEvaluationReceipt):
        raise UnitEvaluationConflict("unit completion requires a stored evaluation receipt")
    receipt = stored.receipt
    _verify_receipt_for_unit(receipt, unit, layer_id=str(layer_id), claim=claim)
    if checkpoint.get("script_hash") != receipt.script_sha256:
        raise UnitEvaluationConflict(
            "unit evaluation receipt does not identify the frozen replay script"
        )
    if receipt.candidate_sha256 is None:
        if checkpoint.get("candidate_hash") != "missing":
            raise UnitEvaluationConflict(
                "unit evaluation has no candidate bytes but the checkpoint claims a candidate"
            )
    elif checkpoint.get("candidate_hash") != receipt.candidate_sha256:
        raise UnitEvaluationConflict(
            "unit evaluation candidate bytes do not identify the frozen checkpoint"
        )
    return receipt


def prepare_unit_evaluation_completion(
    folder: str | Path,
    layer_id: str,
    unit: WorkUnit,
    claim: UnitAttemptClaim,
) -> PreparedUnitEvaluationCompletion:
    """Read and hash every evaluator input before acquiring unit-state locks."""

    shot = Path(folder).expanduser().absolute()
    script_path = shot / canonical_unit_script_path(str(layer_id), unit.id)
    evaluation_path = _receipt_path(shot, claim)
    script_snapshot = _read_source_snapshot(
        shot,
        script_path,
        f"work-unit {layer_id}.{unit.id} completion replay script",
    )
    evaluation_snapshot = _read_source_snapshot(
        shot,
        evaluation_path,
        f"work-unit {layer_id}.{unit.id} completion evaluation receipt",
    )
    identities = [script_snapshot.binding, evaluation_snapshot.binding]
    stored = load_unit_evaluation_receipt(shot, claim)
    if stored.sha256 != evaluation_snapshot.sha256:
        raise UnitEvaluationConflict(
            "unit completion evaluation receipt changed while it was prepared"
        )
    _verify_receipt_for_unit(stored.receipt, unit, layer_id=str(layer_id), claim=claim)
    for index, replay_input in enumerate(stored.receipt.replay_inputs):
        replay_snapshot = _read_source_snapshot(
            shot,
            shot / replay_input.script_path,
            (
                f"work-unit {layer_id}.{unit.id} completion replay input "
                f"{index}"
            ),
        )
        if replay_snapshot.sha256 != replay_input.script_sha256:
            raise UnitEvaluationConflict(
                "unit completion replay input differs from canonical evaluator input: "
                f"{replay_input.script_path}"
            )
        identities.append(replay_snapshot.binding)
        for dependency_index, dependency in enumerate(replay_input.dependencies):
            dependency_snapshot = _read_source_snapshot(
                shot,
                shot / dependency.path,
                (
                    f"work-unit {layer_id}.{unit.id} completion replay input "
                    f"{index} dependency {dependency_index}"
                ),
            )
            if dependency_snapshot.sha256 != dependency.sha256:
                raise UnitEvaluationConflict(
                    "unit completion replay dependency differs from canonical "
                    f"evaluator input: {dependency.path}"
                )
            identities.append(dependency_snapshot.binding)
    candidate_path = (
        None
        if stored.receipt.candidate_path is None
        else shot / stored.receipt.candidate_path
    )
    if candidate_path is not None:
        candidate_snapshot = _read_source_snapshot(
            shot,
            candidate_path,
            f"work-unit {layer_id}.{unit.id} evaluated candidate",
        )
        identities.append(candidate_snapshot.binding)
    script_sha256 = script_snapshot.sha256
    if script_sha256 != stored.receipt.script_sha256:
        raise UnitEvaluationConflict(
            "unit completion replay script differs from canonical evaluator input"
        )
    if (
        candidate_path is not None
        and candidate_snapshot.sha256 != stored.receipt.candidate_sha256
    ):
        raise UnitEvaluationConflict(
            "unit completion candidate differs from canonical evaluator input"
        )
    prepared = PreparedUnitEvaluationCompletion(
        stored=stored,
        script_sha256=script_sha256,
        source_identities=tuple(dict.fromkeys(identities)),
    )
    for identity in prepared.source_identities:
        _require_source_unchanged(identity)
    return prepared


def require_prepared_unit_evaluation_completion(
    prepared: PreparedUnitEvaluationCompletion,
    unit: WorkUnit,
    checkpoint: Mapping[str, Any],
    *,
    layer_id: str,
    claim: UnitAttemptClaim,
) -> UnitEvaluationReceipt:
    """Validate prepared identities and checkpoint under the caller's state CAS."""

    if not isinstance(prepared, PreparedUnitEvaluationCompletion):
        raise UnitEvaluationConflict(
            "unit completion requires prepared canonical evaluator authority"
    )
    for identity in prepared.source_identities:
        _require_source_unchanged(identity)
    if checkpoint.get("script_hash") != prepared.script_sha256:
        raise UnitEvaluationConflict(
            "unit completion replay script changed after checkpoint freeze"
        )
    return require_unit_evaluation_receipt(
        prepared.stored,
        unit,
        checkpoint,
        layer_id=str(layer_id),
        claim=claim,
    )
