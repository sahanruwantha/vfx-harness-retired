"""Read guards for immutable accepted work-unit completion receipts."""

from __future__ import annotations

import stat
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from vfx_harness.domain.authority_head_records import (
    AuthoritySelectionTokenProjection,
    parse_authority_selection_token,
)
from vfx_harness.domain.stop_envelope_primitives import canonical_digest, require_digest
from vfx_harness.domain.unit_completion_receipts import UnitCompletionReceipt
from vfx_harness.domain.work_units import WorkUnit, canonical_unit_script_path, validate_unit_dag
from vfx_harness.infrastructure.trusted_files import (
    TrustedFileBinding,
    TrustedFileError,
    require_trusted_file_unchanged,
)
from vfx_harness.orchestration import plan_bundle_integrity, unit_state
from vfx_harness.orchestration.authority_selection_heads import (
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
    authority_selection_lock,
    require_matching_authority_selection_token,
)
from vfx_harness.orchestration.unit_evaluation_receipts import (
    StoredUnitEvaluationReceipt,
    load_unit_evaluation_receipt,
    require_unit_evaluation_receipt,
)
from vfx_harness.orchestration.unit_state_lock import (
    STATE_DIR,
    unit_state_lock,
)


class UnitCompletionConflict(ValueError):
    """A passed-unit receipt no longer identifies current executable authority."""


@dataclass(frozen=True, slots=True)
class _PreparedCompletionSources:
    receipt: UnitCompletionReceipt
    evaluation: StoredUnitEvaluationReceipt
    identities: tuple[TrustedFileBinding, ...]


def _require_sources_unchanged(prepared: _PreparedCompletionSources) -> None:
    try:
        for identity in prepared.identities:
            require_trusted_file_unchanged(
                identity,
                "completed work-unit causal input",
            )
    except TrustedFileError as exc:
        raise UnitCompletionConflict(str(exc)) from exc


def _selection_projection(
    selection_token: AuthoritySelectionToken,
) -> AuthoritySelectionTokenProjection:
    if not isinstance(selection_token, AuthoritySelectionToken):
        raise UnitCompletionConflict(
            "work-unit completion requires an exact typed authority selection token"
        )
    return parse_authority_selection_token(
        selection_token.to_dict(),
        "work-unit completion authority selection token",
    )


def _prepare_completion_sources(
    folder: str | Path,
    receipt: UnitCompletionReceipt,
) -> _PreparedCompletionSources:
    """Hash external receipt inputs before acquiring selection or state locks."""

    receipt = UnitCompletionReceipt.parse(
        receipt.as_dict(),
        "prepared work-unit completion receipt",
    )
    shot = Path(folder).expanduser().absolute()
    script_path = shot / receipt.script_path
    evaluation_path = shot / receipt.evaluation_receipt_locator
    script_snapshot = plan_bundle_integrity.read_real_file_snapshot(
        shot,
        script_path,
        (
            "completed work-unit canonical replay script "
            f"{receipt.claim.layer_id}.{receipt.claim.unit_id}"
        ),
    )
    evaluation_snapshot = plan_bundle_integrity.read_real_file_snapshot(
        shot,
        evaluation_path,
        "completed work-unit evaluation receipt",
    )
    initial = {
        script_path: script_snapshot.binding,
        evaluation_path: evaluation_snapshot.binding,
    }
    stored = load_unit_evaluation_receipt(folder, receipt.claim)
    if (
        stored.locator != receipt.evaluation_receipt_locator
        or stored.sha256 != receipt.evaluation_receipt_sha256
        or stored.sha256 != evaluation_snapshot.sha256
        or stored.receipt.receipt_digest != receipt.evaluation_receipt_digest
    ):
        raise UnitCompletionConflict(
            "work-unit completion evaluator receipt identity changed"
        )
    for index, replay_input in enumerate(stored.receipt.replay_inputs):
        replay_path = shot / replay_input.script_path
        replay_snapshot = plan_bundle_integrity.read_real_file_snapshot(
            shot,
            replay_path,
            f"completed work-unit replay input {index}",
        )
        if replay_snapshot.sha256 != replay_input.script_sha256:
            raise UnitCompletionConflict(
                "completed work-unit replay input script changed after acceptance: "
                f"{replay_input.script_path}"
            )
        initial[replay_path] = replay_snapshot.binding
        for dependency_index, dependency in enumerate(replay_input.dependencies):
            dependency_path = shot / dependency.path
            dependency_snapshot = plan_bundle_integrity.read_real_file_snapshot(
                shot,
                dependency_path,
                f"completed work-unit replay input {index} dependency {dependency_index}",
            )
            if dependency_snapshot.sha256 != dependency.sha256:
                raise UnitCompletionConflict(
                    "completed work-unit replay dependency changed after acceptance: "
                    f"{dependency.path}"
                )
            initial[dependency_path] = dependency_snapshot.binding
    candidate_path = (
        None
        if stored.receipt.candidate_path is None
        else shot / stored.receipt.candidate_path
    )
    if candidate_path is not None:
        candidate_snapshot = plan_bundle_integrity.read_real_file_snapshot(
            shot,
            candidate_path,
            "completed work-unit evaluated candidate",
        )
        initial[candidate_path] = candidate_snapshot.binding
    if (
        script_snapshot.sha256 != receipt.script_hash
        or stored.receipt.script_sha256 != receipt.script_hash
    ):
        raise UnitCompletionConflict(
            "completed work-unit canonical replay script changed after acceptance"
        )
    if (
        candidate_path is not None
        and candidate_snapshot.sha256 != stored.receipt.candidate_sha256
    ):
        raise UnitCompletionConflict(
            "completed work-unit evaluated candidate bytes changed"
        )
    identities = tuple(initial.values())
    prepared = _PreparedCompletionSources(receipt, stored, identities)
    _require_sources_unchanged(prepared)
    return prepared


