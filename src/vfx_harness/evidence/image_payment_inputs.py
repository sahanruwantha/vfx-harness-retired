"""Immutable image-handle lookup and debt read-back shared by tool transports."""

from __future__ import annotations

import hashlib
from pathlib import Path

from vfx_harness.domain.image_debts import debts_from_dicts, unpaid_image_contract_debts
from vfx_harness.evidence.checks import load_image_contract_payment_rows


def _candidate_for_proposed_check(
    check: dict, default_handle: str | None, registry: dict
) -> tuple[str, dict | None, str]:
    """Resolve one check's frame-local immutable candidate handle."""
    handle = str(check.get("after_handle") or default_handle or "")
    if not handle:
        return "", None, "after_handle is required on the check or at batch level"
    record = registry.get(handle)
    if not isinstance(record, dict) or record.get("role") != "live_candidate":
        available = sorted(
            key for key, value in registry.items() if isinstance(value, dict) and value.get("role") == "live_candidate"
        )[-6:]
        hint = ", ".join(available) or "none — call render_frame first"
        return handle, None, f"unknown current-run candidate handle {handle!r}; recent handles: {hint}"
    return handle, record, ""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _refresh_unpaid_image_debts(
    comparison_state: dict,
    shot_dir: str | Path | None,
    *,
    selected_authority=None,
) -> list[dict]:
    """Recompute unpaid image-contract debts from disk after propose_checks / mutation."""

    cards = debts_from_dicts(comparison_state.get("image_debts"))
    if not cards or not shot_dir:
        comparison_state["unpaid_image_debts"] = []
        return []
    unpaid = [
        card.as_dict()
        for card in unpaid_image_contract_debts(
            cards,
            load_image_contract_payment_rows(
                shot_dir,
                selected_authority=selected_authority,
            ),
        )
    ]
    comparison_state["unpaid_image_debts"] = unpaid
    return unpaid

