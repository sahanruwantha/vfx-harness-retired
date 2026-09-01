"""Strict immutable outcomes produced by canonical work-unit evaluation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.authority_head_records import canonical_json_bytes
from vfx_harness.domain.stop_envelope_primitives import canonical_digest, require_digest
from vfx_harness.domain.unit_attempts import UnitAttemptClaim
from vfx_harness.domain.unit_completion_receipts import normalize_passed_evidence

UNIT_EVALUATION_RECEIPT_SCHEMA = "vfx-harness.work-unit-evaluation-receipt/v1"
PASSING_RESULTS = frozenset({"passed", "reproduced"})
_FIELDS = frozenset(
    {
        "schema",
        "receipt_digest",
        "claim",
        "script_path",
        "script_sha256",
        "replay_inputs",
        "result",
        "canonical",
        "ledger_outcome",
        "ledger_outcome_digest",
        "passed_evidence",
        "candidate_path",
        "candidate_sha256",
        "published_at",
    }
)

_REPLAY_INPUT_FIELDS = frozenset({"script_path", "script_sha256", "dependencies"})
_REPLAY_DEPENDENCY_FIELDS = frozenset({"kind", "path", "sha256"})
_REPLAY_DEPENDENCY_KINDS = frozenset(
    {"construction_pointer", "construction_glb"}
)


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{where} must be a non-empty trimmed string")
    return value


def _relative_path(value: object, where: str) -> str:
    text = _text(value, where)
    path = Path(text)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != text:
        raise ValueError(f"{where} must be a canonical relative path")
    return text


@dataclass(frozen=True, slots=True)
class ReplayDependencyBinding:
    """One non-script byte source consumed while replaying an artifact."""

    kind: str
    path: str
    sha256: str

    @classmethod
    def mint(
        cls,
        *,
        kind: object,
        path: object,
        sha256: object,
        where: str = "unit evaluation replay dependency",
    ) -> ReplayDependencyBinding:
        kind = _text(kind, f"{where}.kind")
        if kind not in _REPLAY_DEPENDENCY_KINDS:
            raise ValueError(
                f"{where}.kind must be one of {sorted(_REPLAY_DEPENDENCY_KINDS)}"
            )
        return cls(
            kind=kind,
            path=_relative_path(path, f"{where}.path"),
            sha256=require_digest(sha256, f"{where}.sha256"),
        )

    @classmethod
    def parse(
        cls,
        value: object,
        where: str = "unit evaluation replay dependency",
    ) -> ReplayDependencyBinding:
        if not isinstance(value, Mapping) or set(value) != _REPLAY_DEPENDENCY_FIELDS:
            raise ValueError(
                f"{where} must contain exactly kind, path, and sha256"
            )
        return cls.mint(
            kind=value.get("kind"),
            path=value.get("path"),
            sha256=value.get("sha256"),
            where=where,
        )

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "path": self.path, "sha256": self.sha256}


def _replay_dependencies(
    value: Iterable[ReplayDependencyBinding],
    where: str,
) -> tuple[ReplayDependencyBinding, ...]:
    if isinstance(value, (str, bytes, Mapping)):
        raise ValueError(f"{where} must be an ordered iterable of typed bindings")
    rows = tuple(value)
    if any(not isinstance(row, ReplayDependencyBinding) for row in rows):
        raise ValueError(f"{where} must contain only typed dependency bindings")
    paths = [row.path for row in rows]
    if len(paths) != len(set(paths)):
        raise ValueError(f"{where} contains duplicate paths")
    kinds = tuple(row.kind for row in rows)
    if kinds not in {(), ("construction_pointer", "construction_glb")}:
        raise ValueError(
            f"{where} must be empty or ordered construction_pointer, construction_glb"
        )
    return rows


@dataclass(frozen=True, slots=True)
class ReplayInputBinding:
    """One ordered canonical script byte-string actually used by replay."""

    script_path: str
    script_sha256: str
    dependencies: tuple[ReplayDependencyBinding, ...] = ()

    @classmethod
    def mint(
        cls,
        *,
        script_path: object,
        script_sha256: object,
        dependencies: Iterable[ReplayDependencyBinding] = (),
        where: str = "unit evaluation replay input",
    ) -> ReplayInputBinding:
        return cls(
            script_path=_relative_path(script_path, f"{where}.script_path"),
            script_sha256=require_digest(
                script_sha256,
                f"{where}.script_sha256",
            ),
            dependencies=_replay_dependencies(
                dependencies,
                f"{where}.dependencies",
            ),
        )

    @classmethod
    def parse(
        cls,
        value: object,
        where: str = "unit evaluation replay input",
    ) -> ReplayInputBinding:
        if not isinstance(value, Mapping) or set(value) != _REPLAY_INPUT_FIELDS:
            raise ValueError(
                f"{where} must contain exactly script_path, script_sha256, and dependencies"
            )
        raw_dependencies = value.get("dependencies")
        if not isinstance(raw_dependencies, list):
            raise ValueError(f"{where}.dependencies must be a list")
        return cls.mint(
            script_path=value.get("script_path"),
            script_sha256=value.get("script_sha256"),
            dependencies=tuple(
                ReplayDependencyBinding.parse(
                    row,
                    f"{where}.dependencies[{index}]",
                )
                for index, row in enumerate(raw_dependencies)
            ),
            where=where,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "script_path": self.script_path,
            "script_sha256": self.script_sha256,
            "dependencies": [row.as_dict() for row in self.dependencies],
        }


def _replay_inputs(
    value: Iterable[ReplayInputBinding],
    where: str,
) -> tuple[ReplayInputBinding, ...]:
    if isinstance(value, (str, bytes, Mapping)):
        raise ValueError(f"{where} must be an ordered iterable of typed bindings")
    rows = tuple(value)
    if not rows:
        raise ValueError(f"{where} must not be empty")
    if any(not isinstance(row, ReplayInputBinding) for row in rows):
        raise ValueError(f"{where} must contain only typed replay input bindings")
    paths = [row.script_path for row in rows]
    if len(paths) != len(set(paths)):
        raise ValueError(f"{where} contains duplicate script paths")
    return rows


def _canonical_rows(value: object, where: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{where} must be a non-empty list")
    rows: list[dict[str, Any]] = []
    frames: set[int] = set()
    for index, raw in enumerate(value):
        row_where = f"{where}[{index}]"
        if not isinstance(raw, Mapping) or set(raw) != {"frame", "ref", "verdict"}:
            raise ValueError(f"{row_where} must contain exactly frame, ref, and verdict")
        frame = raw.get("frame")
        if isinstance(frame, bool) or not isinstance(frame, int) or frame < 1:
            raise ValueError(f"{row_where}.frame must be a positive integer")
        if frame in frames:
            raise ValueError(f"{where} contains duplicate frame {frame}")
        frames.add(frame)
        ref = _relative_path(raw.get("ref"), f"{row_where}.ref")
        verdict = raw.get("verdict")
        if not isinstance(verdict, Mapping):
            raise ValueError(f"{row_where}.verdict must be an object")
        verdict = dict(verdict)
        if verdict.get("pass") is not True:
            raise ValueError(f"{row_where}.verdict must be an independently passing verdict")
        evidence = verdict.get("evidence")
        if not isinstance(evidence, list):
            raise ValueError(f"{row_where}.verdict.evidence must be a list")
        # This also rejects non-finite or otherwise unserializable evaluator output.
        canonical_json_bytes(verdict)
        rows.append({"frame": frame, "ref": ref, "verdict": verdict})
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class UnitEvaluationReceipt:
    """Exact canonical evaluator output for one exclusive build claim."""

    receipt_digest: str
    claim: UnitAttemptClaim
    script_path: str
    script_sha256: str
    replay_inputs: tuple[ReplayInputBinding, ...]
    result: str
    canonical: tuple[dict[str, Any], ...]
    ledger_outcome: dict[str, Any]
    ledger_outcome_digest: str
    passed_evidence: tuple[tuple[str, str], ...]
    candidate_path: str | None
    candidate_sha256: str | None
    published_at: str

    @classmethod
    def mint(
        cls,
        *,
        claim: UnitAttemptClaim,
        script_path: str,
        script_sha256: str,
        replay_inputs: Iterable[ReplayInputBinding],
        result: str,
        canonical: Iterable[Mapping[str, Any]],
        ledger_outcome: Mapping[str, Any],
        passed_evidence: Iterable[tuple[str, str]],
        candidate_path: str | None,
        candidate_sha256: str | None,
        published_at: str,
    ) -> UnitEvaluationReceipt:
        if not isinstance(claim, UnitAttemptClaim) or claim.phase != "building":
            raise ValueError("unit evaluation receipt requires an exact building claim")
        script_path = _relative_path(script_path, "unit evaluation script_path")
        script_sha256 = require_digest(script_sha256, "unit evaluation script_sha256")
        replay = _replay_inputs(replay_inputs, "unit evaluation replay_inputs")
        if (
            replay[-1].script_path != script_path
            or replay[-1].script_sha256 != script_sha256
        ):
            raise ValueError(
                "unit evaluation replay_inputs must end with the exact canonical "
                "unit script binding"
            )
        if result not in PASSING_RESULTS:
            raise ValueError(
                f"unit evaluation result must be one of {sorted(PASSING_RESULTS)}"
            )
        rows = _canonical_rows(list(canonical), "unit evaluation canonical")
        if not isinstance(ledger_outcome, Mapping):
            raise ValueError("unit evaluation ledger_outcome must be an object")
        ledger = dict(ledger_outcome)
        canonical_json_bytes(ledger)
        ledger_digest = canonical_digest(ledger)
        evidence = normalize_passed_evidence(passed_evidence, "unit evaluation passed_evidence")
        if (candidate_path is None) != (candidate_sha256 is None):
            raise ValueError(
                "unit evaluation candidate_path and candidate_sha256 must both be present or absent"
            )
        if candidate_path is not None:
            candidate_path = _relative_path(candidate_path, "unit evaluation candidate_path")
            candidate_sha256 = require_digest(
                candidate_sha256,
                "unit evaluation candidate_sha256",
            )
        published_at = _text(published_at, "unit evaluation published_at")
        identity = {
            "schema": UNIT_EVALUATION_RECEIPT_SCHEMA,
            "claim": claim.as_dict(),
            "script_path": script_path,
            "script_sha256": script_sha256,
            "replay_inputs": [row.as_dict() for row in replay],
            "result": result,
            "canonical": list(rows),
            "ledger_outcome": ledger,
            "ledger_outcome_digest": ledger_digest,
            "passed_evidence": [
                {"kind": kind, "id": identifier} for kind, identifier in evidence
            ],
            "candidate_path": candidate_path,
            "candidate_sha256": candidate_sha256,
            "published_at": published_at,
        }
        return cls(
            receipt_digest=canonical_digest(identity),
            claim=claim,
            script_path=script_path,
            script_sha256=script_sha256,
            replay_inputs=replay,
            result=result,
            canonical=rows,
            ledger_outcome=ledger,
            ledger_outcome_digest=ledger_digest,
            passed_evidence=evidence,
            candidate_path=candidate_path,
            candidate_sha256=candidate_sha256,
            published_at=published_at,
        )

    @classmethod
    def parse(
        cls,
        value: object,
        where: str = "unit evaluation receipt",
    ) -> UnitEvaluationReceipt:
        if not isinstance(value, Mapping):
            raise ValueError(f"{where} must be an object")
        found = set(value)
        if found != _FIELDS:
            raise ValueError(
                f"{where} fields mismatch; missing={sorted(_FIELDS - found)}; "
                f"unexpected={sorted(found - _FIELDS)}"
            )
        if value.get("schema") != UNIT_EVALUATION_RECEIPT_SCHEMA:
            raise ValueError(
                f"{where}.schema must be {UNIT_EVALUATION_RECEIPT_SCHEMA!r}"
            )
        claim = UnitAttemptClaim.parse(value.get("claim"), f"{where}.claim")
        receipt = cls.mint(
            claim=claim,
            script_path=value.get("script_path"),
            script_sha256=value.get("script_sha256"),
            replay_inputs=(
                tuple(
                    ReplayInputBinding.parse(
                        row,
                        f"{where}.replay_inputs[{index}]",
                    )
                    for index, row in enumerate(value.get("replay_inputs"))
                )
                if isinstance(value.get("replay_inputs"), list)
                else ()
            ),
            result=value.get("result"),
            canonical=value.get("canonical"),
            ledger_outcome=value.get("ledger_outcome"),
            passed_evidence=[
                (row.get("kind"), row.get("id"))
                if isinstance(row, Mapping)
                else row
                for row in (
                    value.get("passed_evidence")
                    if isinstance(value.get("passed_evidence"), list)
                    else []
                )
            ],
            candidate_path=value.get("candidate_path"),
            candidate_sha256=value.get("candidate_sha256"),
            published_at=value.get("published_at"),
        )
        observed = require_digest(value.get("receipt_digest"), f"{where}.receipt_digest")
        if observed != receipt.receipt_digest:
            raise ValueError(f"{where}.receipt_digest does not match its exact payload")
        if value.get("ledger_outcome_digest") != receipt.ledger_outcome_digest:
            raise ValueError(f"{where}.ledger_outcome_digest does not match its exact payload")
        return receipt

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": UNIT_EVALUATION_RECEIPT_SCHEMA,
            "receipt_digest": self.receipt_digest,
            "claim": self.claim.as_dict(),
            "script_path": self.script_path,
            "script_sha256": self.script_sha256,
            "replay_inputs": [row.as_dict() for row in self.replay_inputs],
            "result": self.result,
            "canonical": list(self.canonical),
            "ledger_outcome": dict(self.ledger_outcome),
            "ledger_outcome_digest": self.ledger_outcome_digest,
            "passed_evidence": [
                {"kind": kind, "id": identifier}
                for kind, identifier in self.passed_evidence
            ],
            "candidate_path": self.candidate_path,
            "candidate_sha256": self.candidate_sha256,
            "published_at": self.published_at,
        }
