"""Pure source and evidence assembly for synthetic layer composition."""

from __future__ import annotations

import hashlib
import json


def judgment_payment_evidence_digest(
    decision: dict,
    *,
    result: str,
    verdicts: list,
    finding_record_id: str | None = None,
) -> str:
    """Bind a debt outcome to the exact canonical verdict slice that produced it."""

    payload = {
        "schema": "vfx-harness.judgment-debt-payment-evidence/v1",
        "debt_id": decision["debt_id"],
        "definition_digest": decision["definition_digest"],
        "activation_digest": decision["activation_digest"],
        "result": result,
        "verdicts": verdicts,
        "finding_record_id": finding_record_id,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compose_unit_artifact_source(
    parts: list[tuple[str, str, str]],
    *,
    evaluation_barrier: str,
) -> str:
    """Compose unit scripts with evaluated-state publication after every unit."""

    composed = []
    for unit_id, rel, source in parts:
        composed.append(
            f"# --- work unit {unit_id}: {rel} ---\n"
            f"{source.rstrip()}\n\n"
            "# --- publish evaluated unit interface (HIR-0117) ---\n"
            f"{evaluation_barrier.rstrip()}"
        )
    return "\n\n".join(composed).rstrip() + "\n"
