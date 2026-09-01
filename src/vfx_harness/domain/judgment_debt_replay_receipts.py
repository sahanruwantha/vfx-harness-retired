"""Pure receipts for a qualitative judgment's exact canonical replay prefix."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from vfx_harness.domain.judgment_debt_activation import (
    validate_judgment_debt_replay_prefix,
)
from vfx_harness.domain.judgment_debt_models import (
    JudgmentDebtActivation,
    JudgmentDebtDefinition,
)
from vfx_harness.domain.judgment_debt_payment_generations import (
    JudgmentDebtCompletionBinding,
    JudgmentDebtPaymentGeneration,
)
from vfx_harness.domain.unit_evaluation_receipts import ReplayDependencyBinding

REPLAY_PREFIX_RECEIPT_SCHEMA = "vfx-harness.judgment-debt-replay-prefix/v3"


def _require_sha256(value: object, where: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{where} must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class ReplayPrefixUnitReceipt:
    """One checkpoint-concordant unit actually replayed in canonical order."""

    layer_id: str
    unit_id: str
    unit_digest: str
    checkpoint_unit_digest: str
    script_path: str
    script_sha256: str
    checkpoint_script_sha256: str
    completion_receipt_digest: str

    def __post_init__(self) -> None:
        for value, where in (
            (self.layer_id, "ReplayPrefixUnitReceipt.layer_id"),
            (self.unit_id, "ReplayPrefixUnitReceipt.unit_id"),
            (self.script_path, "ReplayPrefixUnitReceipt.script_path"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{where} must be a non-empty string")
        for value, where in (
            (self.unit_digest, "ReplayPrefixUnitReceipt.unit_digest"),
            (
                self.checkpoint_unit_digest,
                "ReplayPrefixUnitReceipt.checkpoint_unit_digest",
            ),
            (self.script_sha256, "ReplayPrefixUnitReceipt.script_sha256"),
            (
                self.checkpoint_script_sha256,
                "ReplayPrefixUnitReceipt.checkpoint_script_sha256",
            ),
            (
                self.completion_receipt_digest,
                "ReplayPrefixUnitReceipt.completion_receipt_digest",
            ),
        ):
            _require_sha256(value, where)
        if self.checkpoint_unit_digest != self.unit_digest:
            raise ValueError(
                "replay receipt checkpoint unit digest does not match selected unit"
            )
        if self.checkpoint_script_sha256 != self.script_sha256:
            raise ValueError(
                "replay receipt checkpoint script digest does not match replayed artifact"
            )

    @property
    def identity(self) -> str:
        return f"{self.layer_id}:{self.unit_id}"

    def as_dict(self) -> dict[str, str]:
        return {
            "layer_id": self.layer_id,
            "unit_id": self.unit_id,
            "unit_digest": self.unit_digest,
            "checkpoint_unit_digest": self.checkpoint_unit_digest,
            "script_path": self.script_path,
            "script_sha256": self.script_sha256,
            "checkpoint_script_sha256": self.checkpoint_script_sha256,
            "completion_receipt_digest": self.completion_receipt_digest,
        }

    @classmethod
    def from_dict(cls, value: object, where: str) -> ReplayPrefixUnitReceipt:
        expected = {
            "layer_id",
            "unit_id",
            "unit_digest",
            "checkpoint_unit_digest",
            "script_path",
            "script_sha256",
            "checkpoint_script_sha256",
            "completion_receipt_digest",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError(f"{where} has unsupported replay-prefix unit shape")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class ReplayPrefixLayerReceipt:
    """One selected layer script and its exact replayed unit receipts."""

    layer_id: str
    layer_generation_digest: str
    predecessor_layer_digests: tuple[tuple[str, str], ...]
    script_path: str
    script_sha256: str
    dependencies: tuple[ReplayDependencyBinding, ...]
    units: tuple[ReplayPrefixUnitReceipt, ...]
    finalization_receipt_digest: str | None = None
    payer_claim_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.layer_id, str) or not self.layer_id.strip():
            raise ValueError(
                "ReplayPrefixLayerReceipt.layer_id must be a non-empty string"
            )
        if not isinstance(self.script_path, str) or not self.script_path.strip():
            raise ValueError(
                "ReplayPrefixLayerReceipt.script_path must be a non-empty string"
            )
        _require_sha256(
            self.layer_generation_digest,
            "ReplayPrefixLayerReceipt.layer_generation_digest",
        )
        _require_sha256(
            self.script_sha256,
            "ReplayPrefixLayerReceipt.script_sha256",
        )
        if not isinstance(self.predecessor_layer_digests, tuple):
            raise ValueError(
                "ReplayPrefixLayerReceipt.predecessor_layer_digests must be a tuple"
            )
        predecessor_ids: list[str] = []
        for index, row in enumerate(self.predecessor_layer_digests):
            if (
                not isinstance(row, tuple)
                or len(row) != 2
                or not isinstance(row[0], str)
                or not row[0].strip()
            ):
                raise ValueError(
                    "ReplayPrefixLayerReceipt.predecessor_layer_digests"
                    f"[{index}] must be a (layer_id, digest) pair"
                )
            _require_sha256(
                row[1],
                "ReplayPrefixLayerReceipt.predecessor_layer_digests"
                f"[{index}].digest",
            )
            predecessor_ids.append(row[0])
        if len(predecessor_ids) != len(set(predecessor_ids)):
            raise ValueError(
                "ReplayPrefixLayerReceipt.predecessor_layer_digests contains "
                "duplicate layer ids"
            )
        if not isinstance(self.dependencies, tuple) or any(
            not isinstance(dependency, ReplayDependencyBinding)
            for dependency in self.dependencies
        ):
            raise ValueError(
                "ReplayPrefixLayerReceipt.dependencies must be typed replay dependencies"
            )
        if not isinstance(self.units, tuple) or not self.units:
            raise ValueError("ReplayPrefixLayerReceipt.units must be non-empty")
        if any(unit.layer_id != self.layer_id for unit in self.units):
            raise ValueError(
                "replay receipt unit layer ids must match their layer receipt"
            )
        unit_ids = [unit.unit_id for unit in self.units]
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("replay receipt layer contains duplicate unit ids")
        if self.finalization_receipt_digest is not None:
            _require_sha256(
                self.finalization_receipt_digest,
                "ReplayPrefixLayerReceipt.finalization_receipt_digest",
            )
        if self.payer_claim_id is not None and (
            not isinstance(self.payer_claim_id, str)
            or not self.payer_claim_id.strip()
        ):
            raise ValueError(
                "ReplayPrefixLayerReceipt.payer_claim_id must be a non-empty string"
            )
        if (self.finalization_receipt_digest is None) == (
            self.payer_claim_id is None
        ):
            raise ValueError(
                "replay-prefix layer must bind exactly one predecessor finalization "
                "receipt or payer finalization claim"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "layer_id": self.layer_id,
            "layer_generation_digest": self.layer_generation_digest,
            "predecessor_layer_digests": [
                {"layer_id": layer_id, "digest": digest}
                for layer_id, digest in self.predecessor_layer_digests
            ],
            "script_path": self.script_path,
            "script_sha256": self.script_sha256,
            "dependencies": [row.as_dict() for row in self.dependencies],
            "units": [unit.as_dict() for unit in self.units],
            "finalization_receipt_digest": self.finalization_receipt_digest,
            "payer_claim_id": self.payer_claim_id,
        }

    @classmethod
    def from_dict(cls, value: object, where: str) -> ReplayPrefixLayerReceipt:
        expected = {
            "layer_id",
            "layer_generation_digest",
            "predecessor_layer_digests",
            "script_path",
            "script_sha256",
            "dependencies",
            "units",
            "finalization_receipt_digest",
            "payer_claim_id",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError(f"{where} has unsupported replay-prefix layer shape")
        dependencies = value["dependencies"]
        units = value["units"]
        predecessors = value["predecessor_layer_digests"]
        if (
            not isinstance(dependencies, list)
            or not isinstance(units, list)
            or not isinstance(predecessors, list)
        ):
            raise ValueError(
                f"{where} predecessor digests, dependencies, and units must be lists"
            )
        predecessor_rows: list[tuple[str, str]] = []
        for index, row in enumerate(predecessors):
            if not isinstance(row, dict) or set(row) != {"layer_id", "digest"}:
                raise ValueError(
                    f"{where}.predecessor_layer_digests[{index}] has unsupported shape"
                )
            predecessor_rows.append((row["layer_id"], row["digest"]))
        return cls(
            layer_id=value["layer_id"],
            layer_generation_digest=value["layer_generation_digest"],
            predecessor_layer_digests=tuple(predecessor_rows),
            script_path=value["script_path"],
            script_sha256=value["script_sha256"],
            dependencies=tuple(
                ReplayDependencyBinding.parse(
                    row,
                    f"{where}.dependencies[{index}]",
                )
                for index, row in enumerate(dependencies)
            ),
            units=tuple(
                ReplayPrefixUnitReceipt.from_dict(
                    row,
                    f"{where}.units[{index}]",
                )
                for index, row in enumerate(units)
            ),
            finalization_receipt_digest=value["finalization_receipt_digest"],
            payer_claim_id=value["payer_claim_id"],
        )


@dataclass(frozen=True, slots=True)
class ReplayPrefixReceipt:
    """Canonical receipt for the exact empty-scene prefix used by a debt payment."""

    layers: tuple[ReplayPrefixLayerReceipt, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.layers, tuple) or not self.layers:
            raise ValueError("ReplayPrefixReceipt.layers must be non-empty")
        if any(not isinstance(layer, ReplayPrefixLayerReceipt) for layer in self.layers):
            raise ValueError(
                "ReplayPrefixReceipt.layers must contain typed layer receipts"
            )
        layer_ids = [layer.layer_id for layer in self.layers]
        if len(layer_ids) != len(set(layer_ids)):
            raise ValueError("replay receipt contains duplicate layer ids")
        if any(
            layer.finalization_receipt_digest is None
            or layer.payer_claim_id is not None
            for layer in self.layers[:-1]
        ):
            raise ValueError(
                "every strict replay-prefix predecessor must bind its terminal "
                "finalization receipt"
            )
        payer = self.layers[-1]
        if (
            payer.finalization_receipt_digest is not None
            or payer.payer_claim_id is None
        ):
            raise ValueError(
                "the replay-prefix payer must bind its active finalization claim"
            )

    @property
    def unit_digests(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (unit.identity, unit.unit_digest)
            for layer in self.layers
            for unit in layer.units
        )

    @property
    def completion_bindings(self) -> tuple[JudgmentDebtCompletionBinding, ...]:
        return tuple(
            JudgmentDebtCompletionBinding(
                layer_id=unit.layer_id,
                unit_id=unit.unit_id,
                unit_digest=unit.unit_digest,
                completion_receipt_digest=unit.completion_receipt_digest,
            )
            for layer in self.layers
            for unit in layer.units
        )

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            {
                "schema": REPLAY_PREFIX_RECEIPT_SCHEMA,
                "layers": [layer.as_dict() for layer in self.layers],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": REPLAY_PREFIX_RECEIPT_SCHEMA,
            "layers": [layer.as_dict() for layer in self.layers],
            "replay_prefix_digest": self.digest,
        }

    @classmethod
    def from_dict(cls, value: object, where: str) -> ReplayPrefixReceipt:
        expected = {"schema", "layers", "replay_prefix_digest"}
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError(f"{where} has unsupported replay-prefix receipt shape")
        if value.get("schema") != REPLAY_PREFIX_RECEIPT_SCHEMA:
            raise ValueError(
                f"{where}.schema must be {REPLAY_PREFIX_RECEIPT_SCHEMA!r}"
            )
        raw_layers = value.get("layers")
        if not isinstance(raw_layers, list):
            raise ValueError(f"{where}.layers must be a list")
        candidate = cls(
            tuple(
                ReplayPrefixLayerReceipt.from_dict(
                    row,
                    f"{where}.layers[{index}]",
                )
                for index, row in enumerate(raw_layers)
            )
        )
        if value.get("replay_prefix_digest") != candidate.digest:
            raise ValueError(f"{where}.replay_prefix_digest is stale")
        return candidate


def replay_parent_chain_digest(receipt: ReplayPrefixReceipt) -> str:
    """Canonical script-chain identity shared by observation mint and verification."""

    if not isinstance(receipt, ReplayPrefixReceipt):
        raise ValueError("replay parent chain requires a ReplayPrefixReceipt")
    encoded = json.dumps(
        {
            "schema": "vfx-harness.judgment-observation-parent-chain/v1",
            "layers": [
                {
                    "layer_id": layer.layer_id,
                    "script_path": layer.script_path,
                    "script_sha256": layer.script_sha256,
                }
                for layer in receipt.layers
            ],
        },
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def payment_generation_for_replay(
    definition: JudgmentDebtDefinition,
    activation: JudgmentDebtActivation,
    replay_receipt: ReplayPrefixReceipt,
) -> JudgmentDebtPaymentGeneration:
    """Compile the exact receipt generation allowed to carry lifecycle state."""

    if not isinstance(replay_receipt, ReplayPrefixReceipt):
        raise ValueError("judgment debt payment requires a ReplayPrefixReceipt")
    activation.assert_matches(definition)
    validate_judgment_debt_replay_prefix(
        activation,
        replay_receipt.unit_digests,
    )
    replay_by_identity = {
        row.identity: row for row in replay_receipt.completion_bindings
    }
    payer_rows: list[JudgmentDebtCompletionBinding] = []
    for identity, expected_unit_digest in activation.payer_unit_digests:
        row = replay_by_identity.get(identity)
        if row is None or row.unit_digest != expected_unit_digest:
            raise ValueError(
                "judgment debt replay prefix has no exact completion receipt for "
                f"payer unit {identity}"
            )
        payer_rows.append(row)
    generation = JudgmentDebtPaymentGeneration(
        definition_digest=definition.digest,
        activation_digest=activation.digest,
        replay_prefix_digest=replay_receipt.digest,
        replay_completions=tuple(
            sorted(replay_receipt.completion_bindings, key=lambda row: row.identity)
        ),
        payer_completions=tuple(sorted(payer_rows, key=lambda row: row.identity)),
    )
    generation.assert_matches(definition, activation)
    return generation


__all__ = [
    "REPLAY_PREFIX_RECEIPT_SCHEMA",
    "ReplayPrefixLayerReceipt",
    "ReplayPrefixReceipt",
    "ReplayPrefixUnitReceipt",
    "payment_generation_for_replay",
    "replay_parent_chain_digest",
]
