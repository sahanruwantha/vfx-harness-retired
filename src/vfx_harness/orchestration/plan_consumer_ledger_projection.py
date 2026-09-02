"""Prepared CAS owner for the ``shot.json`` copy inside a consumer view."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import TypeVar

from vfx_harness.observability.prepared_publication import (
    FilePublicationConflict,
    PreparedFilePublication,
    commit_prepared_file,
    discard_prepared_file,
    prepare_file_update,
    verify_prepared_file_payload,
)
from vfx_harness.observability.prepared_publication_destinations import (
    _bind_prepared_publication_destination_issuer,
    _issue_prepared_publication_destination_authorization,
)
from vfx_harness.orchestration.plan_consumer_view_mutation import (
    PlanConsumerViewMutationCapability,
    PlanConsumerViewMutationConflict,
    _advance_constructed_plan_consumer_ledger_binding,
    _require_current_plan_consumer_view_mutation,
)

_T = TypeVar("_T")

_DESTINATION_ISSUER = _bind_prepared_publication_destination_issuer(
    family="plan-consumer-ledger",
    owner_type=PlanConsumerViewMutationCapability,
)


def update_plan_consumer_ledger(
    capability: PlanConsumerViewMutationCapability,
    update: Callable[[bytes | None], tuple[bytes | None, _T]],
) -> tuple[_T, str | None]:
    """CAS-update only an installed isolated view's ledger."""

    if not callable(update):
        raise PlanConsumerViewMutationConflict(
            "plan-consumer ledger projection requires a callable update"
        )
    record = _require_current_plan_consumer_view_mutation(
        capability,
        phase="installed",
    )
    destination = record.view / "shot.json"
    binding = (
        f"plan-consumer-ledger:{record.phase}:"
        f"{record.view_identity.device}:{record.view_identity.inode}:"
        f"{record.marker_sha256}"
    )
    transaction_binding = object()
    publication: PreparedFilePublication | None = None

    def require_commit_authority(
        authorization: object,
        observed_transaction_binding: object,
    ) -> None:
        if authorization is not capability:
            raise FilePublicationConflict(
                "plan-consumer ledger commit requires its exact view capability"
            )
        if observed_transaction_binding is not transaction_binding:
            raise FilePublicationConflict(
                "plan-consumer ledger commit belongs to another prepared transaction"
            )
        current = _require_current_plan_consumer_view_mutation(
            capability,
            phase="installed",
        )
        if current is not record:
            raise FilePublicationConflict(
                "plan-consumer ledger capability registry changed before commit"
            )

    def bind_result(
        current: bytes | None,
    ) -> tuple[bytes | None, tuple[_T, str | None]]:
        payload, result = update(current)
        digest = None if payload is None else hashlib.sha256(payload).hexdigest()
        return payload, (result, digest)

    try:
        authorization = _issue_prepared_publication_destination_authorization(
            issuer=_DESTINATION_ISSUER,
            shot_folder=record.view,
            destination=destination,
        )
        prepared = prepare_file_update(
            record.view,
            destination,
            bind_result,
            authority_binding=binding,
            commit_policy=require_commit_authority,
            destination_authorization=authorization,
        )
        publication = prepared.publication
        result, expected_sha256 = prepared.result
        if publication is None:
            if expected_sha256 is not None:
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer ledger no-op unexpectedly retained a payload digest"
                )
            if (
                _require_current_plan_consumer_view_mutation(
                    capability,
                    phase="installed",
                )
                is not record
            ):
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer ledger capability changed during no-op"
                )
            return result, None
        if expected_sha256 is None:
            raise PlanConsumerViewMutationConflict(
                "plan-consumer ledger replacement omitted its payload digest"
            )
        verification = verify_prepared_file_payload(
            publication,
            expected_sha256=expected_sha256,
            transaction_binding=transaction_binding,
        )
        observed = commit_prepared_file(
            publication,
            authority_binding=binding,
            payload_verification=verification,
            transaction_binding=transaction_binding,
            commit_authorization=capability,
        )
        publication = None
        if observed != expected_sha256:
            raise PlanConsumerViewMutationConflict(
                "plan-consumer ledger committed a different prepared generation"
            )
        _advance_constructed_plan_consumer_ledger_binding(
            capability,
            record,
            observed,
        )
        _require_current_plan_consumer_view_mutation(
            capability,
            phase="installed",
        )
        return result, observed
    except (FilePublicationConflict, OSError) as exc:
        discard_prepared_file(publication)
        raise PlanConsumerViewMutationConflict(str(exc)) from exc
    except BaseException:
        discard_prepared_file(publication)
        raise


__all__ = [
    "update_plan_consumer_ledger",
]
