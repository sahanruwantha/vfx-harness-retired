"""Structural authority required around builder execution and publication."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

_T = TypeVar("_T")


class ExecutionAuthorityLost(RuntimeError):
    """The exact execution owner changed before guarded work could be consumed."""


@dataclass(frozen=True, slots=True)
class LedgerPublicationScope:
    """Closed ledger row one execution authority is allowed to project.

    A selected-authority token proves which plan generation is current, but it does
    not say whether the writer owns a work-unit row, an in-progress layer row, or a
    terminal layer projection.  Keeping that distinction typed prevents a unit
    attempt from turning an arbitrary ``Milestone`` supplied by its caller into
    apparent layer-finalization authority.
    """

    kind: str
    milestone_id: str
    claim_id: str
    receipt_digest: str | None = None
    terminal_status: str | None = None
    script_path: str | None = None
    script_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {
            "unit_attempt",
            "layer_finalization_claim",
            "layer_finalization_receipt",
        }:
            raise ValueError(f"unsupported builder ledger scope: {self.kind!r}")
        for field, value in (
            ("milestone_id", self.milestone_id),
            ("claim_id", self.claim_id),
        ):
            if not value or value != value.strip():
                raise ValueError(f"builder ledger scope requires a non-empty {field}")
        terminal = self.kind == "layer_finalization_receipt"
        terminal_values = (
            self.receipt_digest,
            self.terminal_status,
            self.script_path,
            self.script_sha256,
        )
        if terminal and any(value is None for value in terminal_values):
            raise ValueError(
                "terminal layer ledger scope requires receipt, status, and script identity"
            )
        if not terminal and any(value is not None for value in terminal_values):
            raise ValueError(
                "non-terminal builder ledger scope cannot carry terminal receipt fields"
            )


class ExecutionGuard(Protocol):
    """Minimum exact-authority interface shared by unit and layer execution.

    ``authority_binding`` contributes the typed owner identity to prepared durable
    publications. Its keys must not replace the ledger's schema or selected-authority
    token. ``label`` is diagnostic only; no caller derives authority from it.
    """

    @property
    def label(self) -> str: ...

    @property
    def authority_binding(self) -> Mapping[str, Any]: ...

    @property
    def ledger_publication_scope(self) -> LedgerPublicationScope: ...

    def check(self, operation: str) -> object: ...

    def publish(self, operation: str, mutation: Callable[[], _T]) -> _T: ...
