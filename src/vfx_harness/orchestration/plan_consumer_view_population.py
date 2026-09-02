"""Complete descriptor-rooted construction of one selected plan-consumer view."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vfx_harness.domain.authority_head_records import canonical_json_bytes
from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.orchestration import (
    plan_bundle_integrity,
    plan_consumer_state_snapshot,
    plan_consumer_view_mutation,
    plan_consumer_view_projection,
)
from vfx_harness.orchestration.plan_consumer_view import (
    OVERLAY_ARTIFACTS,
    PlanConsumerViewMarker,
)
from vfx_harness.orchestration.plan_inputs import (
    DECISION_INPUT_FIELDS,
    DECISION_INPUT_PATHS,
    PROVENANCE_SCHEMA,
    authored_input_bytes,
    decision_input_bytes,
)

_PROVENANCE_FIELDS = frozenset({"schema", "authored_inputs", "decision_inputs"})
PlanPublicationError = plan_bundle_integrity.PlanPublicationError


def _verify_decision_input_payloads(
    live: dict[str, bytes],
    expected: Any,
) -> None:
    if not isinstance(expected, dict):
        raise PlanPublicationError(
            "plan bundle decision-input provenance must be an object"
        )
    allowed = {path.as_posix() for path in DECISION_INPUT_PATHS}
    unsupported = sorted(name for name in expected if name not in allowed)
    if unsupported:
        raise PlanPublicationError(
            "plan bundle decision-input provenance names unsupported paths: "
            + ", ".join(unsupported)
        )
    for name, assertion in expected.items():
        if not isinstance(assertion, dict) or set(assertion) != DECISION_INPUT_FIELDS:
            raise PlanPublicationError(
                f"plan bundle decision-input provenance fields are invalid: {name}"
            )
        size = assertion["size"]
        expected_hash = assertion["sha256"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise PlanPublicationError(
                f"plan bundle decision-input provenance size is invalid: {name}"
            )
        if not plan_bundle_integrity.is_digest(expected_hash):
            raise PlanPublicationError(
                f"plan bundle decision-input provenance hash is invalid: {name}"
            )
        data = live.get(name)
        if data is None or len(data) < size:
            raise PlanPublicationError(
                "published plan was derived from missing or truncated planning decisions"
            )
        if plan_bundle_integrity.digest(data[:size]) != expected_hash:
            raise PlanPublicationError(
                "published plan was derived from different planning decisions"
            )


def _captured_input_identity(
    authored: dict[str, bytes],
    decisions: dict[str, bytes],
) -> tuple[dict[str, str], dict[str, dict[str, str | int]]]:
    return (
        {
            name: plan_bundle_integrity.digest(payload)
            for name, payload in authored.items()
        },
        {
            name: {
                "size": len(payload),
                "sha256": plan_bundle_integrity.digest(payload),
            }
            for name, payload in decisions.items()
        },
    )


def _effective_artifacts(
    selected: Any,
) -> tuple[dict[str, bytes], dict[str, str]]:
    payloads: dict[str, bytes] = {}
    hashes: dict[str, str] = {}
    for name in OVERLAY_ARTIFACTS:
        try:
            source = selected.artifact_paths[name]
        except KeyError as exc:
            raise PlanPublicationError(
                f"selected authority omits effective-view artifact {name!r}"
            ) from exc
        payload, digest = plan_consumer_view_projection.read_exact_source_file(source)
        payloads[name] = payload
        hashes[name] = digest
    return payloads, hashes


def _layers(payloads: dict[str, bytes]) -> list[dict[str, Any]]:
    try:
        layers = json.loads(payloads["layers.json"])["layers"]
        if not isinstance(layers, list) or any(
            not isinstance(layer, dict) for layer in layers
        ):
            raise TypeError("layers must be a list of objects")
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanPublicationError("published layers.json is unreadable") from exc
    return layers


def _project_bundle_artifacts(
    capability: plan_consumer_view_mutation.PlanConsumerViewMutationCapability,
    selected: Any,
    bundle: Any,
    artifact_hashes: dict[str, str],
    projected_members: set[str],
) -> None:
    plan_consumer_view_projection.ensure_directory(capability, "plans")
    for name in bundle.artifacts:
        try:
            source = selected.artifact_paths[name]
        except KeyError as exc:
            raise PlanPublicationError(
                f"selected authority omits bundle artifact {name!r}"
            ) from exc
        target = "plans/global.md" if name == "global.md" else name
        expected_hash = artifact_hashes.get(name)
        if expected_hash is None:
            _payload, expected_hash = (
                plan_consumer_view_projection.read_exact_source_file(source)
            )
        plan_consumer_view_projection.create_verified_file_symlink(
            capability,
            target,
            source,
            expected_sha256=expected_hash,
        )
        projected_members.add(target)


def _project_jit_plans(
    layout: RunLayout,
    capability: plan_consumer_view_mutation.PlanConsumerViewMutationCapability,
    selected: Any,
    layers: list[dict[str, Any]],
    projected_members: set[str],
) -> None:
    # Delayed to avoid the plan-authority/layer-plans module cycle.
    from vfx_harness.orchestration.layer_plans import (  # noqa: PLC0415
        validate_work_unit_plan_authority,
        work_unit_plan_authority_path,
    )

    for layer in layers:
        for unit in layer.get("stages") or []:
            rel = Path(str(unit.get("plan") or ""))
            source = layout.shot / rel
            if not source.is_file():
                continue
            try:
                # Staging feeds the gate, which runs before attestation exists.
                validate_work_unit_plan_authority(
                    layout.shot,
                    source,
                    require_gate=False,
                    selected_authority=selected,
                )
            except ValueError:
                continue
            target = rel.as_posix()
            if target in projected_members:
                continue
            _payload, source_hash = plan_consumer_view_projection.read_exact_source_file(
                source
            )
            plan_consumer_view_projection.create_verified_file_symlink(
                capability,
                target,
                source,
                expected_sha256=source_hash,
            )
            projected_members.add(target)
            source_authority = work_unit_plan_authority_path(source)
            if not source_authority.is_file():
                raise PlanPublicationError(
                    "JIT unit plan authority sidecar is missing: "
                    f"{source_authority}"
                )
            authority_target = work_unit_plan_authority_path(rel).as_posix()
            _authority_payload, authority_hash = (
                plan_consumer_view_projection.read_exact_source_file(source_authority)
            )
            plan_consumer_view_projection.create_verified_file_symlink(
                capability,
                authority_target,
                source_authority,
                expected_sha256=authority_hash,
            )
            projected_members.add(authority_target)


def populate_plan_consumer_view(
    layout: RunLayout,
    selected: Any,
    bundle: Any,
    view: Path,
) -> Path:
    """Populate, verify, seal, and install one exact consumer-view generation."""

    installation = None
    try:
        provenance = plan_bundle_integrity.read_schema_object(
            layout.shot,
            selected.artifact_paths["plan.provenance.json"],
            "selected plan bundle provenance",
            schema=PROVENANCE_SCHEMA,
            fields=_PROVENANCE_FIELDS,
        )
        authored = authored_input_bytes(layout.shot)
        decisions = decision_input_bytes(layout.shot)
        captured_authored, captured_decisions = _captured_input_identity(
            authored,
            decisions,
        )
        if provenance.get("authored_inputs") != captured_authored:
            raise PlanPublicationError(
                "plan consumer snapshot was captured from different authored inputs"
            )
        _verify_decision_input_payloads(
            decisions,
            provenance.get("decision_inputs"),
        )
        overlay_payloads, artifact_hashes = _effective_artifacts(selected)
        marker = PlanConsumerViewMarker.from_dict(
            PlanConsumerViewMarker(
                shot=layout.shot,
                bundle=bundle.root,
                content_hash=bundle.content_hash,
                base_selection=selected.selection_token,
                view_source=selected.assertion.effective_view.source,
                view_digest=selected.assertion.effective_view.digest,
                artifact_hashes=artifact_hashes,
                authored_inputs=captured_authored,
                decision_inputs=captured_decisions,
            ).to_dict()
        )
        layers = _layers(overlay_payloads)
        projected_members: set[str] = set()
        with plan_consumer_view_mutation.allocating_plan_consumer_view(
            layout
        ) as (_temporary, allocation), plan_consumer_view_mutation.constructing_plan_consumer_view(
            layout,
            allocation,
            marker,
        ) as capability:
            plan_consumer_view_projection.require_regular_file_bytes(
                capability,
                ".plan-consumer-view.json",
                canonical_json_bytes(marker.to_dict()),
            )
            plan_consumer_view_projection.ensure_directory(capability, "refs")
            for name, payload in sorted(authored.items()):
                plan_consumer_view_projection.create_regular_file(
                    capability,
                    name,
                    payload,
                )
                projected_members.add(name)
            for name, payload in sorted(decisions.items()):
                plan_consumer_view_projection.create_regular_file(
                    capability,
                    name,
                    payload,
                )
                projected_members.add(name)
            _project_bundle_artifacts(
                capability,
                selected,
                bundle,
                artifact_hashes,
                projected_members,
            )
            plan_consumer_state_snapshot.snapshot_consumer_execution_authority(
                layout,
                capability,
                layers,
            )
            _project_jit_plans(
                layout,
                capability,
                selected,
                layers,
                projected_members,
            )
            plan_consumer_view_projection.require_regular_file_bytes(
                capability,
                ".plan-consumer-view.json",
                canonical_json_bytes(marker.to_dict()),
            )
            # Installation authority is deliberately minted only after the last
            # authored, decision, plan/JIT, state, ledger, and marker readback.
            installation = (
                plan_consumer_view_mutation.prepare_plan_consumer_view_installation(
                    capability
                )
            )
        plan_consumer_view_mutation.install_plan_consumer_view(
            installation,
            view,
        )
    except BaseException as exc:
        try:
            if installation is not None and not (
                plan_consumer_view_mutation.plan_consumer_view_installation_is_committed(
                    installation,
                    view,
                )
            ):
                plan_consumer_view_mutation.discard_prepared_plan_consumer_view_installation(
                    installation
                )
        except BaseException as cleanup_exc:
            failure = PlanPublicationError(
                "plan-consumer preparation failed and exact temporary cleanup "
                "could not prove completion; route to engineering"
            )
            failure.add_note(
                f"primary preparation failure: {type(exc).__name__}: {exc}"
            )
            raise failure from cleanup_exc
        if isinstance(
            exc,
            plan_consumer_view_mutation.PlanConsumerViewMutationConflict,
        ):
            raise PlanPublicationError(str(exc)) from exc
        raise
    return view


__all__ = ["populate_plan_consumer_view"]
