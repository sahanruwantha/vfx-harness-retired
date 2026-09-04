"""What an evidence row may settle: a requirement someone bound, or an unbound veto.

Two different questions were carried by one boolean field.  ``authoritative`` on an
emitted evidence row answers the first: *may this row block acceptance on its own,
when nobody bound it?*  A builder-authored image check must not — it would be marking
its own homework — so ``evidence/checks.py`` deliberately mints those rows with the
flag false.

Consumers that close over a unit's REQUIRED claim bindings ask a different question:
*was the exact id this claim names produced, and did it pass?*  Boundness there is
already established by the claim itself, so autonomy is not the property in question.
Reading the autonomy flag for that question silently discarded every builder-paid
image row — the only mechanism that can discharge a required image contract — and no
unit with a required image binding could publish an outcome (HIR-0205).

The two questions now have two names.  A consumer picks the one it means.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The one mapping from an emitted row's ``source`` to the evidence family a claim
#: binding names.  A row whose source is absent from this map is not typed evidence.
EVIDENCE_FAMILY_BY_SOURCE = {
    "interface_contract": "scene_contract",
    "scene_contract": "scene_contract",
    "image_contract": "image_contract",
    "semantic_diff": "semantic_diff",
    "qualification": "qualification",
    "human_decision": "human_decision",
}


def evidence_family(row: Any) -> str | None:
    """The claim-binding family this row can certify, or ``None`` if it is untyped."""
    if not isinstance(row, Mapping) or not row.get("id"):
        return None
    return EVIDENCE_FAMILY_BY_SOURCE.get(str(row.get("source") or ""))


def is_typed_measurement(row: Any) -> bool:
    """The row is an identified measurement in a family a claim can bind, pass or fail."""
    return evidence_family(row) is not None


def settles_bound_requirement(row: Any) -> bool:
    """The row produced a passing reading of an id something already bound.

    Autonomy is deliberately not consulted: the binding, not the row, supplies the
    authority to require this measurement.  A valid v2 builder payment carries a
    harness-selected adversary and hash-pinned provenance before it ever reaches an
    evaluator (ADR-0008, HIR-0053), so accepting it here is not self-certification.
    """
    return is_typed_measurement(row) and row.get("pass") is True


def blocks_without_a_binding(row: Any) -> bool:
    """The row may veto acceptance although nothing bound it to a claim."""
    return isinstance(row, Mapping) and row.get("authoritative") is True


def is_recorded_evidence(row: Any) -> bool:
    """A real reading of an id something already bound, or already an autonomous one.

    Autonomy is sufficient but never necessary here.  Where boundness is established
    elsewhere -- by the claim that cites the id, or by the outcome being sealed on it --
    an unflagged typed measurement is just as real a reading, and requiring the flag
    dropped every builder-paid image row.  A row that is neither typed nor autonomous is
    unclassified noise and decides nothing.
    """
    return is_typed_measurement(row) or blocks_without_a_binding(row)
