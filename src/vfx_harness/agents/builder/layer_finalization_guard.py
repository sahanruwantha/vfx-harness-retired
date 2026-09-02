"""Builder adapters for claimed and terminal layer-finalization authority."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import vfx_harness.orchestration.shot_authority_capture as shot_authority_capture
from vfx_harness.agents.builder.execution_guard import (
    ExecutionAuthorityLost,
    LedgerPublicationScope,
)
from vfx_harness.domain.authority_head_records import parse_authority_selection_token
from vfx_harness.domain.layer_finalizations import (
    LayerFinalizationClaim,
    LayerFinalizationReceipt,
)
from vfx_harness.domain.work_units import WorkUnit
from vfx_harness.orchestration import layer_finalization_publication_authority
from vfx_harness.orchestration.authority_receipt_lineage import (
    require_preserved_layer_finalization_authorization,
)
from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority
from vfx_harness.orchestration.layer_finalization_state import (
    active_layer_finalization_guard,
    terminal_layer_finalization_guard,
)

_T = TypeVar("_T")


class LayerFinalizationAuthorityLost(ExecutionAuthorityLost):
    """The exact layer-finalization generation no longer owns publication."""


@contextmanager
def _translate_finalization_hold(
    manager: AbstractContextManager[_T],
    *,
    operation: str,
    label: str,
) -> Iterator[_T]:
    """Translate guard acquisition/release failures without masking body errors."""

    try:
        current = manager.__enter__()
    except LayerFinalizationAuthorityLost:
        raise
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise LayerFinalizationAuthorityLost(
            f"{operation} refused because {label} lost authority: {exc}"
        ) from exc
    try:
        yield current
    except BaseException as body_error:
        try:
            suppress = manager.__exit__(
                type(body_error),
                body_error,
                body_error.__traceback__,
            )
        except BaseException as exit_error:
            if exit_error is body_error:
                raise
            if isinstance(exit_error, LayerFinalizationAuthorityLost):
                raise
            if isinstance(exit_error, (KeyError, OSError, TypeError, ValueError)):
                raise LayerFinalizationAuthorityLost(
                    f"{operation} refused because {label} lost authority: {exit_error}"
                ) from exit_error
            raise
        if not suppress:
            raise
    else:
        try:
            manager.__exit__(None, None, None)
        except LayerFinalizationAuthorityLost:
            raise
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise LayerFinalizationAuthorityLost(
                f"{operation} refused because {label} lost authority: {exc}"
            ) from exc


@dataclass(frozen=True, slots=True)
class LayerFinalizationClaimGuard:
    folder: Path
    claim: LayerFinalizationClaim
    units: tuple[WorkUnit, ...]
    selected_authority: ResolvedSelectedAuthority

    @classmethod
    def bind(
        cls,
        folder: str | Path,
        claim: LayerFinalizationClaim,
        units: tuple[WorkUnit, ...],
        selected_authority: ResolvedSelectedAuthority,
    ) -> LayerFinalizationClaimGuard:
        guard = cls(Path(folder), claim, tuple(units), selected_authority)
        guard.check("bind active layer-finalization claim")
        return guard

    @property
    def label(self) -> str:
        return f"layer {self.claim.layer_id} finalization {self.claim.claim_id}"

    @property
    def authority_binding(self) -> dict:
        return {"layer_finalization_claim": self.claim.as_dict()}

    @property
    def ledger_publication_scope(self) -> LedgerPublicationScope:
        return LedgerPublicationScope(
            kind="layer_finalization_claim",
            milestone_id=self.claim.layer_id,
            claim_id=self.claim.claim_id,
        )

    @contextmanager
    def hold(self, operation: str) -> Iterator[LayerFinalizationClaim]:
        manager = active_layer_finalization_guard(
            self.folder,
            self.claim,
            self.units,
            selection_token=self.selected_authority.selection_token,
        )
        with (
            _translate_finalization_hold(
                manager,
                operation=operation,
                label=self.label,
            ) as current,
            layer_finalization_publication_authority._hold_layer_finalization_prepared_mutation_guard(
                issuer=_PREPARED_MUTATION_ISSUER,
                guard=self,
                shot_folder=self.folder,
                claim=self.claim,
                receipt=None,
            ),
        ):
            yield current

    def check(self, operation: str) -> LayerFinalizationClaim:
        with self.hold(operation) as current:
            return current

    def publish(self, operation: str, mutation: Callable[[], _T]) -> _T:
        with self.hold(operation):
            return mutation()

    def publish_prepared(
        self,
        operation: str,
        transaction_binding: object,
        mutation: Callable[
            [
                layer_finalization_publication_authority.LayerFinalizationPreparedMutationAuthorization
            ],
            _T,
        ],
    ) -> _T:
        with (
            shot_authority_capture.shot_authority_writer_fence(self.folder) as capability,
            self.hold(operation),
            layer_finalization_publication_authority._issue_layer_finalization_prepared_mutation_authorization(
                issuer=_PREPARED_MUTATION_ISSUER,
                guard=self,
                shot_folder=self.folder,
                claim=self.claim,
                receipt=None,
                writer_capability=capability,
                transaction_binding=transaction_binding,
            ) as authorization,
        ):
            return mutation(authorization)


@dataclass(frozen=True, slots=True)
class LayerFinalizationReceiptGuard:
    folder: Path
    receipt: LayerFinalizationReceipt
    units: tuple[WorkUnit, ...]
    selected_authority: ResolvedSelectedAuthority

    @property
    def claim(self) -> LayerFinalizationClaim:
        return self.receipt.claim

    @classmethod
    def bind(
        cls,
        folder: str | Path,
        receipt: LayerFinalizationReceipt,
        units: tuple[WorkUnit, ...],
        selected_authority: ResolvedSelectedAuthority,
    ) -> LayerFinalizationReceiptGuard:
        guard = cls(Path(folder), receipt, tuple(units), selected_authority)
        guard.check("bind terminal layer-finalization receipt")
        return guard

    @property
    def label(self) -> str:
        return f"layer {self.receipt.claim.layer_id} terminal finalization {self.receipt.receipt_digest}"

    @property
    def authority_binding(self) -> dict:
        return {"layer_finalization_receipt": self.receipt.as_dict()}

    @property
    def ledger_publication_scope(self) -> LedgerPublicationScope:
        return LedgerPublicationScope(
            kind="layer_finalization_receipt",
            milestone_id=self.receipt.claim.layer_id,
            claim_id=self.receipt.claim.claim_id,
            receipt_digest=self.receipt.receipt_digest,
            terminal_status=self.receipt.final_status,
            script_path=self.receipt.layer_script_path,
            script_sha256=self.receipt.layer_script_sha256,
        )

    @contextmanager
    def hold(self, operation: str) -> Iterator[LayerFinalizationReceipt]:
        try:
            current_projection = parse_authority_selection_token(
                self.selected_authority.selection_token.to_dict(),
                "selected authority for terminal layer finalization",
            )
            lineage_authorization = (
                None
                if self.receipt.claim.selection_token == current_projection
                else require_preserved_layer_finalization_authorization(
                    self.folder,
                    self.receipt,
                    self.selected_authority,
                )
            )
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise LayerFinalizationAuthorityLost(
                f"{operation} refused because {self.label} lost authority: {exc}"
            ) from exc
        manager = terminal_layer_finalization_guard(
            self.folder,
            self.receipt,
            self.units,
            selection_token=self.selected_authority.selection_token,
            lineage_authorization=lineage_authorization,
        )
        with (
            _translate_finalization_hold(
                manager,
                operation=operation,
                label=self.label,
            ) as current,
            layer_finalization_publication_authority._hold_layer_finalization_prepared_mutation_guard(
                issuer=_PREPARED_MUTATION_ISSUER,
                guard=self,
                shot_folder=self.folder,
                claim=self.claim,
                receipt=self.receipt,
            ),
        ):
            yield current

    def check(self, operation: str) -> LayerFinalizationReceipt:
        with self.hold(operation) as current:
            return current

    def publish(self, operation: str, mutation: Callable[[], _T]) -> _T:
        with self.hold(operation):
            return mutation()

    def publish_prepared(
        self,
        operation: str,
        transaction_binding: object,
        mutation: Callable[
            [
                layer_finalization_publication_authority.LayerFinalizationPreparedMutationAuthorization
            ],
            _T,
        ],
    ) -> _T:
        with (
            shot_authority_capture.shot_authority_writer_fence(self.folder) as capability,
            self.hold(operation),
            layer_finalization_publication_authority._issue_layer_finalization_prepared_mutation_authorization(
                issuer=_PREPARED_MUTATION_ISSUER,
                guard=self,
                shot_folder=self.folder,
                claim=self.claim,
                receipt=self.receipt,
                writer_capability=capability,
                transaction_binding=transaction_binding,
            ) as authorization,
        ):
            return mutation(authorization)


_PREPARED_MUTATION_ISSUER = (
    layer_finalization_publication_authority._bind_layer_finalization_prepared_mutation_issuer(
        claim_guard_type=LayerFinalizationClaimGuard,
        receipt_guard_type=LayerFinalizationReceiptGuard,
    )
)


__all__ = [
    "LayerFinalizationAuthorityLost",
    "LayerFinalizationClaimGuard",
    "LayerFinalizationReceiptGuard",
]
