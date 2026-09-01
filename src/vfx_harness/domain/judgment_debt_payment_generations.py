"""Immutable completion-receipt generations for qualitative debt payment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from vfx_harness.domain.stop_envelope_primitives import (
    canonical_digest,
    list_value,
    record,
    require_canonical_digest,
    require_digest,
    require_text,
)

if TYPE_CHECKING:
    from vfx_harness.domain.judgment_debt_models import (
        JudgmentDebtActivation,
        JudgmentDebtDefinition,
    )


@dataclass(frozen=True, slots=True)
class JudgmentDebtCompletionBinding:
    """One immutable unit completion consumed by a qualitative observation."""

    SCHEMA: ClassVar[str] = "vfx-harness.judgment-debt-completion-binding/v1"
    layer_id: str
    unit_id: str
    unit_digest: str
    completion_receipt_digest: str

    def __post_init__(self) -> None:
        require_text(self.layer_id, "JudgmentDebtCompletionBinding.layer_id")
        require_text(self.unit_id, "JudgmentDebtCompletionBinding.unit_id")
        require_digest(self.unit_digest, "JudgmentDebtCompletionBinding.unit_digest")
        require_digest(
            self.completion_receipt_digest,
            "JudgmentDebtCompletionBinding.completion_receipt_digest",
        )

    @property
    def identity(self) -> str:
        return f"{self.layer_id}:{self.unit_id}"

    def as_dict(self) -> dict[str, str]:
        return {
            "schema": self.SCHEMA,
            "layer_id": self.layer_id,
            "unit_id": self.unit_id,
            "unit_digest": self.unit_digest,
            "completion_receipt_digest": self.completion_receipt_digest,
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        where: str,
    ) -> JudgmentDebtCompletionBinding:
        row = record(
            value,
            where,
            cls.SCHEMA,
            (
                "layer_id",
                "unit_id",
                "unit_digest",
                "completion_receipt_digest",
            ),
        )
        return cls(
            layer_id=row["layer_id"],
            unit_id=row["unit_id"],
            unit_digest=row["unit_digest"],
            completion_receipt_digest=row["completion_receipt_digest"],
        )


@dataclass(frozen=True, slots=True)
class JudgmentDebtPaymentGeneration:
    """Exact replay and completion-receipt generation that may carry debt state.

    Definition and activation equality alone is insufficient: a later A-like selected
    view may consume a prior payment only while every completion in the paid replay
    prefix remains coordinator-authorized through contiguous preservation lineage.
    """

    SCHEMA: ClassVar[str] = "vfx-harness.judgment-debt-payment-generation/v1"
    definition_digest: str
    activation_digest: str
    replay_prefix_digest: str
    replay_completions: tuple[JudgmentDebtCompletionBinding, ...]
    payer_completions: tuple[JudgmentDebtCompletionBinding, ...]

    def __post_init__(self) -> None:
        require_digest(
            self.definition_digest,
            "JudgmentDebtPaymentGeneration.definition_digest",
        )
        require_digest(
            self.activation_digest,
            "JudgmentDebtPaymentGeneration.activation_digest",
        )
        require_digest(
            self.replay_prefix_digest,
            "JudgmentDebtPaymentGeneration.replay_prefix_digest",
        )
        for rows, where in (
            (self.replay_completions, "replay_completions"),
            (self.payer_completions, "payer_completions"),
        ):
            if (
                not isinstance(rows, tuple)
                or not rows
                or any(
                    not isinstance(row, JudgmentDebtCompletionBinding)
                    for row in rows
                )
            ):
                raise ValueError(
                    f"JudgmentDebtPaymentGeneration.{where} must be a non-empty "
                    "tuple of JudgmentDebtCompletionBinding values"
                )
            identities = [row.identity for row in rows]
            if len(identities) != len(set(identities)):
                raise ValueError(
                    f"JudgmentDebtPaymentGeneration.{where} contains duplicate units"
                )
            if identities != sorted(identities):
                raise ValueError(
                    f"JudgmentDebtPaymentGeneration.{where} must be identity-sorted"
                )
        replay = {row.identity: row for row in self.replay_completions}
        if any(replay.get(row.identity) != row for row in self.payer_completions):
            raise ValueError(
                "JudgmentDebtPaymentGeneration payer completions must be an exact "
                "subset of its replay completions"
            )

    def assert_matches(
        self,
        definition: JudgmentDebtDefinition,
        activation: JudgmentDebtActivation,
    ) -> None:
        activation.assert_matches(definition)
        if (
            self.definition_digest != definition.digest
            or self.activation_digest != activation.digest
        ):
            raise ValueError(
                "JudgmentDebtPaymentGeneration does not match the exact debt activation"
            )
        expected_payers = dict(activation.payer_unit_digests)
        observed_payers = {
            row.identity: row.unit_digest for row in self.payer_completions
        }
        if observed_payers != expected_payers:
            raise ValueError(
                "JudgmentDebtPaymentGeneration payer completions do not match the "
                "activation's exact payer unit set"
            )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "definition_digest": self.definition_digest,
            "activation_digest": self.activation_digest,
            "replay_prefix_digest": self.replay_prefix_digest,
            "replay_completions": [
                row.as_dict() for row in self.replay_completions
            ],
            "payer_completions": [row.as_dict() for row in self.payer_completions],
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "payment_generation_digest": self.digest}

    @classmethod
    def from_dict(
        cls,
        value: Any,
        where: str,
    ) -> JudgmentDebtPaymentGeneration:
        row = record(
            value,
            where,
            cls.SCHEMA,
            (
                "definition_digest",
                "activation_digest",
                "replay_prefix_digest",
                "replay_completions",
                "payer_completions",
                "payment_generation_digest",
            ),
        )
        candidate = cls(
            definition_digest=row["definition_digest"],
            activation_digest=row["activation_digest"],
            replay_prefix_digest=row["replay_prefix_digest"],
            replay_completions=tuple(
                JudgmentDebtCompletionBinding.from_dict(
                    item,
                    f"{where}.replay_completions[{index}]",
                )
                for index, item in enumerate(
                    list_value(
                        row["replay_completions"],
                        f"{where}.replay_completions",
                    )
                )
            ),
            payer_completions=tuple(
                JudgmentDebtCompletionBinding.from_dict(
                    item,
                    f"{where}.payer_completions[{index}]",
                )
                for index, item in enumerate(
                    list_value(
                        row["payer_completions"],
                        f"{where}.payer_completions",
                    )
                )
            ),
        )
        require_canonical_digest(
            row["payment_generation_digest"],
            candidate.digest,
            where,
            "payment_generation_digest",
        )
        return candidate


__all__ = [
    "JudgmentDebtCompletionBinding",
    "JudgmentDebtPaymentGeneration",
]