def require_completed_unit_receipt_in_state(
    value: Mapping,
    layer_id: str,
    unit_id: str,
    units: tuple[WorkUnit, ...],
    receipt: UnitCompletionReceipt,
    *,
    expected_plan_hash: str,
    selection_token: AuthoritySelectionToken,
) -> UnitCompletionReceipt:
    """Validate an exact receipt while the caller owns selection/state locks."""

    if not isinstance(receipt, UnitCompletionReceipt):
        raise UnitCompletionConflict("completed work-unit guard requires a typed receipt")
    validate_unit_dag(units, f"layer {layer_id} work units")
    unit_state.validate_current(dict(value), layer_id, units)
    expected_plan_hash = require_digest(
        expected_plan_hash,
        "completed work-unit expected_plan_hash",
    )
    if value.get("plan_hash") != expected_plan_hash:
        raise UnitCompletionConflict("completed work-unit plan identity changed")
    try:
        slot = value["units"][unit_id]
    except KeyError as exc:
        raise UnitCompletionConflict(f"unknown work unit {unit_id!r}") from exc
    if not isinstance(slot, Mapping) or slot.get("status") != "passed":
        raise UnitCompletionConflict(
            f"work unit {unit_id} is not an accepted executable checkpoint"
        )
    raw = slot.get("completion_receipt")
    current = UnitCompletionReceipt.parse(
        raw,
        f"work-unit state {layer_id}.{unit_id}.completion_receipt",
    )
    if current != receipt:
        raise UnitCompletionConflict(
            f"work-unit completion receipt changed; expected={current.receipt_digest}, "
            f"provided={receipt.receipt_digest}"
        )
    unit = next((candidate for candidate in units if candidate.id == unit_id), None)
    if unit is None:
        raise UnitCompletionConflict(f"unknown work unit {unit_id!r}")
    projection = _selection_projection(selection_token)
    if (
        current.claim.layer_id != str(layer_id)
        or current.claim.unit_id != unit_id
        or current.claim.unit_digest != unit_state.unit_digest(unit)
        or current.claim.plan_hash != expected_plan_hash
        or current.claim.selection_token != projection
    ):
        raise UnitCompletionConflict(
            "work-unit completion receipt belongs to another unit or authority selection"
        )
    expected_script_path = canonical_unit_script_path(str(layer_id), unit_id)
    if current.script_path != expected_script_path:
        raise UnitCompletionConflict(
            "work-unit completion receipt names a non-canonical replay script"
        )
    checkpoint = slot.get("checkpoint")
    if not isinstance(checkpoint, Mapping):
        raise UnitCompletionConflict("completed work-unit checkpoint is missing")
    if canonical_digest(dict(checkpoint)) != current.checkpoint_digest:
        raise UnitCompletionConflict("completed work-unit checkpoint changed")
    if checkpoint.get("script_hash") != current.script_hash:
        raise UnitCompletionConflict("completed work-unit script identity changed")
    return current


