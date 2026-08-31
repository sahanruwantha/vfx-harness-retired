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
from pathlib import Path
from typing import Any

from vfx_harness.blender.observation_environment import (
    SCHEMA as OBSERVATION_ENVIRONMENT_SCHEMA,
)
from vfx_harness.blender.observation_environment import (
    canonical_observation_environment,
)
from vfx_harness.domain.judgment_debts import (
    JudgmentObservationRequest,
    JudgmentPoint,
    validate_judgment_debt_replay_prefix,
)
from vfx_harness.domain.refobs import PROMOTED_CONSTRUCTION_SCHEMA
from vfx_harness.orchestration.authority_selection import (
    ResolvedSelectedAuthority,
    SelectedAuthorityResolutionError,
    resolve_selected_authority,
)
from vfx_harness.orchestration.judgment_debt_state import (
    ReplayPrefixReceipt,
    current_judgment_debt_states_for_authority,
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


def _parent_chain_digest(receipt: ReplayPrefixReceipt) -> str:
    return _digest_json(
        {
            "schema": "vfx-harness.judgment-observation-parent-chain/v1",
            "layers": [
                {
                    "layer_id": layer.layer_id,
                    "script_path": layer.script_path,
                    "script_sha256": layer.script_sha256,
                }
                for layer in receipt.layers
            ],
        }
    )


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


def compile_current_judgment_observation_request(
    shot_folder: str | Path,
    definition_digest: str,
    *,
    replay_receipt: ReplayPrefixReceipt,
    frame: int,
    ref: str,
    render_mode: str,
    render_scale: float,
    observation_environment: Mapping[str, Any],
    comparison_config: Mapping[str, Any],
    judge_config: Mapping[str, Any],
) -> JudgmentObservationRequest:
    """Seal one currently-due debt observation before any raster is produced."""
    if not isinstance(replay_receipt, ReplayPrefixReceipt):
        raise ValueError("judgment observation requires a ReplayPrefixReceipt")
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
    if state.status != "due" or state.activation_digest != activation.digest:
        raise ValueError(
            f"judgment debt {definition.debt_id} observation requires the exact due "
            f"activation; found {state.status}"
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

    request = JudgmentObservationRequest(
        definition_digest=definition.digest,
        activation_digest=activation.digest,
        payment_generation_digest=_digest_json(
            {
                "schema": "vfx-harness.judgment-payment-generation/v1",
                "bundle_digest": bundle_digest,
                "definition_digest": definition.digest,
                "activation_digest": activation.digest,
            }
        ),
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
        parent_chain_digest=_parent_chain_digest(replay_receipt),
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
    return request
