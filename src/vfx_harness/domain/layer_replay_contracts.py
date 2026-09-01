"""Immutable cumulative-replay receipt for one layer-finalization claim."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from vfx_harness.domain.layer_finalization_claims import (
    LayerFinalizationClaim,
    _text,
)
from vfx_harness.domain.layer_finalization_values import semantic_digest
from vfx_harness.domain.layer_replay_observations import LayerReplayObservation
from vfx_harness.domain.stop_envelope_primitives import require_digest
from vfx_harness.domain.unit_evaluation_receipts import ReplayInputBinding

LAYER_REPLAY_RECEIPT_SCHEMA = "vfx-harness.layer-replay-receipt/v2"
LAYER_REPLAY_STATES = frozenset({"ready"})

_REPLAY_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "receipt_digest",
        "claim",
        "layer_script_sha256",
        "replay_inputs",
        "observation",
        "replay_status",
        "created_at",
    }
)


def _replay_inputs(
    value: Iterable[ReplayInputBinding],
    where: str,
) -> tuple[ReplayInputBinding, ...]:
    if isinstance(value, (str, bytes, Mapping)):
        raise ValueError(f"{where} must be an ordered iterable of typed inputs")
    rows = tuple(value)
    if not rows or any(not isinstance(row, ReplayInputBinding) for row in rows):
        raise ValueError(f"{where} must contain typed replay inputs and must not be empty")
    paths = [row.script_path for row in rows]
    if len(paths) != len(set(paths)):
        raise ValueError(f"{where} contains duplicate script paths")
    return rows


@dataclass(frozen=True, slots=True)
class LayerReplayReceipt:
    """Immutable pre-judgment cumulative replay for one layer claim."""

    receipt_digest: str
    claim: LayerFinalizationClaim
    layer_script_sha256: str
    replay_inputs: tuple[ReplayInputBinding, ...]
    observation: LayerReplayObservation
    replay_status: str
    created_at: str

    @classmethod
    def mint(
        cls,
        *,
        claim: LayerFinalizationClaim,
        layer_script_sha256: object,
        replay_inputs: Iterable[ReplayInputBinding],
        observation: LayerReplayObservation,
        replay_status: object = "ready",
        created_at: object,
    ) -> LayerReplayReceipt:
        if not isinstance(claim, LayerFinalizationClaim):
            raise ValueError("layer replay receipt requires a typed finalization claim")
        script_sha256 = require_digest(
            layer_script_sha256,
            "layer replay receipt layer_script_sha256",
        )
        if script_sha256 != claim.layer_script_sha256:
            raise ValueError(
                "layer replay receipt layer_script_sha256 must match the "
                "proposed composed bytes bound by its finalization claim"
            )
        inputs = _replay_inputs(replay_inputs, "layer replay receipt replay_inputs")
        expected_inputs = (
            *((row.script_path, row.script_sha256) for row in claim.predecessor_inputs),
            (claim.layer_script_path, claim.layer_script_sha256),
        )
        observed_inputs = tuple((row.script_path, row.script_sha256) for row in inputs)
        if observed_inputs != expected_inputs:
            raise ValueError(
                "layer replay receipt replay_inputs must be the exact ordered "
                "predecessor prefix followed by the claimed layer script"
            )
        if not isinstance(observation, LayerReplayObservation):
            raise ValueError(
                "layer replay receipt observation must be a typed "
                "LayerReplayObservation"
            )
        observation.assert_matches_claim(claim, inputs)
        if replay_status not in LAYER_REPLAY_STATES:
            raise ValueError(f"layer replay receipt replay_status must be one of {sorted(LAYER_REPLAY_STATES)}")
        at = _text(created_at, "layer replay receipt created_at")
        identity = {
            "schema": LAYER_REPLAY_RECEIPT_SCHEMA,
            "claim": claim.as_dict(),
            "layer_script_sha256": script_sha256,
            "replay_inputs": [row.as_dict() for row in inputs],
            "observation": observation.as_dict(),
            "replay_status": replay_status,
        }
        return cls(
            receipt_digest=semantic_digest(identity),
            claim=claim,
            layer_script_sha256=script_sha256,
            replay_inputs=inputs,
            observation=observation,
            replay_status=str(replay_status),
            created_at=at,
        )

    @classmethod
    def parse(
        cls,
        value: object,
        where: str = "layer replay receipt",
    ) -> LayerReplayReceipt:
        if not isinstance(value, Mapping):
            raise ValueError(f"{where} must be an object")
        found = set(value)
        if found != _REPLAY_RECEIPT_FIELDS:
            raise ValueError(
                f"{where} fields mismatch; missing={sorted(_REPLAY_RECEIPT_FIELDS - found)}; "
                f"unexpected={sorted(found - _REPLAY_RECEIPT_FIELDS)}"
            )
        if value.get("schema") != LAYER_REPLAY_RECEIPT_SCHEMA:
            raise ValueError(f"{where}.schema must be {LAYER_REPLAY_RECEIPT_SCHEMA!r}")
        raw_inputs = value.get("replay_inputs")
        if not isinstance(raw_inputs, list):
            raise ValueError(f"{where}.replay_inputs must be a list")
        receipt = cls.mint(
            claim=LayerFinalizationClaim.parse(value.get("claim"), f"{where}.claim"),
            layer_script_sha256=value.get("layer_script_sha256"),
            replay_inputs=tuple(
                ReplayInputBinding.parse(
                    row,
                    f"{where}.replay_inputs[{index}]",
                )
                for index, row in enumerate(raw_inputs)
            ),
            observation=LayerReplayObservation.from_dict(
                value.get("observation"),
                f"{where}.observation",
            ),
            replay_status=value.get("replay_status"),
            created_at=value.get("created_at"),
        )
        observed = require_digest(
            value.get("receipt_digest"),
            f"{where}.receipt_digest",
        )
        if observed != receipt.receipt_digest:
            raise ValueError(f"{where}.receipt_digest does not match its exact payload")
        return receipt

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": LAYER_REPLAY_RECEIPT_SCHEMA,
            "receipt_digest": self.receipt_digest,
            "claim": self.claim.as_dict(),
            "layer_script_sha256": self.layer_script_sha256,
            "replay_inputs": [row.as_dict() for row in self.replay_inputs],
            "observation": self.observation.as_dict(),
            "replay_status": self.replay_status,
            "created_at": self.created_at,
        }


def canonical_layer_replay_receipt_bytes(receipt: LayerReplayReceipt) -> bytes:
    """Return the only accepted durable byte representation of a replay receipt."""

    if not isinstance(receipt, LayerReplayReceipt):
        raise ValueError("receipt must be a typed layer replay receipt")
    return (
        json.dumps(
            receipt.as_dict(),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


__all__ = [
    "LAYER_REPLAY_RECEIPT_SCHEMA",
    "LAYER_REPLAY_STATES",
    "LayerReplayReceipt",
    "canonical_layer_replay_receipt_bytes",
]