@contextmanager
def completed_unit_attempt_guard(
    folder: str | Path,
    layer_id: str,
    unit_id: str,
    units: tuple[WorkUnit, ...],
    receipt: UnitCompletionReceipt,
    *,
    expected_plan_hash: str,
    selection_token: AuthoritySelectionToken,
) -> Iterator[UnitCompletionReceipt]:
    """Hold selection-SH then state-SH around one receipt-owned publication."""

    _selection_projection(selection_token)
    prepared = _prepare_completion_sources(folder, receipt)
    with authority_selection_lock(folder, exclusive=False):
        observed = read_authority_selection_heads(folder).token
        require_matching_authority_selection_token(selection_token, observed)
        with unit_state_lock(folder, layer_id, exclusive=False):
            value = unit_state.load(folder, layer_id)
            if not value:
                raise UnitCompletionConflict("work-unit state is not initialized")
            current = require_completed_unit_receipt_in_state(
                value,
                layer_id,
                unit_id,
                units,
                receipt,
                expected_plan_hash=expected_plan_hash,
                selection_token=selection_token,
            )
            if current != prepared.receipt:
                raise UnitCompletionConflict(
                    "work-unit completion changed after source verification"
                )
            _require_sources_unchanged(prepared)
            checkpoint = value["units"][unit_id]["checkpoint"]
            unit = next(candidate for candidate in units if candidate.id == unit_id)
            require_unit_evaluation_receipt(
                prepared.evaluation,
                unit,
                checkpoint,
                layer_id=str(layer_id),
                claim=current.claim,
            )
            yield current


def _state_layer_ids(folder: str | Path) -> tuple[str, ...]:
    state_dir = Path(folder) / STATE_DIR
    if not state_dir.exists():
        return ()
    if not state_dir.is_dir() or state_dir.is_symlink():
        raise UnitCompletionConflict(
            "work-unit state directory must be a real directory"
        )
    layer_ids: list[str] = []
    for path in sorted(state_dir.iterdir(), key=lambda item: item.name):
        if not path.name.startswith("layer_") or path.suffix != ".json":
            continue
        observed = path.lstat()
        if not stat.S_ISREG(observed.st_mode):
            raise UnitCompletionConflict(
                f"work-unit state must be a real regular file: {path}"
            )
        layer_id = path.stem.removeprefix("layer_")
        if not layer_id:
            raise UnitCompletionConflict(f"invalid work-unit state filename: {path.name}")
        layer_ids.append(layer_id)
    return tuple(layer_ids)


@contextmanager
def current_completion_receipt_digests(
    folder: str | Path,
) -> Iterator[Mapping[tuple[str, str], str]]:
    """Optimistically expose live receipt digests without holding locks over callers.

    This intentionally does not acquire the selection lock. Callers that compare the map
    with selected authority own that separate verification.  Large receipt/script reads
    happen between two short all-layer state snapshots.  The second snapshot runs after
    the caller's resolution read and refuses a concurrent invalidation, so the caller
    never treats a stale receipt-bound row as current while retaining state locks over
    unrelated ledger I/O.
    """

    layer_ids = _state_layer_ids(folder)
    snapshots: dict[tuple[str, str], UnitCompletionReceipt] = {}
    with ExitStack() as stack:
        for layer_id in layer_ids:
            stack.enter_context(unit_state_lock(folder, layer_id, exclusive=False))
        for layer_id in layer_ids:
            value = unit_state.load(folder, layer_id)
            for unit_id, slot in value.get("units", {}).items():
                if not isinstance(slot, Mapping) or slot.get("completion_receipt") is None:
                    continue
                snapshots[(str(layer_id), str(unit_id))] = UnitCompletionReceipt.parse(
                    slot["completion_receipt"]
                )
    prepared = {
        key: _prepare_completion_sources(folder, receipt)
        for key, receipt in snapshots.items()
    }
    receipts: dict[tuple[str, str], str] = {}
    with ExitStack() as stack:
        for layer_id in layer_ids:
            stack.enter_context(unit_state_lock(folder, layer_id, exclusive=False))
        for key, verified in prepared.items():
            layer_id, unit_id = key
            value = unit_state.load(folder, layer_id)
            try:
                current = UnitCompletionReceipt.parse(
                    value["units"][unit_id]["completion_receipt"]
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise UnitCompletionConflict(
                    f"work-unit completion {layer_id}.{unit_id} changed during verification"
                ) from exc
            if current != verified.receipt:
                raise UnitCompletionConflict(
                    f"work-unit completion {layer_id}.{unit_id} changed during verification"
                )
            _require_sources_unchanged(verified)
            receipts[key] = current.receipt_digest
    yield receipts
    with ExitStack() as stack:
        for layer_id in layer_ids:
            stack.enter_context(unit_state_lock(folder, layer_id, exclusive=False))
        for key, verified in prepared.items():
            layer_id, unit_id = key
            value = unit_state.load(folder, layer_id)
            try:
                current = UnitCompletionReceipt.parse(
                    value["units"][unit_id]["completion_receipt"]
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise UnitCompletionConflict(
                    f"work-unit completion {layer_id}.{unit_id} changed during use"
                ) from exc
            if current != verified.receipt:
                raise UnitCompletionConflict(
                    f"work-unit completion {layer_id}.{unit_id} changed during use"
                )
            _require_sources_unchanged(verified)
