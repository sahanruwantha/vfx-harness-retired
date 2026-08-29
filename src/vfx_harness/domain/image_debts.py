"""Compiled unpaid image-contract debts (HIR-0048).

HIR-0046/0047 made ``asserts: image`` + ``image_contract`` ids legal at plan time
while ``checks.json`` stayed empty. Every consumer then guessed: the live probe
treated the ids as scene-selector misses, the unit plan called them critic
comparison, canonical repair retagged nodes, and ``cannot_express`` reused the
HIR-0031 interpolation template.

The claim already carries id, axis, moments, and property. This module is the
one compiler for that card. Payment is ``propose_checks`` on the live plate;
canonical repair cannot author evaluation contracts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

CHECK_PREFIX = "check:"

# Claim.property → metric-id prefix in the canonical ``METRICS`` registry
# (evidence/checks.py). Domain cannot import that registry (PIL). Membership
# is the prefix; the architecture test pins it to the live keys (ADR-0003).
IMAGE_PROPERTY_PREFIXES: dict[str, str] = {
    # Runtime payments are already proved against the pre-unit adversary by
    # ``propose_checks``.  A scalar ``frame_*`` threshold that passes only after
    # the unit therefore proves an observable frame delta; requiring a literal
    # ``frame_delta`` metric made the debt impossible because the canonical image
    # registry intentionally exposes scalar measurements, not pair-valued checks.
    "frame_delta": "frame_",
    "render_region_stat": "region_",
}

IMAGE_PROPERTY_VOCABULARY_RULE = (
    "an image-domain claim property must be payable by the canonical image-metric "
    "registry before the unit is published. Use frame_delta for any candidate-only "
    "frame scalar, render_region_stat for a region scalar, or one exact registered "
    "metric id. Free-form appearance labels belong in the proposition, not property"
)

UNPAID_IMAGE_DEBT = "unpaid_image_debt"
UNSATISFIABLE_IN_SCOPE = "unsatisfiable_in_scope"

UNPAID_IMAGE_DEBT_RULE = (
    "an owed image_contract id with no checks.json or runtime_checks.json row is "
    "an unpaid image-contract debt. Call propose_checks with that exact id, frame, "
    "property kind, and axis on an existing run render, or cannot_express_in_scope "
    "as unpaid_image_debt. A role or control retag cannot produce the row. "
    "Candidate freeze refuses while any owed id is unpaid and not abstained. "
    "Canonical repair cannot author evaluation contracts."
)

UNPAID_IMAGE_DEBT_AUTHORITY = (
    "pay the named image_contract debts with propose_checks on the live plate "
    "(exact id, frame, property kind, axis), or amend the claim; vfx units replan "
    "consumes the finding. Canonical repair cannot author evaluation contracts"
)

UNSATISFIABLE_PAIR_AUTHORITY = (
    "amend the named contracts so they are jointly satisfiable, then "
    "vfx units replan; builders cannot invent a third interpolation"
)


@dataclass(frozen=True, slots=True)
class ImageContractDebt:
    """One owed image-contract payment at one judge moment."""

    id: str
    axis: str
    frame: int
    property: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "axis": self.axis,
            "frame": self.frame,
            "property": self.property,
        }


@dataclass(frozen=True, slots=True)
class ImagePropertyVocabularyGap:
    """One required image claim whose property has no executable payer."""

    unit_id: str
    claim_id: str
    property: str
    contract_ids: tuple[str, ...]


def normalize_evidence_id(raw: Any) -> str:
    """Bare claim id. ``check:`` is a critic citation tag, not part of the id."""
    text = str(raw or "").strip()
    if text.startswith(CHECK_PREFIX):
        return text[len(CHECK_PREFIX) :]
    return text


def normalize_evidence_ids(raw: Iterable[Any] | None) -> tuple[str, ...]:
    """Bare ids, dropping blanks. ``check:`` is stripped (HIR-0048)."""
    return tuple(
        normalize_evidence_id(item)
        for item in (raw or ())
        if str(item or "").strip()
    )


def metrics_certifying_property(property_kind: str, registry: Iterable[str]) -> frozenset[str]:
    """Ids in ``registry`` that can pay a claim of this property kind."""
    prefix = IMAGE_PROPERTY_PREFIXES.get(property_kind)
    names = [str(name) for name in registry]
    if prefix is None:
        return frozenset(name for name in names if name == property_kind)
    return frozenset(name for name in names if name.startswith(prefix))


def payable_image_property_kinds(registry: Iterable[str]) -> frozenset[str]:
    """Closed authoring vocabulary derived from the canonical metric registry."""
    return frozenset({*IMAGE_PROPERTY_PREFIXES, *(str(name) for name in registry)})


def image_property_vocabulary_gaps(
    units: Iterable[Any], registry: Iterable[str]
) -> tuple[ImagePropertyVocabularyGap, ...]:
    """Required image debts that no registered metric can possibly pay."""
    names = tuple(str(name) for name in registry)
    gaps: list[ImagePropertyVocabularyGap] = []
    for unit in units:
        for claim in getattr(getattr(unit, "evaluation", None), "claims", ()) or ():
            if not getattr(claim, "required", False) or getattr(claim, "asserts", None) != "image":
                continue
            contract_ids = tuple(
                normalize_evidence_id(getattr(binding, "id", ""))
                for binding in getattr(claim, "evidence", ()) or ()
                if str(getattr(binding, "kind", "") or "") == "image_contract"
            )
            if not contract_ids:
                continue
            property_kind = str(getattr(claim, "property", "") or "")
            if metrics_certifying_property(property_kind, names):
                continue
            gaps.append(
                ImagePropertyVocabularyGap(
                    unit_id=str(getattr(unit, "id", "") or ""),
                    claim_id=str(getattr(claim, "id", "") or ""),
                    property=property_kind,
                    contract_ids=contract_ids,
                )
            )
    return tuple(gaps)


def metric_matches_property(metric: str, property_kind: str) -> bool:
    """True when this image-check metric can certify the claim's property.

    ``render_region_stat`` is the ``region_*`` family in ``METRICS``, not a
    hand-copied key list. A new region metric is an honest payment.
    """
    if not metric:
        return False
    prefix = IMAGE_PROPERTY_PREFIXES.get(property_kind)
    if prefix is not None:
        return metric.startswith(prefix)
    return metric == property_kind


def image_contract_debt_cards(unit: Any) -> tuple[ImageContractDebt, ...]:
    """Required ``image_contract`` bindings, one card per (id, moment)."""
    cards: list[ImageContractDebt] = []
    seen: set[tuple[str, int]] = set()
    for claim in getattr(getattr(unit, "evaluation", None), "claims", ()) or ():
        if not getattr(claim, "required", False):
            continue
        if getattr(claim, "asserts", None) != "image":
            continue
        claim_moments = tuple(int(moment) for moment in (getattr(claim, "moments", ()) or ()))
        axis = str(getattr(claim, "axis", "") or "")
        property_kind = str(getattr(claim, "property", "") or "")
        for binding in getattr(claim, "evidence", ()) or ():
            if str(getattr(binding, "kind", "") or "") != "image_contract":
                continue
            cid = normalize_evidence_id(getattr(binding, "id", ""))
            if not cid:
                continue
            declared = getattr(binding, "moments", None)
            frames = (
                tuple(int(moment) for moment in declared)
                if declared is not None
                else claim_moments
            )
            for frame in frames:
                key = (cid, frame)
                if key in seen:
                    continue
                seen.add(key)
                cards.append(
                    ImageContractDebt(
                        id=cid,
                        axis=axis,
                        frame=frame,
                        property=property_kind,
                    )
                )
    return tuple(cards)


def debts_from_dicts(rows: Sequence[Mapping[str, Any]] | None) -> tuple[ImageContractDebt, ...]:
    """Rebuild cards from the compiled comparison-state / unit-scope projection."""
    cards: list[ImageContractDebt] = []
    for row in rows or ():
        cid = normalize_evidence_id(row.get("id"))
        if not cid:
            continue
        try:
            frame = int(row["frame"])
        except (KeyError, TypeError, ValueError):
            continue
        cards.append(
            ImageContractDebt(
                id=cid,
                axis=str(row.get("axis") or ""),
                frame=frame,
                property=str(row.get("property") or ""),
            )
        )
    return tuple(cards)


def payment_mismatches(row: Mapping[str, Any], debt: ImageContractDebt) -> tuple[str, ...]:
    """Why this proposed or stored row does not pay ``debt``. Empty means it pays."""
    reasons: list[str] = []
    found_id = normalize_evidence_id(row.get("id"))
    if found_id != debt.id:
        reasons.append(f"id {found_id!r} != {debt.id!r}")
    raw_frame = row.get("frame")
    try:
        found_frame = int(raw_frame)
    except (TypeError, ValueError):
        reasons.append(f"frame {raw_frame!r} != {debt.frame}")
    else:
        if found_frame != debt.frame:
            reasons.append(f"frame {found_frame} != {debt.frame}")
    found_axis = str(row.get("axis") or "")
    if found_axis != debt.axis:
        reasons.append(f"axis {found_axis!r} != {debt.axis!r}")
    metric = str(row.get("metric") or "")
    if not metric_matches_property(metric, debt.property):
        reasons.append(
            f"metric {metric!r} does not certify property {debt.property!r}"
        )
    return tuple(reasons)


def row_pays_debt(row: Mapping[str, Any], debt: ImageContractDebt) -> bool:
    return not payment_mismatches(row, debt)


def unpaid_image_contract_debts(
    cards: Sequence[ImageContractDebt],
    payment_rows: Iterable[Mapping[str, Any]],
) -> tuple[ImageContractDebt, ...]:
    rows = list(payment_rows)
    return tuple(
        card for card in cards if not any(row_pays_debt(row, card) for row in rows)
    )


def reject_proposed_image_check(
    proposed: Mapping[str, Any],
    debts: Sequence[ImageContractDebt],
    *,
    unpaid: Sequence[ImageContractDebt] | None = None,
    registry: Iterable[str] | None = None,
) -> str | None:
    """Rejection naming requested vs owed, or None if the proposal may proceed.

    With no debts, free-form builder checks stay legal. While any debt is unpaid,
    a non-owed id is rejected. An owed id still has to match frame, property kind,
    and axis (HIR-0015: an f150 measurement cannot pay an f72 debt).
    """
    if not debts:
        return None
    cid = normalize_evidence_id(proposed.get("id"))
    owed_ids = sorted({card.id for card in debts})
    matching_id = [card for card in debts if card.id == cid]
    still_unpaid = tuple(unpaid) if unpaid is not None else tuple(debts)
    unpaid_ids = sorted({card.id for card in still_unpaid})
    if not matching_id:
        if unpaid_ids:
            return (
                f"id {cid!r} is not an owed image-contract debt. owed: "
                + ", ".join(owed_ids)
                + ". propose_checks must pay those exact ids."
            )
        return None
    for card in matching_id:
        if not payment_mismatches(proposed, card):
            return None
    card = matching_id[0]
    reasons = payment_mismatches(proposed, card)
    compatible = metrics_certifying_property(card.property, registry or ())
    metric_hint = (
        " Compatible metrics: " + ", ".join(sorted(compatible)) + "."
        if compatible
        else " No registered metric can certify this property; abstain as unpaid_image_debt."
    )
    return (
        f"id {cid!r} does not pay the owed debt: requested frame="
        f"{proposed.get('frame')!r} axis={proposed.get('axis')!r} "
        f"metric={proposed.get('metric')!r}; owed frame={card.frame} "
        f"axis={card.axis!r} property={card.property!r} ({'; '.join(reasons)})."
        + metric_hint
    )


def cannot_express_covers_unpaid(
    payload: Mapping[str, Any] | None,
    unpaid: Sequence[ImageContractDebt],
) -> bool:
    """True when a typed ``unpaid_image_debt`` abstention covers every unpaid id."""
    if not unpaid or not isinstance(payload, Mapping):
        return False
    if str(payload.get("classification") or "") != UNPAID_IMAGE_DEBT:
        return False
    covered = {
        normalize_evidence_id(item) for item in (payload.get("contract_ids") or [])
    }
    return {card.id for card in unpaid} <= covered


def freeze_refusal(
    unpaid: Sequence[ImageContractDebt],
    cannot_express: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """None if freeze may proceed; otherwise the unpaid ids and payment path.

    Freeze succeeds when every debt is paid or a typed ``unpaid_image_debt``
    abstention covers the remainder. Anything else guarantees a canonical
    contract-gap whose only legal repair move is predetermined.
    """
    if not unpaid:
        return None
    if cannot_express_covers_unpaid(cannot_express, unpaid):
        return None
    ids = [card.id for card in unpaid]
    return {
        "ids": ids,
        "classification": UNPAID_IMAGE_DEBT,
        "reason": (
            "unpaid image-contract debts "
            + ", ".join(ids)
            + "; call propose_checks with each exact id, frame, property kind, and axis, "
            "or cannot_express_in_scope as unpaid_image_debt. Canonical repair cannot "
            "author evaluation contracts. "
            + UNPAID_IMAGE_DEBT_RULE
        ),
    }


def classify_cannot_express(
    contract_ids: Sequence[str],
    debts: Sequence[ImageContractDebt],
) -> str:
    """``unpaid_image_debt`` when every named id is an owed image-contract debt."""
    named = {normalize_evidence_id(item) for item in contract_ids if str(item).strip()}
    debt_ids = {card.id for card in debts}
    if named and debt_ids and named <= debt_ids:
        return UNPAID_IMAGE_DEBT
    return UNSATISFIABLE_IN_SCOPE


def conflict_authority(classification: str) -> str:
    if classification == UNPAID_IMAGE_DEBT:
        return UNPAID_IMAGE_DEBT_AUTHORITY
    return UNSATISFIABLE_PAIR_AUTHORITY
