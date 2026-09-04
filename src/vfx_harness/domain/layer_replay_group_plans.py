"""What one layer replay group must observe: its claims and its group plan.

Split from ``layer_replay_observations`` at the cohesive seam between what a group is
required to observe and what it actually saw, when that module crossed the 900-line
budget.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import vfx_harness.domain.layer_replay_observation_values as replay_values
from vfx_harness.domain.judgment_debt_observation import (
    JUDGMENT_OBSERVATION_RENDER_MODES,
)
from vfx_harness.domain.layer_finalization_claims import _relative_path, _text
from vfx_harness.domain.stop_envelope_primitives import require_digest

LAYER_REPLAY_EVIDENCE_KINDS = frozenset({"executable_only", "render"})
LAYER_REPLAY_CLAIM_AUTHORITIES = frozenset(
    {"executable_required", "qualified_qualitative_required"}
)

_CLAIM_REQUIRED_FIELDS = frozenset(
    {"claim_id", "authority", "judge_frames", "evidence_ids"}
)
#: ``evidence_frames`` and ``debt_points`` are written by every current producer. A
#: receipt sealed before they existed omits them, and absence is not an ambiguous value
#: to interpret: it is exactly the state a new record carries when nothing declares a
#: schedule, and it is the faithful reading of what that receipt actually proved. Every
#: writer emits them; the reader accepts their absence and derives the historical
#: default (HIR-0207).
_CLAIM_FIELDS = _CLAIM_REQUIRED_FIELDS | {"evidence_frames"}
_PLAN_REQUIRED_FIELDS = frozenset(
    {
        "group_index",
        "planned_group_count",
        "requirement_ids",
        "debt_id",
        "definition_digest",
        "activation_digest",
        "payment_generation_digest",
        "judge_points",
        "axes",
        "claims",
        "evidence_kind",
        "render_mode",
        "render_scale",
    }
)
_PLAN_FIELDS = _PLAN_REQUIRED_FIELDS | {"debt_points"}
@dataclass(frozen=True, slots=True)
class LayerReplayClaimRequirement:
    """One required claim whose authority is evaluated in this replay group."""

    claim_id: str
    authority: str
    judge_frames: tuple[int, ...]
    evidence_ids: tuple[str, ...]
    # Frames each evidence id declares, as ``(id, (frame, ...))`` pairs. An id absent from
    # this map, or mapped to no frames, is unframed and due at every judge frame. Without it
    # the group required every id of a claim at every moment of that claim, so a claim
    # judged at 113/151/176 binding rows pinned to 113 and 176 was unsatisfiable at
    # composition: room run 20260904T154222Z-bc1735 refused a layer whose evidence all
    # passed, with failed=[] and "missing" at all three points (HIR-0204).
    evidence_frames: tuple[tuple[str, tuple[int, ...]], ...] = ()

    def __post_init__(self) -> None:
        _text(self.claim_id, "layer replay claim_id")
        if self.authority not in LAYER_REPLAY_CLAIM_AUTHORITIES:
            raise ValueError(
                "layer replay claim authority must be executable_required or "
                "qualified_qualitative_required"
            )
        replay_values.positive_frames(
            self.judge_frames,
            "layer replay claim judge_frames",
        )
        replay_values.strings(
            self.evidence_ids,
            "layer replay claim evidence_ids",
            allow_empty=self.authority == "qualified_qualitative_required",
        )
        seen: set[str] = set()
        for evidence_id, frames in self.evidence_frames:
            if evidence_id not in self.evidence_ids:
                raise ValueError(
                    "layer replay claim evidence_frames names an id outside evidence_ids: "
                    f"{evidence_id}"
                )
            if evidence_id in seen:
                raise ValueError(
                    f"layer replay claim evidence_frames repeats id {evidence_id}"
                )
            seen.add(evidence_id)
            replay_values.positive_frames(frames, "layer replay claim evidence_frames")

    @classmethod
    def mint(
        cls,
        *,
        claim_id: object,
        authority: object,
        judge_frames: Iterable[int],
        evidence_ids: Iterable[str],
        evidence_frames: Iterable[tuple[str, Iterable[int]]] = (),
    ) -> LayerReplayClaimRequirement:
        return cls(
            claim_id=str(claim_id),
            authority=str(authority),
            judge_frames=tuple(judge_frames),
            evidence_ids=tuple(evidence_ids),
            evidence_frames=tuple(
                (str(evidence_id), tuple(int(frame) for frame in frames))
                for evidence_id, frames in evidence_frames
            ),
        )

    @classmethod
    def from_dict(cls, value: object, where: str) -> LayerReplayClaimRequirement:
        if not isinstance(value, Mapping) or not (
            _CLAIM_REQUIRED_FIELDS <= set(value) <= _CLAIM_FIELDS
        ):
            raise ValueError(
                f"{where} has unsupported layer replay claim shape: it carries "
                f"{sorted(value) if isinstance(value, Mapping) else type(value).__name__}, "
                f"and {sorted(_CLAIM_REQUIRED_FIELDS)} are required"
            )
        return cls(
            claim_id=value["claim_id"],
            authority=value["authority"],
            judge_frames=replay_values.positive_frames(
                value["judge_frames"],
                f"{where}.judge_frames",
            ),
            evidence_ids=replay_values.strings(
                value["evidence_ids"],
                f"{where}.evidence_ids",
                allow_empty=value["authority"] == "qualified_qualitative_required",
            ),
            # An absent schedule is an unframed one: every bound row was due at every
            # frame its claim judged, which is what a pre-HIR-0204 receipt recorded.
            evidence_frames=tuple(
                (str(row[0]), replay_values.positive_frames(row[1], f"{where}.evidence_frames"))
                for row in (value.get("evidence_frames") or ())
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "claim_id": self.claim_id,
            "authority": self.authority,
            "judge_frames": list(self.judge_frames),
            "evidence_ids": list(self.evidence_ids),
        }
        # An additive field is written only when it differs from the default a reader
        # derives for it. A record read from stored bytes must serialize back to exactly
        # those bytes, or the digest that made it evidence no longer verifies (HIR-0208).
        if self.evidence_frames:
            row["evidence_frames"] = [
                [evidence_id, list(frames)] for evidence_id, frames in self.evidence_frames
            ]
        return row


@dataclass(frozen=True, slots=True)
class LayerReplayEvaluationGroupPlan:
    """Exact authority and observation settings for one actual replay group."""

    group_index: int
    planned_group_count: int
    requirement_ids: tuple[str, ...]
    debt_id: str | None
    definition_digest: str | None
    activation_digest: str | None
    payment_generation_digest: str | None
    judge_points: tuple[tuple[int, str], ...]
    #: The judge points this group's judgment debt actually owns. A debt is due at a
    #: subset of the group's judge points, and the payment compiler declines to produce
    #: an observation anywhere else; demanding one at every point from the layer-level
    #: ``debt_id`` made a legal group unmintable (HIR-0206).
    debt_points: tuple[tuple[int, str], ...]
    axes: tuple[str, ...]
    claims: tuple[LayerReplayClaimRequirement, ...]
    evidence_kind: str
    render_mode: str | None
    render_scale: float | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.group_index, int)
            or isinstance(self.group_index, bool)
            or self.group_index < 0
        ):
            raise ValueError("layer replay group_index must be a non-negative integer")
        if (
            not isinstance(self.planned_group_count, int)
            or isinstance(self.planned_group_count, bool)
            or self.planned_group_count < 1
            or self.group_index >= self.planned_group_count
        ):
            raise ValueError("layer replay planned_group_count must contain group_index")
        replay_values.strings(
            self.requirement_ids,
            "layer replay requirement_ids",
            allow_empty=True,
        )
        replay_values.strings(
            self.axes,
            "layer replay axes",
            allow_empty=True,
        )
        debt_values = (
            self.debt_id,
            self.definition_digest,
            self.activation_digest,
            self.payment_generation_digest,
        )
        if any(value is None for value in debt_values) != all(
            value is None for value in debt_values
        ):
            raise ValueError(
                "layer replay debt identity and payment generation must all be "
                "present or all be null"
            )
        if self.debt_id is not None:
            _text(self.debt_id, "layer replay debt_id")
            require_digest(self.definition_digest, "layer replay definition_digest")
            require_digest(self.activation_digest, "layer replay activation_digest")
            require_digest(
                self.payment_generation_digest,
                "layer replay payment_generation_digest",
            )
        if not isinstance(self.judge_points, tuple) or not self.judge_points:
            raise ValueError("layer replay judge_points must be a non-empty tuple")
        normalized_points: list[tuple[int, str]] = []
        for index, point in enumerate(self.judge_points):
            if (
                not isinstance(point, tuple)
                or len(point) != 2
                or not isinstance(point[0], int)
                or isinstance(point[0], bool)
                or point[0] < 1
            ):
                raise ValueError(
                    f"layer replay judge_points[{index}] must be (positive frame, ref)"
                )
            normalized_points.append(
                (
                    point[0],
                    _relative_path(
                        point[1],
                        f"layer replay judge_points[{index}].ref",
                    ),
                )
            )
        if len(normalized_points) != len(set(normalized_points)):
            raise ValueError("layer replay judge_points contains duplicates")
        if not isinstance(self.debt_points, tuple):
            raise ValueError("layer replay debt_points must be a tuple")
        debt_points = tuple(
            (point[0], point[1])
            for point in self.debt_points
            if isinstance(point, tuple) and len(point) == 2
        )
        if len(debt_points) != len(self.debt_points):
            raise ValueError("layer replay debt_points[] must each be (frame, ref)")
        if len(debt_points) != len(set(debt_points)):
            raise ValueError("layer replay debt_points contains duplicates")
        if self.debt_id is None:
            if debt_points:
                raise ValueError(
                    "layer replay debt_points require a judgment debt on this group"
                )
        else:
            if not debt_points:
                raise ValueError(
                    "a layer replay judgment debt must own at least one judge point"
                )
            outside = sorted(set(debt_points) - set(normalized_points))
            if outside:
                raise ValueError(
                    "layer replay debt_points must be judge points of this group; "
                    f"{outside} are not among {sorted(normalized_points)}"
                )
        if not isinstance(self.claims, tuple) or not self.claims:
            raise ValueError("layer replay group must bind at least one required claim")
        if any(not isinstance(row, LayerReplayClaimRequirement) for row in self.claims):
            raise ValueError("layer replay group claims must be typed")
        claim_ids = [row.claim_id for row in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("layer replay group contains duplicate claim ids")
        judge_frames = {frame for frame, _ref in self.judge_points}
        if any(not set(row.judge_frames) <= judge_frames for row in self.claims):
            raise ValueError("layer replay claim judges frames outside its group")
        if self.evidence_kind not in LAYER_REPLAY_EVIDENCE_KINDS:
            raise ValueError(
                "layer replay evidence_kind must be executable_only or render"
            )
        authorities = {row.authority for row in self.claims}
        if self.evidence_kind == "executable_only":
            if authorities != {"executable_required"}:
                raise ValueError(
                    "executable-only replay groups require only executable claims"
                )
            if self.render_mode is not None or self.render_scale is not None:
                raise ValueError(
                    "executable-only layer replay groups forbid render settings"
                )
            if self.debt_id is not None:
                raise ValueError(
                    "qualitative judgment debt requires a rendered replay group"
                )
            return
        if (
            self.debt_id is not None
            and "qualified_qualitative_required" not in authorities
        ):
            raise ValueError(
                "judgment-debt replay groups require a qualified qualitative claim"
            )
        if self.render_mode not in JUDGMENT_OBSERVATION_RENDER_MODES:
            raise ValueError("rendered layer replay group requires solid or eevee mode")
        if (
            isinstance(self.render_scale, bool)
            or not isinstance(self.render_scale, (int, float))
            or not math.isfinite(float(self.render_scale))
            or not 0.0 < float(self.render_scale) <= 1.0
        ):
            raise ValueError("rendered layer replay group requires scale in (0, 1]")

    def executable_evidence_ids(self, frame: int) -> tuple[str, ...]:
        """Executable ids due at ``frame``: unframed rows always, framed rows at their frame.

        A bound static row is scheduled at its declared frame, falling back to the active
        judge only when unframed. Requiring every id of a claim at every moment of that claim
        demanded frame-pinned rows where they cannot exist (HIR-0204).
        """
        due: list[str] = []
        for claim in self.claims:
            if claim.authority != "executable_required" or int(frame) not in claim.judge_frames:
                continue
            declared = dict(claim.evidence_frames)
            for evidence_id in claim.evidence_ids:
                frames = declared.get(evidence_id) or ()
                if not frames or int(frame) in frames:
                    due.append(evidence_id)
        return tuple(dict.fromkeys(due))

    @classmethod
    def from_dict(cls, value: object, where: str) -> LayerReplayEvaluationGroupPlan:
        if not isinstance(value, Mapping) or not (
            _PLAN_REQUIRED_FIELDS <= set(value) <= _PLAN_FIELDS
        ):
            raise ValueError(
                f"{where} has unsupported layer replay group-plan shape: it carries "
                f"{sorted(value) if isinstance(value, Mapping) else type(value).__name__}, "
                f"and {sorted(_PLAN_REQUIRED_FIELDS)} are required"
            )
        points = value["judge_points"]
        claims = value["claims"]
        if not isinstance(points, list) or not isinstance(claims, list):
            raise ValueError(f"{where}.judge_points and claims must be lists")
        parsed_points: list[tuple[int, str]] = []
        for index, point in enumerate(points):
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError(f"{where}.judge_points[{index}] must be [frame, ref]")
            parsed_points.append((point[0], point[1]))
        # An absent debt schedule on a stored plan is not a guess: such a receipt minted
        # under a reader that demanded an observation at every judge point, so a debt
        # there owned all of them and a debtless group owned none.
        if "debt_points" in value:
            debt_points = value["debt_points"]
            if not isinstance(debt_points, list):
                raise ValueError(f"{where}.debt_points must be a list")
            parsed_debt_points: list[tuple[int, str]] = []
            for index, point in enumerate(debt_points):
                if not isinstance(point, list) or len(point) != 2:
                    raise ValueError(f"{where}.debt_points[{index}] must be [frame, ref]")
                parsed_debt_points.append((point[0], point[1]))
        else:
            parsed_debt_points = (
                list(parsed_points) if value["debt_id"] is not None else []
            )
        return cls(
            group_index=value["group_index"],
            planned_group_count=value["planned_group_count"],
            requirement_ids=replay_values.strings(
                value["requirement_ids"],
                f"{where}.requirement_ids",
                allow_empty=True,
            ),
            debt_id=value["debt_id"],
            definition_digest=value["definition_digest"],
            activation_digest=value["activation_digest"],
            payment_generation_digest=value["payment_generation_digest"],
            judge_points=tuple(parsed_points),
            debt_points=tuple(parsed_debt_points),
            axes=replay_values.strings(
                value["axes"],
                f"{where}.axes",
                allow_empty=True,
            ),
            claims=tuple(
                LayerReplayClaimRequirement.from_dict(
                    row,
                    f"{where}.claims[{index}]",
                )
                for index, row in enumerate(claims)
            ),
            evidence_kind=value["evidence_kind"],
            render_mode=value["render_mode"],
            render_scale=value["render_scale"],
        )

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "group_index": self.group_index,
            "planned_group_count": self.planned_group_count,
            "requirement_ids": list(self.requirement_ids),
            "debt_id": self.debt_id,
            "definition_digest": self.definition_digest,
            "activation_digest": self.activation_digest,
            "payment_generation_digest": self.payment_generation_digest,
            "judge_points": [[frame, ref] for frame, ref in self.judge_points],
            "axes": list(self.axes),
            "claims": [row.as_dict() for row in self.claims],
            "evidence_kind": self.evidence_kind,
            "render_mode": self.render_mode,
            "render_scale": self.render_scale,
        }
        # Written only when the debt's points differ from what a reader derives from a
        # plan that omits them: every judge point with a debt, none without (HIR-0208).
        if self.debt_points != self._derived_debt_points():
            payload["debt_points"] = [[frame, ref] for frame, ref in self.debt_points]
        return payload

    def _derived_debt_points(self) -> tuple[tuple[int, str], ...]:
        return tuple(self.judge_points) if self.debt_id is not None else ()
