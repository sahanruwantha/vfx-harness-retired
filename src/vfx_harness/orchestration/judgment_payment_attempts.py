"""Append-only, authority-bound qualitative-payment attempt failures (HIR-0163)."""

from __future__ import annotations

import fcntl
import json
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.judgment_debts import (
    JudgmentDebtActivation,
    JudgmentDebtDefinition,
    JudgmentDebtState,
    JudgmentObservationRequest,
    JudgmentPaymentAttemptFailure,
)
from vfx_harness.observability.provenance import atomic_write
from vfx_harness.observability.run_artifacts import shot_state_dir
from vfx_harness.orchestration import judgment_debt_state
from vfx_harness.orchestration.authority_selection import (
    SelectedAuthorityResolutionError,
    resolve_selected_authority,
)
from vfx_harness.orchestration.authority_selection_heads import (
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
    authority_selection_lock,
    require_matching_authority_selection_token,
)

EVENT_SCHEMA = "vfx-harness.judgment-payment-attempt-event/v1"
EVENTS = "judgment-payment-attempts.jsonl"


@dataclass(frozen=True, slots=True)
class _PaymentAttemptEvent:
    bundle_digest: str
    debt_id: str
    definition_digest: str
    activation_digest: str
    request: JudgmentObservationRequest
    failure: JudgmentPaymentAttemptFailure

    def __post_init__(self) -> None:
        if not isinstance(self.debt_id, str) or not self.debt_id.strip():
            raise ValueError("payment-attempt event debt_id must be a non-empty string")
        if self.bundle_digest != self.request.bundle_digest:
            raise ValueError("payment-attempt event bundle_digest does not match its request")
        if self.definition_digest != self.request.definition_digest:
            raise ValueError("payment-attempt event definition_digest does not match its request")
        if self.activation_digest != self.request.activation_digest:
            raise ValueError("payment-attempt event activation_digest does not match its request")
        self.failure.assert_matches_request(self.request)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": EVENT_SCHEMA,
            "bundle_digest": self.bundle_digest,
            "debt_id": self.debt_id,
            "definition_digest": self.definition_digest,
            "activation_digest": self.activation_digest,
            "request": self.request.as_dict(),
            "failure": self.failure.as_dict(),
        }


