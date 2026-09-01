"""Compile exact, pre-render observation identities for qualitative debt.

The compiler sits at the filesystem/runtime boundary: pure domain contracts do not
discover selected authority, replay artifacts, reference bytes, or promoted assets.
Every input that can make a repeated visual observation materially different is sealed
before the renderer is allowed to spend.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from vfx_harness.blender.observation_environment import (
    SCHEMA as OBSERVATION_ENVIRONMENT_SCHEMA,
)
from vfx_harness.blender.observation_environment import (
    canonical_observation_environment,
)
from vfx_harness.domain.judgment_debt_replay_receipts import (
    ReplayPrefixReceipt,
    payment_generation_for_replay,
    replay_parent_chain_digest,
)
from vfx_harness.domain.judgment_debts import (
    JudgmentObservationRequest,
    JudgmentPoint,
    validate_judgment_debt_replay_prefix,
)
from vfx_harness.domain.refobs import PROMOTED_CONSTRUCTION_SCHEMA
from vfx_harness.domain.stop_envelope_primitives import require_digest
from vfx_harness.orchestration.authority_selection import (
    ResolvedSelectedAuthority,
    SelectedAuthorityResolutionError,
    resolve_selected_authority,
)
from vfx_harness.orchestration.judgment_debt_state import (
    current_judgment_debt_states_for_authority,
)

ProvisionalJudgmentLifecycle = Literal["pending_not_due", "due"]


@dataclass(frozen=True, slots=True)
class ProvisionalJudgmentObservationCompilation:
    """A read-only observation request and the lifecycle state it was compiled from.

    ``pending_not_due`` is legal here because a layer-finalization transaction may
    need to perform the observation before publishing debt activation.  The request
    itself is identical to the one compiled after activation; lifecycle is deliberately
    not part of the payment identity.
    """

    request: JudgmentObservationRequest
    lifecycle: ProvisionalJudgmentLifecycle

    def __post_init__(self) -> None:
        if not isinstance(self.request, JudgmentObservationRequest):
            raise ValueError(
                "ProvisionalJudgmentObservationCompilation.request must be a "
                "JudgmentObservationRequest"
            )
        if self.lifecycle not in ("pending_not_due", "due"):
            raise ValueError(
                "ProvisionalJudgmentObservationCompilation.lifecycle must be "
                "pending_not_due or due"
            )


def _digest_json(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("judgment observation provenance must be finite canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _selected_snapshot(shot: Path) -> tuple[ResolvedSelectedAuthority, str, str]:
    """Return bundle/view identity from one completely verified selection snapshot."""

    try:
        selected = resolve_selected_authority(shot)
    except SelectedAuthorityResolutionError as exc:
        raise ValueError(str(exc)) from exc
    bundle = selected.assertion.bundle
    view = selected.assertion.effective_view
    if selected.assertion.selection != "selected" or bundle is None or view is None:
        raise ValueError("judgment observation requires selected plan authority")
    return selected, bundle.digest, view.digest


def selected_view_digest(shot: Path, bundle_digest: str) -> str:
    """Return the view digest only when it belongs to the same verified snapshot."""

    _selected, observed_bundle, observed_view = _selected_snapshot(shot)
    if observed_bundle != bundle_digest:
        raise ValueError("selected plan bundle changed before judgment observation")
    return observed_view


def _external_asset_provenance_digest(shot: Path, receipt: ReplayPrefixReceipt) -> str:
    """Seal every legal promoted-construction input in the replay prefix.

    Unit replay authority has exactly one identity-derived script.  Its adjacent
    ``.construction.json`` is the only legal external construction pointer after
    ADR-0009; an absent pointer is recorded explicitly, while a malformed or stale one
    fails closed instead of being treated as procedural work.
    """
    rows: list[dict[str, Any]] = []
    for layer in receipt.layers:
        for unit in layer.units:
            pointer = (shot / unit.script_path).with_suffix(".construction.json")
            identity = f"{unit.layer_id}:{unit.unit_id}"
            if not pointer.is_file():
                rows.append({"unit_id": identity, "construction": "none"})
                continue
            try:
                payload = json.loads(pointer.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"construction provenance for replayed unit {identity} is unreadable"
                ) from exc
            if not isinstance(payload, dict) or payload.get("schema") != PROMOTED_CONSTRUCTION_SCHEMA:
                raise ValueError(
                    f"construction provenance for replayed unit {identity} has an unsupported schema"
                )
            if (
                str(payload.get("layer_id") or "") != unit.layer_id
                or str(payload.get("unit_id") or "") != unit.unit_id
                or str(payload.get("unit_digest") or "") != unit.unit_digest
            ):
                raise ValueError(
                    f"construction provenance for replayed unit {identity} names stale unit authority"
                )
            relative = payload.get("glb")
            expected = payload.get("sha256")
            if not isinstance(relative, str) or not isinstance(expected, str):
                raise ValueError(
                    f"construction provenance for replayed unit {identity} has no pinned GLB"
                )
            glb = (shot / relative).resolve()
            try:
                glb.relative_to(shot)
            except ValueError as exc:
                raise ValueError(
                    f"construction provenance for replayed unit {identity} escapes the shot root"
                ) from exc
            if not glb.is_file() or _sha256(glb) != expected:
                raise ValueError(
                    f"construction provenance for replayed unit {identity} names stale GLB bytes"
                )
            rows.append(
                {
                    "unit_id": identity,
                    "unit_digest": unit.unit_digest,
                    "pointer_sha256": _sha256(pointer),
                    "glb": relative,
                    "glb_sha256": expected,
                }
            )
    return _digest_json(
        {
            "schema": "vfx-harness.judgment-observation-assets/v1",
            "units": rows,
        }
    )


def _validated_environment_digest(
    environment: Mapping[str, Any],
    *,
    frame: int,
    subject_roles: tuple[str, ...],
    carrier_families: tuple[str, ...],
    observation_medium: str,
) -> str:
    if not isinstance(environment, Mapping):
        raise ValueError("canonical observation environment must be an object")
    if environment.get("schema") != OBSERVATION_ENVIRONMENT_SCHEMA:
        raise ValueError("canonical observation environment has an unsupported schema")
    snapshot = environment.get("snapshot")
    canonical = canonical_observation_environment(snapshot)
    if canonical["digest"] != environment.get("digest"):
        raise ValueError("canonical observation environment digest is stale")
    if canonical["canonical_json"] != environment.get("canonical_json"):
        raise ValueError("canonical observation environment JSON is not canonical")
    if canonical["snapshot"].get("frame") != frame:
        raise ValueError(
            "canonical observation environment frame does not match the requested judge point"
        )
    if canonical["snapshot"].get("observation_medium") != observation_medium:
        raise ValueError(
            "canonical observation environment medium does not match the debt definition"
        )
    if tuple(canonical["snapshot"].get("subject_roles") or ()) != subject_roles:
        raise ValueError(
            "canonical observation environment subjects do not match the debt definition"
        )
    if tuple(canonical["snapshot"].get("carrier_families") or ()) != tuple(
        sorted(carrier_families)
    ):
        raise ValueError(
            "canonical observation environment carriers do not match the debt definition"
        )
    return str(canonical["digest"])


def _compile_judgment_observation_request(
    shot_folder: str | Path,
    definition_digest: str,
    *,
    replay_receipt: ReplayPrefixReceipt,
    layer_replay_receipt_digest: str,
    frame: int,
    ref: str,
    render_mode: str,
    render_scale: float,
    observation_environment: Mapping[str, Any],
    comparison_config: Mapping[str, Any],
    judge_config: Mapping[str, Any],
    allow_pending_not_due: bool,
) -> tuple[JudgmentObservationRequest, ProvisionalJudgmentLifecycle]:
    if not isinstance(replay_receipt, ReplayPrefixReceipt):
        raise ValueError("judgment observation requires a ReplayPrefixReceipt")
    layer_replay_digest = require_digest(
        layer_replay_receipt_digest,
        "judgment observation layer_replay_receipt_digest",
    )
    shot = Path(shot_folder).resolve()
    selected_authority, bundle_digest, selected_view = _selected_snapshot(shot)
    try:
        definition, activation, state = next(
            row
            for row in current_judgment_debt_states_for_authority(
                shot,
                selected_authority,
            )
            if row[0].digest == definition_digest
        )
    except StopIteration as exc:
        raise ValueError(
            f"unknown current judgment debt definition {definition_digest}"
        ) from exc
    if activation is None:
        raise ValueError(
            f"judgment debt {definition.debt_id} has no current payer activation"
        )
    if state.definition_digest != definition.digest:
        raise ValueError(
            f"judgment debt {definition.debt_id} state belongs to another definition"
        )
    lifecycle = state.status
    if allow_pending_not_due:
        if lifecycle not in ("pending_not_due", "due"):
            raise ValueError(
                f"judgment debt {definition.debt_id} provisional observation requires "
                f"pending_not_due or the exact due activation; found {lifecycle}"
            )
        if lifecycle == "due" and state.activation_digest != activation.digest:
            raise ValueError(
                f"judgment debt {definition.debt_id} provisional observation requires "
                "the exact due activation; found a stale activation"
            )
    elif lifecycle != "due" or state.activation_digest != activation.digest:
        raise ValueError(
            f"judgment debt {definition.debt_id} observation requires the exact due "
            f"activation; found {lifecycle}"
        )
    if bundle_digest != definition.seed.bundle_digest:
        raise ValueError(
            f"judgment debt {definition.debt_id} belongs to another selected bundle"
        )
    validate_judgment_debt_replay_prefix(activation, replay_receipt.unit_digests)
    point = JudgmentPoint(frame=frame, ref=ref)
    if point not in definition.seed.judge_points:
        raise ValueError(
            f"judgment point f{frame} {ref!r} is not declared by debt {definition.debt_id}"
        )
    reference = (shot / ref).resolve()
    try:
        reference.relative_to(shot)
    except ValueError as exc:
        raise ValueError(f"judgment reference {ref!r} escapes the shot root") from exc
    if not reference.is_file():
        raise ValueError(f"judgment reference {ref!r} is missing")

    payment_generation = payment_generation_for_replay(
        definition,
        activation,
        replay_receipt,
    )
    if (
        lifecycle == "due"
        and state.payment_generation_digest != payment_generation.digest
    ):
        raise ValueError(
            f"judgment debt {definition.debt_id} due state belongs to another "
            "replay payment generation"
        )
    request = JudgmentObservationRequest(
        definition_digest=definition.digest,
        activation_digest=activation.digest,
        payment_generation_digest=payment_generation.digest,
        bundle_digest=bundle_digest,
        owner_view_digest=_digest_json(
            {
                "schema": "vfx-harness.judgment-owner-view/v1",
                "selected_view_digest": selected_view,
                "layer_id": definition.seed.owner_layer,
            }
        ),
        payer_view_digest=_digest_json(
            {
                "schema": "vfx-harness.judgment-payer-view/v1",
                "selected_view_digest": selected_view,
                "layer_id": activation.payer_layer,
            }
        ),
        replay_receipt_digest=replay_receipt.digest,
        layer_replay_receipt_digest=layer_replay_digest,
        parent_chain_digest=replay_parent_chain_digest(replay_receipt),
        judge_point=point,
        observation_medium=definition.seed.observation_medium,
        render_mode=render_mode,
        render_scale=render_scale,
        reference_digest=_sha256(reference),
        reference_marker=None,
        observation_environment_digest=_validated_environment_digest(
            observation_environment,
            frame=frame,
            subject_roles=definition.seed.subject_roles,
            carrier_families=definition.seed.carrier_families,
            observation_medium=definition.seed.observation_medium,
        ),
        external_asset_provenance_digest=_external_asset_provenance_digest(
            shot,
            replay_receipt,
        ),
        comparison_config_digest=_digest_json(comparison_config),
        judge_config_digest=_digest_json(judge_config),
    )
    request.assert_matches(definition, activation)
    return request, lifecycle


def compile_current_judgment_observation_request(
    shot_folder: str | Path,
    definition_digest: str,
    *,
    replay_receipt: ReplayPrefixReceipt,
    layer_replay_receipt_digest: str,
    frame: int,
    ref: str,
    render_mode: str,
    render_scale: float,
    observation_environment: Mapping[str, Any],
    comparison_config: Mapping[str, Any],
    judge_config: Mapping[str, Any],
) -> JudgmentObservationRequest:
    """Seal one currently-due debt observation before any raster is produced."""
    request, _lifecycle = _compile_judgment_observation_request(
        shot_folder,
        definition_digest,
        replay_receipt=replay_receipt,
        layer_replay_receipt_digest=layer_replay_receipt_digest,
        frame=frame,
        ref=ref,
        render_mode=render_mode,
        render_scale=render_scale,
        observation_environment=observation_environment,
        comparison_config=comparison_config,
        judge_config=judge_config,
        allow_pending_not_due=False,
    )
    return request


def compile_provisional_judgment_observation_request(
    shot_folder: str | Path,
    definition_digest: str,
    *,
    replay_receipt: ReplayPrefixReceipt,
    layer_replay_receipt_digest: str,
    frame: int,
    ref: str,
    render_mode: str,
    render_scale: float,
    observation_environment: Mapping[str, Any],
    comparison_config: Mapping[str, Any],
    judge_config: Mapping[str, Any],
) -> ProvisionalJudgmentObservationCompilation:
    """Compile from an exact pending or due activation without changing debt state.

    This is the pre-terminal half of layer finalization.  The selected definition,
    selected payer activation, and exact replay prefix are all verified just as they
    are for a due observation.  Only the durable lifecycle precondition differs.
    """
    request, lifecycle = _compile_judgment_observation_request(
        shot_folder,
        definition_digest,
        replay_receipt=replay_receipt,
        layer_replay_receipt_digest=layer_replay_receipt_digest,
        frame=frame,
        ref=ref,
        render_mode=render_mode,
        render_scale=render_scale,
        observation_environment=observation_environment,
        comparison_config=comparison_config,
        judge_config=judge_config,
        allow_pending_not_due=True,
    )
    return ProvisionalJudgmentObservationCompilation(
        request=request,
        lifecycle=lifecycle,
    )
