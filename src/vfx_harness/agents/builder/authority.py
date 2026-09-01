"""Exact selected-authority guards for builder-owned durable mutations."""

from __future__ import annotations

import json
from collections.abc import Mapping

from vfx_harness.agents.builder.execution_guard import (
    ExecutionGuard,
    LedgerPublicationScope,
)
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
        *,
        execution_guard: ExecutionGuard,
    ) -> None:
        scope = execution_guard.ledger_publication_scope
        if not isinstance(scope, LedgerPublicationScope):
            raise TypeError(
                "builder execution guard requires a typed ledger publication scope"
            )
        self._builder_selected_authority = selected_authority
        self._builder_execution_guard = execution_guard
        self._builder_ledger_scope = scope
        super().__init__(shot, selected_authority=selected_authority)

    def _slot(self, milestone) -> dict:
        expected = self._builder_ledger_scope.milestone_id
        if str(milestone.id) != expected:
            raise ValueError(
                "builder ledger authority cannot mutate another milestone: "
                f"expected {expected!r}, found {str(milestone.id)!r}"
            )
        return super()._slot(milestone)

    def _require_scope_current(self, execution_binding: Mapping) -> None:
        scope = self._builder_ledger_scope
        if self._builder_execution_guard.ledger_publication_scope != scope:
            raise ValueError("builder ledger publication scope changed after binding")
        if self._touched != {scope.milestone_id}:
            raise ValueError(
                "builder ledger publication must touch exactly its authorized milestone: "
                f"expected {scope.milestone_id!r}, found {sorted(self._touched)!r}"
            )
        changed_top_level = sorted(
            key
            for key in set(self.data) | set(self._loaded)
            if key != "milestones"
            and (
                key not in self.data
                or key not in self._loaded
                or self.data[key] != self._loaded[key]
            )
        )
        if changed_top_level:
            raise ValueError(
                "builder ledger authority cannot mutate top-level ledger fields: "
                + ", ".join(changed_top_level)
            )
        try:
            slot = self.data["milestones"][scope.milestone_id]
        except (KeyError, TypeError) as exc:
            raise ValueError(
                "builder ledger publication omits its authorized milestone row"
            ) from exc
        if not isinstance(slot, Mapping):
            raise ValueError("builder ledger milestone row must be an object")

        if scope.kind == "unit_attempt":
            claim = execution_binding.get("attempt")
            observed = (
                claim.get("claim_id"),
                f"{claim.get('layer_id')}@{claim.get('unit_id')}",
            ) if isinstance(claim, Mapping) else (None, None)
            if observed != (scope.claim_id, scope.milestone_id):
                raise ValueError(
                    "unit ledger scope does not match its exact attempt binding"
                )
            reserved = {
                "finalization_receipt_digest",
                "layer_finalization_claim",
                "script_sha256",
            } & set(slot)
            if reserved:
                raise ValueError(
                    "unit attempt cannot publish layer-finalization ledger fields: "
                    + ", ".join(sorted(reserved))
                )
            return

        if scope.kind == "layer_finalization_claim":
            claim = execution_binding.get("layer_finalization_claim")
            observed = (
                claim.get("claim_id"),
                claim.get("layer_id"),
            ) if isinstance(claim, Mapping) else (None, None)
            if observed != (scope.claim_id, scope.milestone_id):
                raise ValueError(
                    "layer ledger scope does not match its exact finalization claim"
                )
            if (
                slot.get("status") != "in_progress"
                or slot.get("layer_finalization_claim") != scope.claim_id
                or slot.get("script") != claim.get("layer_script_path")
                or "finalization_receipt_digest" in slot
            ):
                raise ValueError(
                    "active layer-finalization claim may publish only its exact "
                    "in-progress ledger row"
                )
            return

        receipt = execution_binding.get("layer_finalization_receipt")
        receipt_claim = receipt.get("claim") if isinstance(receipt, Mapping) else None
        observed = (
            receipt.get("receipt_digest"),
            receipt.get("final_status"),
            receipt.get("layer_script_path"),
            receipt.get("layer_script_sha256"),
            receipt_claim.get("claim_id") if isinstance(receipt_claim, Mapping) else None,
            receipt_claim.get("layer_id") if isinstance(receipt_claim, Mapping) else None,
        ) if isinstance(receipt, Mapping) else (None,) * 6
        expected = (
            scope.receipt_digest,
            scope.terminal_status,
            scope.script_path,
            scope.script_sha256,
            scope.claim_id,
            scope.milestone_id,
        )
        if observed != expected:
            raise ValueError(
                "terminal layer ledger scope does not match its exact receipt binding"
            )
        if (
            slot.get("status") != scope.terminal_status
            or slot.get("finalization_receipt_digest") != scope.receipt_digest
            or slot.get("layer_finalization_claim") != scope.claim_id
            or slot.get("script") != scope.script_path
            or slot.get("script_sha256") != scope.script_sha256
            or slot.get("script_sha") != str(scope.script_sha256)[:16]
        ):
            raise ValueError(
                "terminal layer ledger projection must exactly name its finalization receipt"
            )

    def save(self) -> None:
        execution_guard = self._builder_execution_guard
        execution_binding = dict(execution_guard.authority_binding)
        reserved = {"schema", "selection_token"} & set(execution_binding)
        if reserved:
            raise ValueError(
                "builder execution guard authority binding cannot replace reserved "
                f"ledger field(s): {', '.join(sorted(reserved))}"
            )
        if not execution_binding:
            raise ValueError("builder execution guard authority binding must not be empty")
        self._require_scope_current(execution_binding)
        binding = json.dumps(
            {
                "schema": "vfx-harness.builder-ledger-publication-binding/v1",
                "selection_token": self._builder_selected_authority.selection_token.to_dict(),
                **execution_binding,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        execution_guard.check("start builder ledger publication staging")
        prepared = super().prepare_save(authority_binding=binding)
        try:
            execution_guard.publish(
                "builder ledger publication",
                lambda: Ledger.commit_prepared_save(
                    self,
                    prepared,
                    authority_binding=binding,
                ),
            )
        finally:
            Ledger.discard_prepared_save(prepared)
