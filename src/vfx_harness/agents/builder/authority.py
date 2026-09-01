"""Exact selected-authority guards for builder-owned durable mutations."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from vfx_harness.agents.builder.attempt_guard import UnitAttemptGuard


class AuthorityBoundLedger(Ledger):
    """Builder ledger whose every publication is exact-selection CAS guarded."""

    def __init__(
        self,
        shot,
        selected_authority: ResolvedSelectedAuthority,
        *,
        attempt_guard: UnitAttemptGuard | None = None,
    ) -> None:
        self._builder_selected_authority = selected_authority
        self._builder_attempt_guard = attempt_guard
        super().__init__(shot, selected_authority=selected_authority)

    def save(self) -> None:
        attempt_guard = self._builder_attempt_guard
        binding = json.dumps(
            {
                "schema": "vfx-harness.builder-ledger-publication-binding/v1",
                "selection_token": self._builder_selected_authority.selection_token.to_dict(),
                "attempt": (
                    attempt_guard.claim.as_dict() if attempt_guard is not None else None
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        if attempt_guard is not None:
            attempt_guard.check("start builder ledger publication staging")
        else:
            require_selected_authority_unchanged(
                self.shot.folder,
                self._builder_selected_authority,
                operation="start builder ledger publication staging",
            )
        prepared = super().prepare_save(authority_binding=binding)
        try:
            if attempt_guard is not None:
                attempt_guard.publish(
                    "builder ledger publication",
                    lambda: Ledger.commit_prepared_save(
                        self,
                        prepared,
                        authority_binding=binding,
                    ),
                )
                return
            with selected_authority_commit(
                self.shot.folder,
                self._builder_selected_authority,
                operation="builder ledger publication",
            ):
                Ledger.commit_prepared_save(
                    self,
                    prepared,
                    authority_binding=binding,
                )
        finally:
            Ledger.discard_prepared_save(prepared)