@contextmanager
def _event_lock(path: Path):
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _event_rows(path: Path) -> tuple[_PaymentAttemptEvent, ...]:
    if not path.is_file():
        return ()
    rows: list[_PaymentAttemptEvent] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        where = f"state/{EVENTS}:{line_no}"
        if not line.strip():
            raise ValueError(f"{where} must not be blank")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{where} is invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{where} must contain an object")
        expected = {
            "schema",
            "bundle_digest",
            "debt_id",
            "definition_digest",
            "activation_digest",
            "request",
            "failure",
        }
        if set(value) != expected or value.get("schema") != EVENT_SCHEMA:
            raise ValueError(f"{where} has unsupported judgment-payment-attempt event shape")
        try:
            rows.append(
                _PaymentAttemptEvent(
                    bundle_digest=value["bundle_digest"],
                    debt_id=value["debt_id"],
                    definition_digest=value["definition_digest"],
                    activation_digest=value["activation_digest"],
                    request=JudgmentObservationRequest.from_dict(
                        value["request"], f"{where}.request"
                    ),
                    failure=JudgmentPaymentAttemptFailure.from_dict(
                        value["failure"], f"{where}.failure"
                    ),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{where} has invalid judgment-payment-attempt authority: {exc}") from exc
    return tuple(rows)


def _current_due_request(
    shot_folder: str | Path,
    request: JudgmentObservationRequest,
) -> tuple[
    str,
    JudgmentDebtDefinition,
    JudgmentDebtActivation,
    tuple[tuple[JudgmentDebtDefinition, JudgmentDebtActivation | None, JudgmentDebtState], ...],
    AuthoritySelectionToken,
]:
    if not isinstance(request, JudgmentObservationRequest):
        raise ValueError("request must be a JudgmentObservationRequest")
    shot = Path(shot_folder)
    try:
        selected = resolve_selected_authority(shot)
    except SelectedAuthorityResolutionError as exc:
        raise ValueError(str(exc)) from exc
    bundle = selected.assertion.bundle
    if selected.assertion.selection != "selected" or bundle is None:
        raise ValueError("payment-attempt request requires selected plan authority")
    states = judgment_debt_state.current_judgment_debt_states_for_authority(
        shot,
        selected,
    )
    if request.bundle_digest != bundle.digest:
        raise ValueError("JudgmentObservationRequest.bundle_digest is not the selected bundle")
    try:
        definition, activation, state = next(
            row for row in states if row[0].digest == request.definition_digest
        )
    except StopIteration as exc:
        raise ValueError(
            f"JudgmentObservationRequest.definition_digest {request.definition_digest} is not current"
        ) from exc
    if activation is None:
        raise ValueError(f"judgment debt {definition.debt_id} has no selected payer activation")
    activation.assert_matches(definition)
    request.assert_matches(definition, activation)
    if state.status != "due":
        raise ValueError(
            f"judgment debt {definition.debt_id} payment attempt requires due state, not {state.status}"
        )
    if request.payment_generation_digest != state.payment_generation_digest:
        raise ValueError(
            f"judgment debt {definition.debt_id} payment attempt belongs to another "
            "replay payment generation"
        )
    return bundle.digest, definition, activation, states, selected.selection_token


def _failure_for_current_request(
    events: Iterable[_PaymentAttemptEvent],
    *,
    bundle_digest: str,
    definition: JudgmentDebtDefinition,
    activation: JudgmentDebtActivation,
    states: tuple[tuple[JudgmentDebtDefinition, JudgmentDebtActivation | None, JudgmentDebtState], ...],
    request: JudgmentObservationRequest,
) -> JudgmentPaymentAttemptFailure | None:
    current = {row[0].digest: row for row in states}
    for event in events:
        if event.bundle_digest != bundle_digest:
            # Historical selected generations are immutable audit data, never a retry key.
            continue
        current_row = current.get(event.definition_digest)
        if current_row is None:
            raise ValueError(
                "payment-attempt event names a definition absent from the selected bundle"
            )
        current_definition, current_activation, _state = current_row
        if event.debt_id != current_definition.debt_id:
            raise ValueError("payment-attempt event debt_id does not match its selected definition")
        if current_activation is None or event.activation_digest != current_activation.digest:
            # Payer rematerialization changes the activation: retain, but do not suppress.
            continue
        current_activation.assert_matches(current_definition)
        event.request.assert_matches(current_definition, current_activation)
        if event.definition_digest != definition.digest:
            continue
        if event.request.digest != request.digest:
            continue
        return event.failure
    return None


def current_payment_attempt_failure(
    shot_folder: str | Path,
    request: JudgmentObservationRequest,
) -> JudgmentPaymentAttemptFailure | None:
    """Return the exact current-generation failure suppressing ``request``, if any."""
    bundle_digest, definition, activation, states, _selection_token = _current_due_request(
        shot_folder,
        request,
    )
    return _failure_for_current_request(
        _event_rows(shot_state_dir(shot_folder) / EVENTS),
        bundle_digest=bundle_digest,
        definition=definition,
        activation=activation,
        states=states,
        request=request,
    )


def record_payment_attempt_failure(
    shot_folder: str | Path,
    request: JudgmentObservationRequest,
    failure: JudgmentPaymentAttemptFailure,
) -> JudgmentPaymentAttemptFailure:
    """Append one typed current payment failure, or return its exact prior record."""
    if not isinstance(failure, JudgmentPaymentAttemptFailure):
        raise ValueError("failure must be a JudgmentPaymentAttemptFailure")
    failure.assert_matches_request(request)
    path = shot_state_dir(shot_folder) / EVENTS
    with _event_lock(path):
        bundle_digest, definition, activation, states, selection_token = _current_due_request(
            shot_folder,
            request,
        )
        existing = _failure_for_current_request(
            _event_rows(path),
            bundle_digest=bundle_digest,
            definition=definition,
            activation=activation,
            states=states,
            request=request,
        )
        if existing is not None:
            if existing != failure:
                raise ValueError(
                    "payment-attempt failure conflicts with the existing exact request record"
                )
            return existing
        event = _PaymentAttemptEvent(
            bundle_digest=bundle_digest,
            debt_id=definition.debt_id,
            definition_digest=definition.digest,
            activation_digest=activation.digest,
            request=request,
            failure=failure,
        )
        serialized = json.dumps(event.as_dict(), sort_keys=True) + "\n"
        prior = path.read_text(encoding="utf-8") if path.is_file() else ""
        with authority_selection_lock(shot_folder, exclusive=False):
            require_matching_authority_selection_token(
                selection_token,
                read_authority_selection_heads(shot_folder).token,
            )
            atomic_write(path, prior + serialized)
        return failure
