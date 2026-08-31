"""Exact selected-authority guards for builder-owned durable mutations."""

from __future__ import annotations

from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority
from vfx_harness.orchestration.ledger import Ledger
from vfx_harness.orchestration.selected_authority_guard import (
    commit_selected_authority as commit_selected_authority,
)
from vfx_harness.orchestration.selected_authority_guard import (
    require_selected_authority_unchanged as require_selected_authority_unchanged,
)
from vfx_harness.orchestration.selected_authority_guard import (
    selected_authority_commit as selected_authority_commit,
)


class AuthorityBoundLedger(Ledger):
    """Builder ledger whose every publication is exact-selection CAS guarded."""

    def __init__(
        self,
        shot,
        selected_authority: ResolvedSelectedAuthority,
    ) -> None:
        self._builder_selected_authority = selected_authority
        super().__init__(shot, selected_authority=selected_authority)

    def save(self) -> None:
        with selected_authority_commit(
            self.shot.folder,
            self._builder_selected_authority,
            operation="builder ledger publication",
        ):
            super().save()
