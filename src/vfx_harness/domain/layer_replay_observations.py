"""Closed pre-judgment observations for one actual layer replay group."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import vfx_harness.domain.layer_replay_observation_values as replay_values
from vfx_harness.domain.judgment_debt_observation import (
    JUDGMENT_OBSERVATION_RENDER_MODES,
)
from vfx_harness.domain.judgment_debt_replay_receipts import ReplayPrefixReceipt
from vfx_harness.domain.layer_finalization_claims import (
    LayerFinalizationClaim,
    _relative_path,
    _text,
)
from vfx_harness.domain.layer_finalization_values import semantic_digest
from vfx_harness.domain.stop_envelope_primitives import canonical_digest, require_digest

if TYPE_CHECKING:
    from vfx_harness.domain.unit_evaluation_receipts import ReplayInputBinding

LAYER_REPLAY_OBSERVATION_SCHEMA = "vfx-harness.layer-replay-observation/v1"
LAYER_REPLAY_EVIDENCE_KINDS = frozenset({"executable_only", "render"})
LAYER_REPLAY_CLAIM_AUTHORITIES = frozenset(
    {"executable_required", "qualified_qualitative_required"}
)
LAYER_REPLAY_DETERMINISTIC_STATUSES = frozenset({"passed", "failed", "missing"})

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
_POINT_FIELDS = frozenset(
    {
        "frame",
        "ref",
        "ref_sha256",
        "evidence",
        "evidence_digest",
        "missing_evidence_ids",
        "failed_evidence_ids",
        "deterministic_status",
        "render",
        "render_sha256",
        "render_capture",
    }
)
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
        return {
            "claim_id": self.claim_id,
            "authority": self.authority,
            "judge_frames": list(self.judge_frames),
            "evidence_ids": list(self.evidence_ids),
            "evidence_frames": [
                [evidence_id, list(frames)] for evidence_id, frames in self.evidence_frames
            ],
        }


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
            parsed_debt_points = list(parsed_points) if value["debt_id"] is not None else []
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
        return {
            "group_index": self.group_index,
            "planned_group_count": self.planned_group_count,
            "requirement_ids": list(self.requirement_ids),
            "debt_id": self.debt_id,
            "definition_digest": self.definition_digest,
            "activation_digest": self.activation_digest,
            "payment_generation_digest": self.payment_generation_digest,
            "judge_points": [[frame, ref] for frame, ref in self.judge_points],
            "debt_points": [[frame, ref] for frame, ref in self.debt_points],
            "axes": list(self.axes),
            "claims": [row.as_dict() for row in self.claims],
            "evidence_kind": self.evidence_kind,
            "render_mode": self.render_mode,
            "render_scale": self.render_scale,
        }


@dataclass(frozen=True, slots=True)
class LayerReplayPointObservation:
    """Actual deterministic and render evidence captured before a critic call."""

    frame: int
    ref: str
    ref_sha256: str
    evidence: tuple[dict[str, Any], ...]
    evidence_digest: str
    missing_evidence_ids: tuple[str, ...]
    failed_evidence_ids: tuple[str, ...]
    deterministic_status: str
    render: str | None
    render_sha256: str | None
    render_capture: dict[str, Any] | None

    @classmethod
    def mint(
        cls,
        *,
        plan: LayerReplayEvaluationGroupPlan,
        frame: int,
        ref: object,
        ref_sha256: object,
        evidence: object,
        render: object = None,
        render_sha256: object = None,
        render_capture: object = None,
    ) -> LayerReplayPointObservation:
        if not isinstance(plan, LayerReplayEvaluationGroupPlan):
            raise ValueError("layer replay point requires a typed group plan")
        if not isinstance(frame, int) or isinstance(frame, bool) or frame < 1:
            raise ValueError("layer replay point frame must be a positive integer")
        point = (frame, str(ref))
        if point not in plan.judge_points:
            raise ValueError("layer replay point is not declared by its group plan")
        rows = replay_values.evidence_rows(evidence, "layer replay point evidence")
        by_id = {str(row["id"]): row for row in rows}
        required_ids = plan.executable_evidence_ids(frame)
        missing = tuple(sorted(set(required_ids) - set(by_id)))
        for evidence_id in required_ids:
            row = by_id.get(evidence_id)
            if row is not None and row["authoritative"] is not True:
                raise ValueError(
                    "required executable evidence must be harness-authoritative: "
                    f"{evidence_id}"
                )
        failed = tuple(
            sorted(
                str(row["id"])
                for row in rows
                if row["authoritative"] is True and row["pass"] is False
            )
        )
        status = "missing" if missing else "failed" if failed else "passed"
        evidence_digest = canonical_digest({"evidence": list(rows)})
        ref_path = _relative_path(ref, "layer replay point ref")
        ref_digest = require_digest(ref_sha256, "layer replay point ref_sha256")
        if plan.evidence_kind == "executable_only":
            if (
                render is not None
                or render_sha256 is not None
                or render_capture is not None
            ):
                raise ValueError("executable-only replay point forbids render evidence")
            render_path = None
            render_digest = None
            capture = None
        else:
            render_path = _relative_path(render, "layer replay point render")
            render_digest = require_digest(
                render_sha256,
                "layer replay point render_sha256",
            )
            capture = replay_values.render_capture(
                render_capture,
                "layer replay point render_capture",
                frame=frame,
                mode=str(plan.render_mode),
                scale=float(plan.render_scale),
            )
            if capture["png_sha256"] != render_digest:
                raise ValueError(
                    "layer replay point render SHA does not match its capture receipt"
                )
        return cls(
            frame=frame,
            ref=ref_path,
            ref_sha256=ref_digest,
            evidence=rows,
            evidence_digest=evidence_digest,
            missing_evidence_ids=missing,
            failed_evidence_ids=failed,
            deterministic_status=status,
            render=render_path,
            render_sha256=render_digest,
            render_capture=capture,
        )

    @classmethod
    def from_dict(
        cls,
        value: object,
        where: str,
        *,
        plan: LayerReplayEvaluationGroupPlan,
    ) -> LayerReplayPointObservation:
        if not isinstance(value, Mapping) or set(value) != _POINT_FIELDS:
            raise ValueError(f"{where} has unsupported layer replay point shape")
        candidate = cls.mint(
            plan=plan,
            frame=value["frame"],
            ref=value["ref"],
            ref_sha256=value["ref_sha256"],
            evidence=value["evidence"],
            render=value["render"],
            render_sha256=value["render_sha256"],
            render_capture=value["render_capture"],
        )
        expected = {
            "evidence_digest": candidate.evidence_digest,
            "missing_evidence_ids": list(candidate.missing_evidence_ids),
            "failed_evidence_ids": list(candidate.failed_evidence_ids),
            "deterministic_status": candidate.deterministic_status,
        }
        if any(value[key] != observed for key, observed in expected.items()):
            raise ValueError(f"{where} deterministic evidence projection is stale")
        return candidate

    def as_dict(self) -> dict[str, Any]:
        return {
            "frame": self.frame,
            "ref": self.ref,
            "ref_sha256": self.ref_sha256,
            "evidence": list(self.evidence),
            "evidence_digest": self.evidence_digest,
            "missing_evidence_ids": list(self.missing_evidence_ids),
            "failed_evidence_ids": list(self.failed_evidence_ids),
            "deterministic_status": self.deterministic_status,
            "render": self.render,
            "render_sha256": self.render_sha256,
            "render_capture": self.render_capture,
        }


@dataclass(frozen=True, slots=True)
class LayerReplayObservation:
    """Strict replay prefix and actual inputs for one pre-judgment group."""

    replay_prefix: ReplayPrefixReceipt
    plan: LayerReplayEvaluationGroupPlan
    points: tuple[LayerReplayPointObservation, ...]
    auxiliary_captures: tuple[dict[str, Any], ...] = ()
    execution_status: str = "passed"
    execution_failure: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.replay_prefix, ReplayPrefixReceipt):
            raise ValueError("layer replay observation requires ReplayPrefixReceipt")
        if not isinstance(self.plan, LayerReplayEvaluationGroupPlan):
            raise ValueError("layer replay observation requires a typed group plan")
        if not isinstance(self.points, tuple) or any(
            not isinstance(row, LayerReplayPointObservation) for row in self.points
        ):
            raise ValueError("layer replay observation points must be typed")
        if self.execution_status not in {"passed", "failed"}:
            raise ValueError("layer replay execution_status must be passed or failed")
        if self.execution_status == "passed":
            if self.execution_failure is not None:
                raise ValueError("passed layer replay forbids an execution failure")
            if tuple((row.frame, row.ref) for row in self.points) != self.plan.judge_points:
                raise ValueError(
                    "layer replay observation points must exactly match planned judge order"
                )
        else:
            if self.points or self.auxiliary_captures:
                raise ValueError(
                    "failed layer replay forbids unexecuted point and auxiliary evidence"
                )
            if self.execution_failure is None:
                raise ValueError("failed layer replay requires typed execution failure")
            object.__setattr__(
                self,
                "execution_failure",
                replay_values.execution_failure(
                    self.execution_failure,
                    "layer replay observation execution_failure",
                ),
            )
        for index, point in enumerate(self.points):
            rebuilt = LayerReplayPointObservation.mint(
                plan=self.plan,
                frame=point.frame,
                ref=point.ref,
                ref_sha256=point.ref_sha256,
                evidence=point.evidence,
                render=point.render,
                render_sha256=point.render_sha256,
                render_capture=point.render_capture,
            )
            if rebuilt != point:
                raise ValueError(
                    "layer replay observation point contains non-derived fields: "
                    f"index={index}"
                )
        object.__setattr__(
            self,
            "auxiliary_captures",
            replay_values.auxiliary_captures(
                self.auxiliary_captures,
                "layer replay observation auxiliary_captures",
            ),
        )

    @property
    def group_index(self) -> int:
        return self.plan.group_index

    @classmethod
    def failed(
        cls,
        *,
        replay_prefix: ReplayPrefixReceipt,
        plan: LayerReplayEvaluationGroupPlan,
        stage: object,
        message: object,
    ) -> LayerReplayObservation:
        stage_text = _text(stage, "layer replay execution failure stage")
        message_text = _text(message, "layer replay execution failure message")
        failure = {
            "stage": stage_text,
            "message": message_text,
            "failure_digest": canonical_digest(
                {"stage": stage_text, "message": message_text}
            ),
        }
        return cls(
            replay_prefix=replay_prefix,
            plan=plan,
            points=(),
            execution_status="failed",
            execution_failure=failure,
        )

    @property
    def digest(self) -> str:
        return semantic_digest(
            {
                "schema": LAYER_REPLAY_OBSERVATION_SCHEMA,
                "replay_prefix": self.replay_prefix.as_dict(),
                "plan": self.plan.as_dict(),
                "points": [row.as_dict() for row in self.points],
                "auxiliary_captures": list(self.auxiliary_captures),
                "execution_status": self.execution_status,
                "execution_failure": self.execution_failure,
            }
        )

    def assert_matches_claim(
        self,
        claim: LayerFinalizationClaim,
        replay_inputs: tuple[ReplayInputBinding, ...],
    ) -> None:
        if not isinstance(claim, LayerFinalizationClaim):
            raise ValueError("layer replay observation requires a finalization claim")
        layers = self.replay_prefix.layers
        payer = layers[-1]
        if (
            payer.layer_id,
            payer.payer_claim_id,
            payer.layer_generation_digest,
            payer.script_path,
            payer.script_sha256,
        ) != (
            claim.layer_id,
            claim.claim_id,
            claim.plan_hash,
            claim.layer_script_path,
            claim.layer_script_sha256,
        ):
            raise ValueError("layer replay observation payer does not match its claim")
        predecessor_rows = tuple(
            (
                row.layer_id,
                row.finalization_receipt_digest,
                row.script_path,
                row.script_sha256,
            )
            for row in claim.predecessor_inputs
        )
        observed_predecessors = tuple(
            (
                row.layer_id,
                row.finalization_receipt_digest,
                row.script_path,
                row.script_sha256,
            )
            for row in layers[:-1]
        )
        if observed_predecessors != predecessor_rows:
            raise ValueError(
                "layer replay observation predecessor prefix does not match its claim"
            )
        observed_inputs = tuple(
            (row.script_path, row.script_sha256) for row in replay_inputs
        )
        prefix_inputs = tuple((row.script_path, row.script_sha256) for row in layers)
        if observed_inputs != prefix_inputs:
            raise ValueError(
                "layer replay observation prefix does not match executed replay inputs"
            )
        observed_units = tuple(
            (
                row.unit_id,
                row.unit_digest,
                row.completion_receipt_digest,
                row.script_path,
                row.script_sha256,
            )
            for row in payer.units
        )
        expected_units = tuple(
            (
                row.unit_id,
                row.unit_digest,
                row.completion_receipt_digest,
                row.script_path,
                row.script_sha256,
            )
            for row in claim.unit_inputs
        )
        if observed_units != expected_units:
            raise ValueError("layer replay observation units do not match its claim")

    @classmethod
    def from_dict(cls, value: object, where: str) -> LayerReplayObservation:
        expected = {
            "schema",
            "replay_prefix",
            "plan",
            "points",
            "auxiliary_captures",
            "execution_status",
            "execution_failure",
            "observation_digest",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError(f"{where} has unsupported layer replay observation shape")
        if value["schema"] != LAYER_REPLAY_OBSERVATION_SCHEMA:
            raise ValueError(
                f"{where}.schema must be {LAYER_REPLAY_OBSERVATION_SCHEMA!r}"
            )
        raw_points = value["points"]
        if not isinstance(raw_points, list):
            raise ValueError(f"{where}.points must be a list")
        plan = LayerReplayEvaluationGroupPlan.from_dict(
            value["plan"],
            f"{where}.plan",
        )
        candidate = cls(
            replay_prefix=ReplayPrefixReceipt.from_dict(
                value["replay_prefix"],
                f"{where}.replay_prefix",
            ),
            plan=plan,
            points=tuple(
                LayerReplayPointObservation.from_dict(
                    row,
                    f"{where}.points[{index}]",
                    plan=plan,
                )
                for index, row in enumerate(raw_points)
            ),
            auxiliary_captures=replay_values.auxiliary_captures(
                value["auxiliary_captures"],
                f"{where}.auxiliary_captures",
            ),
            execution_status=value["execution_status"],
            execution_failure=value["execution_failure"],
        )
        if require_digest(
            value["observation_digest"],
            f"{where}.observation_digest",
        ) != candidate.digest:
            raise ValueError(f"{where}.observation_digest does not match its payload")
        return candidate

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": LAYER_REPLAY_OBSERVATION_SCHEMA,
            "replay_prefix": self.replay_prefix.as_dict(),
            "plan": self.plan.as_dict(),
            "points": [row.as_dict() for row in self.points],
            "auxiliary_captures": list(self.auxiliary_captures),
            "execution_status": self.execution_status,
            "execution_failure": self.execution_failure,
            "observation_digest": self.digest,
        }


def canonical_layer_replay_observation_bytes(
    observation: LayerReplayObservation,
) -> bytes:
    if not isinstance(observation, LayerReplayObservation):
        raise ValueError("observation must be a typed layer replay observation")
    return (
        json.dumps(
            observation.as_dict(),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


__all__ = [
    "LAYER_REPLAY_CLAIM_AUTHORITIES",
    "LAYER_REPLAY_DETERMINISTIC_STATUSES",
    "LAYER_REPLAY_EVIDENCE_KINDS",
    "LAYER_REPLAY_OBSERVATION_SCHEMA",
    "LayerReplayClaimRequirement",
    "LayerReplayEvaluationGroupPlan",
    "LayerReplayObservation",
    "LayerReplayPointObservation",
    "canonical_layer_replay_observation_bytes",
]
