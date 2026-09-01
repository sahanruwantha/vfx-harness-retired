"""Pure identity contracts for one exact layer-finalization attempt."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from vfx_harness.domain.authority_head_records import (
    AUTHORITY_SELECTION_TOKEN_SCHEMA,
    AuthorityHeadRecordError,
    AuthoritySelectionTokenProjection,
    parse_authority_selection_token,
)
from vfx_harness.domain.run_ids import require_run_id
from vfx_harness.domain.stop_envelope_primitives import canonical_digest, require_digest

LAYER_FINALIZATION_CLAIM_SCHEMA = "vfx-harness.layer-finalization-claim/v1"
LAYER_FINALIZATION_MODES = frozenset({"singleton_passthrough", "multi_unit_fan_in"})

_UNIT_INPUT_FIELDS = frozenset(
    {
        "unit_id",
        "unit_digest",
        "completion_receipt_digest",
        "script_path",
        "script_sha256",
    }
)
_PREDECESSOR_INPUT_FIELDS = frozenset(
    {
        "layer_id",
        "finalization_receipt_digest",
        "script_path",
        "script_sha256",
    }
)
_CLAIM_FIELDS = frozenset(
    {
        "schema",
        "claim_id",
        "attempt_revision",
        "run_id",
        "layer_id",
        "mode",
        "selection_token",
        "plan_hash",
        "layer_script_path",
        "layer_script_sha256",
        "unit_inputs",
        "predecessor_inputs",
        "claimed_at",
    }
)


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{where} must be a non-empty trimmed string")
    return value


def _relative_path(value: object, where: str) -> str:
    text = _text(value, where)
    path = PurePosixPath(text)
    if path.is_absolute() or text == "." or ".." in path.parts or "\\" in text or path.as_posix() != text:
        raise ValueError(f"{where} must be a canonical relative POSIX path")
    return text


def _selection_token_dict(
    token: AuthoritySelectionTokenProjection,
) -> dict[str, Any]:
    return {
        "schema": AUTHORITY_SELECTION_TOKEN_SCHEMA,
        "plan_revision": token.plan_revision,
        "plan_pointer_sha256": token.plan_pointer_sha256,
        "jit_revision": token.jit_revision,
        "jit_pointer_sha256": token.jit_pointer_sha256,
    }


def _parse_selection_token(
    value: object,
    where: str,
) -> AuthoritySelectionTokenProjection:
    try:
        return parse_authority_selection_token(value, where)
    except AuthorityHeadRecordError as exc:
        raise ValueError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class LayerFinalizationUnitInput:
    """One exact accepted unit consumed by layer composition."""

    unit_id: str
    unit_digest: str
    completion_receipt_digest: str
    script_path: str
    script_sha256: str

    @classmethod
    def mint(
        cls,
        *,
        unit_id: object,
        unit_digest: object,
        completion_receipt_digest: object,
        script_path: object,
        script_sha256: object,
        where: str = "layer finalization unit input",
    ) -> LayerFinalizationUnitInput:
        return cls(
            unit_id=_text(unit_id, f"{where}.unit_id"),
            unit_digest=require_digest(unit_digest, f"{where}.unit_digest"),
            completion_receipt_digest=require_digest(
                completion_receipt_digest,
                f"{where}.completion_receipt_digest",
            ),
            script_path=_relative_path(script_path, f"{where}.script_path"),
            script_sha256=require_digest(
                script_sha256,
                f"{where}.script_sha256",
            ),
        )

    @classmethod
    def parse(
        cls,
        value: object,
        where: str = "layer finalization unit input",
    ) -> LayerFinalizationUnitInput:
        if not isinstance(value, Mapping) or set(value) != _UNIT_INPUT_FIELDS:
            raise ValueError(f"{where} must contain exactly {sorted(_UNIT_INPUT_FIELDS)}")
        return cls.mint(
            unit_id=value.get("unit_id"),
            unit_digest=value.get("unit_digest"),
            completion_receipt_digest=value.get("completion_receipt_digest"),
            script_path=value.get("script_path"),
            script_sha256=value.get("script_sha256"),
            where=where,
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "unit_id": self.unit_id,
            "unit_digest": self.unit_digest,
            "completion_receipt_digest": self.completion_receipt_digest,
            "script_path": self.script_path,
            "script_sha256": self.script_sha256,
        }


@dataclass(frozen=True, slots=True)
class LayerFinalizationPredecessorInput:
    """One exact finalized predecessor layer consumed by cumulative replay."""

    layer_id: str
    finalization_receipt_digest: str
    script_path: str
    script_sha256: str

    @classmethod
    def mint(
        cls,
        *,
        layer_id: object,
        finalization_receipt_digest: object,
        script_path: object,
        script_sha256: object,
        where: str = "layer finalization predecessor input",
    ) -> LayerFinalizationPredecessorInput:
        return cls(
            layer_id=_text(layer_id, f"{where}.layer_id"),
            finalization_receipt_digest=require_digest(
                finalization_receipt_digest,
                f"{where}.finalization_receipt_digest",
            ),
            script_path=_relative_path(script_path, f"{where}.script_path"),
            script_sha256=require_digest(
                script_sha256,
                f"{where}.script_sha256",
            ),
        )

    @classmethod
    def parse(
        cls,
        value: object,
        where: str = "layer finalization predecessor input",
    ) -> LayerFinalizationPredecessorInput:
        if not isinstance(value, Mapping) or set(value) != _PREDECESSOR_INPUT_FIELDS:
            raise ValueError(f"{where} must contain exactly {sorted(_PREDECESSOR_INPUT_FIELDS)}")
        return cls.mint(
            layer_id=value.get("layer_id"),
            finalization_receipt_digest=value.get("finalization_receipt_digest"),
            script_path=value.get("script_path"),
            script_sha256=value.get("script_sha256"),
            where=where,
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "layer_id": self.layer_id,
            "finalization_receipt_digest": self.finalization_receipt_digest,
            "script_path": self.script_path,
            "script_sha256": self.script_sha256,
        }


def _unit_inputs(
    value: Iterable[LayerFinalizationUnitInput],
    where: str,
) -> tuple[LayerFinalizationUnitInput, ...]:
    if isinstance(value, (str, bytes, Mapping)):
        raise ValueError(f"{where} must be an ordered iterable of typed inputs")
    rows = tuple(value)
    if not rows or any(not isinstance(row, LayerFinalizationUnitInput) for row in rows):
        raise ValueError(f"{where} must contain typed unit inputs and must not be empty")
    ids = [row.unit_id for row in rows]
    paths = [row.script_path for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{where} contains duplicate unit ids")
    if len(paths) != len(set(paths)):
        raise ValueError(f"{where} contains duplicate script paths")
    return rows


def _predecessor_inputs(
    value: Iterable[LayerFinalizationPredecessorInput],
    where: str,
) -> tuple[LayerFinalizationPredecessorInput, ...]:
    if isinstance(value, (str, bytes, Mapping)):
        raise ValueError(f"{where} must be an ordered iterable of typed inputs")
    rows = tuple(value)
    if any(not isinstance(row, LayerFinalizationPredecessorInput) for row in rows):
        raise ValueError(f"{where} must contain only typed predecessor inputs")
    ids = [row.layer_id for row in rows]
    paths = [row.script_path for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{where} contains duplicate layer ids")
    if len(paths) != len(set(paths)):
        raise ValueError(f"{where} contains duplicate script paths")
    return rows


@dataclass(frozen=True, slots=True)
class LayerFinalizationClaim:
    """Exclusive claim over the exact inputs to one layer boundary attempt."""

    claim_id: str
    attempt_revision: int
    run_id: str
    layer_id: str
    mode: str
    selection_token: AuthoritySelectionTokenProjection
    plan_hash: str
    layer_script_path: str
    layer_script_sha256: str
    unit_inputs: tuple[LayerFinalizationUnitInput, ...]
    predecessor_inputs: tuple[LayerFinalizationPredecessorInput, ...]
    claimed_at: str

    @classmethod
    def mint(
        cls,
        *,
        attempt_revision: object,
        run_id: object,
        layer_id: object,
        mode: object,
        selection_token: AuthoritySelectionTokenProjection,
        plan_hash: object,
        layer_script_path: object,
        layer_script_sha256: object,
        unit_inputs: Iterable[LayerFinalizationUnitInput],
        predecessor_inputs: Iterable[LayerFinalizationPredecessorInput],
        claimed_at: object,
    ) -> LayerFinalizationClaim:
        if not isinstance(attempt_revision, int) or isinstance(attempt_revision, bool) or attempt_revision <= 0:
            raise ValueError("layer finalization attempt_revision must be a positive integer")
        run = require_run_id(run_id, "layer finalization run_id")
        layer = _text(layer_id, "layer finalization layer_id")
        if mode not in LAYER_FINALIZATION_MODES:
            raise ValueError(f"layer finalization mode must be one of {sorted(LAYER_FINALIZATION_MODES)}")
        if not isinstance(selection_token, AuthoritySelectionTokenProjection):
            raise ValueError("layer finalization selection_token must be an exact typed projection")
        token = _parse_selection_token(
            _selection_token_dict(selection_token),
            "layer finalization selection_token",
        )
        plan = require_digest(plan_hash, "layer finalization plan_hash")
        script_path = _relative_path(
            layer_script_path,
            "layer finalization layer_script_path",
        )
        script_sha256 = require_digest(
            layer_script_sha256,
            "layer finalization layer_script_sha256",
        )
        units = _unit_inputs(unit_inputs, "layer finalization unit_inputs")
        predecessors = _predecessor_inputs(
            predecessor_inputs,
            "layer finalization predecessor_inputs",
        )
        if mode == "singleton_passthrough" and len(units) != 1:
            raise ValueError("singleton_passthrough layer finalization requires exactly one unit input")
        if mode == "multi_unit_fan_in" and len(units) < 2:
            raise ValueError("multi_unit_fan_in layer finalization requires at least two unit inputs")
        if layer in {row.layer_id for row in predecessors}:
            raise ValueError("layer finalization cannot name itself as a predecessor")
        source_paths = [row.script_path for row in (*predecessors, *units)]
        if len(source_paths) != len(set(source_paths)):
            raise ValueError("layer finalization unit and predecessor script paths must be disjoint")
        if script_path in source_paths:
            raise ValueError("layer finalization layer script must be distinct from every source script")
        identity = {
            "schema": LAYER_FINALIZATION_CLAIM_SCHEMA,
            "attempt_revision": attempt_revision,
            "run_id": run,
            "layer_id": layer,
            "mode": mode,
            "selection_token": _selection_token_dict(token),
            "plan_hash": plan,
            "layer_script_path": script_path,
            "layer_script_sha256": script_sha256,
            "unit_inputs": [row.as_dict() for row in units],
            "predecessor_inputs": [row.as_dict() for row in predecessors],
        }
        return cls(
            claim_id=f"lfc-{canonical_digest(identity)}",
            attempt_revision=attempt_revision,
            run_id=run,
            layer_id=layer,
            mode=str(mode),
            selection_token=token,
            plan_hash=plan,
            layer_script_path=script_path,
            layer_script_sha256=script_sha256,
            unit_inputs=units,
            predecessor_inputs=predecessors,
            claimed_at=_text(claimed_at, "layer finalization claimed_at"),
        )

    @classmethod
    def parse(
        cls,
        value: object,
        where: str = "layer finalization claim",
    ) -> LayerFinalizationClaim:
        if not isinstance(value, Mapping):
            raise ValueError(f"{where} must be an object")
        found = set(value)
        if found != _CLAIM_FIELDS:
            raise ValueError(
                f"{where} fields mismatch; missing={sorted(_CLAIM_FIELDS - found)}; "
                f"unexpected={sorted(found - _CLAIM_FIELDS)}"
            )
        if value.get("schema") != LAYER_FINALIZATION_CLAIM_SCHEMA:
            raise ValueError(f"{where}.schema must be {LAYER_FINALIZATION_CLAIM_SCHEMA!r}")
        raw_units = value.get("unit_inputs")
        if not isinstance(raw_units, list):
            raise ValueError(f"{where}.unit_inputs must be a list")
        raw_predecessors = value.get("predecessor_inputs")
        if not isinstance(raw_predecessors, list):
            raise ValueError(f"{where}.predecessor_inputs must be a list")
        claim = cls.mint(
            attempt_revision=value.get("attempt_revision"),
            run_id=value.get("run_id"),
            layer_id=value.get("layer_id"),
            mode=value.get("mode"),
            selection_token=_parse_selection_token(
                value.get("selection_token"),
                f"{where}.selection_token",
            ),
            plan_hash=value.get("plan_hash"),
            layer_script_path=value.get("layer_script_path"),
            layer_script_sha256=value.get("layer_script_sha256"),
            unit_inputs=tuple(
                LayerFinalizationUnitInput.parse(
                    row,
                    f"{where}.unit_inputs[{index}]",
                )
                for index, row in enumerate(raw_units)
            ),
            predecessor_inputs=tuple(
                LayerFinalizationPredecessorInput.parse(
                    row,
                    f"{where}.predecessor_inputs[{index}]",
                )
                for index, row in enumerate(raw_predecessors)
            ),
            claimed_at=value.get("claimed_at"),
        )
        if _text(value.get("claim_id"), f"{where}.claim_id") != claim.claim_id:
            raise ValueError(f"{where}.claim_id does not match its exact identity")
        return claim

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": LAYER_FINALIZATION_CLAIM_SCHEMA,
            "claim_id": self.claim_id,
            "attempt_revision": self.attempt_revision,
            "run_id": self.run_id,
            "layer_id": self.layer_id,
            "mode": self.mode,
            "selection_token": _selection_token_dict(self.selection_token),
            "plan_hash": self.plan_hash,
            "layer_script_path": self.layer_script_path,
            "layer_script_sha256": self.layer_script_sha256,
            "unit_inputs": [row.as_dict() for row in self.unit_inputs],
            "predecessor_inputs": [row.as_dict() for row in self.predecessor_inputs],
            "claimed_at": self.claimed_at,
        }


__all__ = [
    "LAYER_FINALIZATION_CLAIM_SCHEMA",
    "LAYER_FINALIZATION_MODES",
    "LayerFinalizationClaim",
    "LayerFinalizationPredecessorInput",
    "LayerFinalizationUnitInput",
]
