"""Run-scoped evidence from the JIT materialization terminal gate.

The materializer tool used to flatten a dirty :class:`GateResult` into prose and
discard the typed result.  A later max-turn or process stop therefore had no exact
evidence from which to decide whether plan repair was legal.  This module records the
result against the candidate bytes that were actually gated.  It does not classify the
stop; the public planner boundary owns that decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.orchestration.jit_materialization.schema import (
    materialization_finalization_path,
)

if TYPE_CHECKING:
    from vfx_harness.evaluation.plan_gate import GateResult
    from vfx_harness.observability.run_artifacts import RunLayout


MATERIALIZATION_GATE_EVIDENCE_SCHEMA = "vfx-harness.materialization-gate-evidence/v1"
MATERIALIZATION_GATE_REPORT = "materialization-gate-evidence"

_AUDIT_LOCATOR = re.compile(
    r"(?:(?:[A-Za-z]:)?[/\\][^\s,;:)\]}]+)+|"
    r"(?<![\w.-])[\w.-]+\.(?:json|py|md|png|jpe?g|glb|out|txt)(?![\w.-])",
    re.IGNORECASE,
)
_RUN_TOKEN = re.compile(
    r"\b(?:run[-_:]?)?[0-9]{8}T[0-9]{6}Z(?:-[A-Za-z0-9]+)?\b",
    re.IGNORECASE,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_record(path: Path, *, locator: str) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return {"locator": locator, "state": "missing"}
    except OSError:
        return {"locator": locator, "state": "unreadable"}
    return {
        "locator": locator,
        "state": "present",
        "sha256": _sha256(payload),
        "bytes": len(payload),
    }


def _candidate_record(layout: RunLayout, candidate: Path) -> dict[str, Any]:
    path = candidate.expanduser().resolve()
    try:
        locator = path.relative_to(layout.shot).as_posix()
    except ValueError as exc:
        raise ValueError("materialization gate candidate must stay inside the shot") from exc
    record = _file_record(path, locator=locator)
    if record["state"] != "present":
        raise ValueError("materialization gate candidate must be readable")
    return record


def _split_local_finding(value: str) -> tuple[str | None, str]:
    """Separate the validator's RFC 6901 locator from its semantic message.

    ``format_finding`` is the validator's one output contract.  The locator is retained
    for audit and repair, but it is deliberately excluded from the causal fact so list
    positions and filenames cannot change a stop fingerprint.
    """

    text = str(value).strip()
    if text.startswith("/") and ": " in text:
        pointer, message = text.split(": ", 1)
        return pointer, message
    return None, text


def normalized_materialization_message(value: str) -> str:
    """Remove attempt locators from a causal message, retaining semantic facts."""

    without_runs = _RUN_TOKEN.sub("<run>", str(value).strip())
    return _AUDIT_LOCATOR.sub("<locator>", without_runs)


def semantic_materialization_pointer(value: str) -> str | None:
    """Return an RFC 6901 repair target without its run-local artifact prefix."""

    text = str(value).strip()
    if "#/" in text:
        return text[text.index("#") + 1 :]
    if text.startswith("/"):
        return text
    return None


def materialization_local_finding_records(findings: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
    normalized: list[tuple[str | None, str, dict[str, str]]] = []
    for raw in findings:
        pointer, message = _split_local_finding(raw)
        if not message:
            raise ValueError("materialization validation findings must be non-empty")
        if pointer is None:
            raise ValueError(
                "materialization validation findings must carry an RFC 6901 pointer"
            )
        normalized.append(
            (
                pointer,
                message,
                {
                    "validator": "jit-materialization/v2",
                    "pointer": pointer,
                    "message": normalized_materialization_message(message),
                },
            )
        )
    normalized.sort(
        key=lambda row: (
            canonical_digest(
                {
                    "schema": "vfx-harness.materialization-local-causal-fact/v1",
                    "fact": row[2],
                }
            ),
            row[0] or "",
        )
    )
    counters: Counter[str] = Counter()
    records: list[dict[str, Any]] = []
    for pointer, message, causal_fact in normalized:
        fact_digest = canonical_digest(
            {
                "schema": "vfx-harness.materialization-local-causal-fact/v1",
                "fact": causal_fact,
            }
        )
        ordinal = counters[fact_digest]
        counters[fact_digest] += 1
        records.append(
            {
                "finding_id": "materialization-local:"
                + canonical_digest(
                    {
                        "schema": "vfx-harness.materialization-local-finding-id/v1",
                        "causal_fact_digest": fact_digest,
                        "ordinal": ordinal,
                    }
                ),
                "causal_fact": causal_fact,
                "fact": {
                    "message": message,
                    "pointer": pointer,
                },
            }
        )
    return records


def write_materialization_gate_evidence(
    layout: RunLayout,
    *,
    candidate: str | Path,
    bundle_digest: str,
    layer_id: str,
    local_findings: list[str] | tuple[str, ...] = (),
    gate_result: GateResult | None = None,
    gate_issue: str | None = None,
) -> Path:
    """Publish the latest exact terminal attempt for the current candidate revision.

    Exactly one of local findings, a typed terminal gate result, or a closed gate issue
    is recorded.  ``gate_issue`` says only that the typed result could not be produced;
    exception text is intentionally absent.
    """

    options = sum((bool(local_findings), gate_result is not None, gate_issue is not None))
    if options != 1:
        raise ValueError(
            "materialization gate evidence requires exactly one of local_findings, "
            "gate_result, or gate_issue"
        )
    if gate_issue not in {None, "terminal_gate_execution_failed"}:
        raise ValueError("unsupported materialization gate issue")
    candidate_path = Path(candidate)
    candidate_record = _candidate_record(layout, candidate_path)
    finalization = _file_record(
        materialization_finalization_path(candidate_path),
        locator=(candidate_record["locator"] + ".finalization.json"),
    )

    if local_findings:
        phase = "candidate_validation"
        local_records = materialization_local_finding_records(local_findings)
        gate_document = None
    elif gate_result is not None:
        phase = "terminal_gate"
        local_records = []
        gate_document = gate_result.to_dict(
            outcome=gate_result.publishable_outcome if gate_result.clean else "dirty"
        )
    else:
        phase = "gate_unavailable"
        local_records = []
        gate_document = None

    document = {
        "schema": MATERIALIZATION_GATE_EVIDENCE_SCHEMA,
        "bundle_digest": str(bundle_digest),
        "layer_id": str(layer_id),
        "candidate": candidate_record,
        "phase": phase,
        "local_findings": local_records,
        "gate_result": gate_document,
        "gate_issue": gate_issue,
        "finalization": finalization,
    }
    document["record_digest"] = canonical_digest(document)
    path = layout.write_report(MATERIALIZATION_GATE_REPORT, document)
    try:
        observed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("materialization gate evidence did not publish as readable JSON") from exc
    if not isinstance(observed, dict) or observed != document:
        raise RuntimeError("materialization gate evidence failed exact read-back")
    layout.terminal_metadata.update(
        {
            "materialization_gate_evidence": (
                f"reports/{MATERIALIZATION_GATE_REPORT}.json"
            ),
            "materialization_gate_evidence_digest": document["record_digest"],
        }
    )
    return path
