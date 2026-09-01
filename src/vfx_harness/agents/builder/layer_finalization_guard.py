"""Builder adapters for claimed and terminal layer-finalization authority."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

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
        try:
            with active_layer_finalization_guard(
                self.folder,
                self.claim,
                self.units,
                selection_token=self.selected_authority.selection_token,
            ) as current:
                yield current
        except LayerFinalizationAuthorityLost:
            raise
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise LayerFinalizationAuthorityLost(
                f"{operation} refused because {self.label} lost authority: {exc}"
            ) from exc

    def check(self, operation: str) -> LayerFinalizationClaim:
        with self.hold(operation) as current:
            return current

    def publish(self, operation: str, mutation: Callable[[], _T]) -> _T:
        with self.hold(operation):
            return mutation()


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
            with terminal_layer_finalization_guard(
                self.folder,
                self.receipt,
                self.units,
                selection_token=self.selected_authority.selection_token,
                lineage_authorization=lineage_authorization,
            ) as current:
                yield current
        except LayerFinalizationAuthorityLost:
            raise
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise LayerFinalizationAuthorityLost(
                f"{operation} refused because {self.label} lost authority: {exc}"
            ) from exc

    def check(self, operation: str) -> LayerFinalizationReceipt:
        with self.hold(operation) as current:
            return current

    def publish(self, operation: str, mutation: Callable[[], _T]) -> _T:
        with self.hold(operation):
            return mutation()


__all__ = [
    "LayerFinalizationAuthorityLost",
    "LayerFinalizationClaimGuard",
    "LayerFinalizationReceiptGuard",
]
