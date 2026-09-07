"""Native Flynn capabilities for one unpublished, revision-bound VFX layer candidate."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from uuid import uuid4

import anyio
import flynn_agents_sdk as flynn
from jsonschema import Draft202012Validator

from vfx_harness.agents import materialization_operations as actions
from vfx_harness.domain.work_units import allowed_unit_provides
from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.orchestration import plan_inputs
from vfx_harness.orchestration.jit_materialization import (
    OVERLAY_ARTIFACTS,
    materialization_finalization_current,
    materialization_finalization_path,
)
from vfx_harness.orchestration.jit_materialization.overlay_base import OVERLAY_BASE_FILE
from vfx_harness.orchestration.plan_bundle_integrity import digest, read_real_file, require_real_directory

FEEDBACK_CHARACTERS = 8_000


def materialization_tools(
    *, layout: RunLayout, candidate: Path, check_current: Callable[[], None],
    overlay_root: Path | None = None,
) -> tuple[tuple[flynn.Tool, ...], flynn.DispatchGuard]:
    """Bind six native operations to an existing seeded candidate and live owner check.

    The caller serializes the attempt and must register the returned dispatch guard.
    No operation selects authority, accepts units or resumes an interrupted session.
    Complete results are retained in run reports; context selection remains caller-owned.
    """
    check_current()
    candidate = Path(candidate).absolute()
    if ".." in candidate.parts or not candidate.is_relative_to(layout.scratch):
        raise ValueError("native materialization candidate must be inside this run's scratch directory")
    require_real_directory(layout.shot, candidate.parent, "native materialization candidate parent")

    def read(path):
        payload = read_real_file(layout.shot, path, "native materialization input or output")
        if path.stat().st_nlink != 1:
            raise ValueError("native materialization cannot bind a hard-linked file")
        return payload

    payload = json.loads(read(candidate))
    authority = actions._materialization_authority_inputs(layout.shot, candidate, overlay_root=overlay_root)
    selection = authority.selected.selection_token
    layer_id = str(payload["layer"]["id"])
    layers = json.loads(read(authority.bundle_root / "layers.json"))["layers"]
    row = next(row for row in layers if str(row["id"]) == layer_id)
    requirements = json.loads(read(authority.bundle_root / "requirements.json"))["requirements"]
    owned_ids = set(row["jit"]["owned_requirements"])
    namespace = SimpleNamespace(
        materialization_revision_token=digest(read(candidate)),
        materialization_axis_ids=tuple(row["owns"]), materialization_layer_id=layer_id,
        materialization_allowed_provides=allowed_unit_provides(row),
        materialization_requirement_statements={
            item["id"]: item["statement"].strip() for item in requirements if item["id"] in owned_ids
        },
    )
    # Bind all local validation sources, including an unpublished replacement overlay.
    sources = {path: digest(read(path)) for path in {
        authority.base_layers, authority.base_scene_checks, authority.base_requirements,
    }}
    if overlay_root is not None:
        sources.update({Path(overlay_root) / name: digest(read(Path(overlay_root) / name))
                        for name in (*OVERLAY_ARTIFACTS, OVERLAY_BASE_FILE)})
    inputs_digest = plan_inputs.exact_planning_input_identity_digest(layout.shot)
    refs = tuple(sorted({item["ref"] for item in row["judge"]}))
    reference_bytes = {name: digest(read(layout.shot / name)) for name in refs}
    attestation = materialization_finalization_path(candidate)

    def outputs():
        return {str(path.relative_to(layout.root)): (
            digest(read(path)) if path.exists() or path.is_symlink() else None
        ) for path in (candidate, attestation)}

    owned = outputs()

    def check_inputs():
        check_current()
        current = actions._materialization_authority_inputs(layout.shot, candidate, overlay_root=overlay_root)
        actions._require_same_selection(selection, current.selected.selection_token,
                                        boundary="native materialization selection changed")
        if any(digest(read(path)) != value for path, value in sources.items()):
            raise ValueError("native materialization validation source changed; stop this attempt")
        if plan_inputs.exact_planning_input_identity_digest(layout.shot) != inputs_digest:
            raise ValueError("native materialization authored or decision inputs changed; stop this attempt")
        if any(digest(read(layout.shot / name)) != value for name, value in reference_bytes.items()):
            raise ValueError("native materialization reference changed; stop this attempt")

    def check():
        check_inputs()
        if outputs() != owned:
            raise ValueError("native materialization output changed outside this attempt; preserve the newer candidate")

    def resolve_reference(name):
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or name not in refs:
            raise ValueError("mint_refobs source must be an exact reference from this layer's judge list")
        check()
        return layout.shot / name

    @contextmanager
    def write_guard(shot, selected):
        with actions._current_selection_guard(shot, selected):
            # The shared writer calls this in its worker immediately before the
            # candidate CAS. Do not reacquire the selection lock through check().
            check_current()
            if outputs() != owned:
                raise ValueError("native materialization candidate changed before durable mutation")
            yield

    operations = actions.materialization_operations(
        shot_folder=layout.shot, layout=layout, candidate_materialization=candidate,
        overlay_root=overlay_root, ns=namespace, materialization_write_lock=anyio.Lock(),
        _resolve=resolve_reference, write_guard=write_guard,
    )

    def register(operation):
        schema = operation.input_schema
        if operation.name == "mint_refobs":
            schema = {**schema, "properties": {
                **schema["properties"], "source": {"type": "string", "enum": list(refs)},
            }}
        validator = Draft202012Validator(schema)

        def validate(arguments):
            errors = list(validator.iter_errors(arguments))
            if errors:
                raise ValueError(f"{operation.name} arguments: " + "; ".join(error.message for error in errors[:5]))

        async def execute(arguments):
            nonlocal owned
            check()
            before = dict(owned)
            result = await operation.handler(arguments)
            # Adopt only this operation's completed writes. Exceptions leave the old
            # ownership set and an uncertain SDK operation, never a reusable candidate.
            check_inputs()
            owned = outputs()
            finalized = (
                operation.name == "finalize_materialization" and not result.refused
                and materialization_finalization_current(layout.shot, candidate, bundle_hash=authority.bundle_hash)
            )
            record = {
                "schema": "vfx-harness.materialization-observation/v1", "operation": operation.name,
                "layer": layer_id, "candidate": str(candidate.relative_to(layout.root)),
                "base_selection": selection.to_dict(), "before": before, "after": dict(owned),
                "refused": result.refused, "text": result.text, "result": result.data,
                "finalization_current": finalized,
                "authority_selected": False,
            }
            report = layout.write_report(f"native-materialization-{uuid4().hex}", record)
            check()
            result_omitted = len(json.dumps(result.data, ensure_ascii=False)) > FEEDBACK_CHARACTERS // 2
            return flynn.ToolResult(
                status="refused" if result.refused else "ok",
                content=(flynn.TextContent(result.text[:FEEDBACK_CHARACTERS]),),
                data_json=json.dumps({
                    key: value for key, value in {
                        **record, "text": None, "report": str(report.relative_to(layout.root)),
                        "result": None if result_omitted else result.data, "result_omitted": result_omitted,
                        "report_sha256": digest(read(report)),
                        "omitted_characters": max(0, len(result.text) - FEEDBACK_CHARACTERS),
                    }.items() if key != "text"
                }, sort_keys=True),
            )

        return flynn.Tool.structured(
            operation.name, description=operation.description, parameters_json=json.dumps(schema),
            validate=validate, execute=execute,
            # Every call publishes a complete audit report, including status reads.
            external_action=True,
        )

    async def guard(_context):
        check()
        return flynn.GuardDecision(True, "exact VFX materialization attempt and candidate remain current")

    check()
    return tuple(register(operation) for operation in operations), flynn.DispatchGuard(
        "current-vfx-materialization-attempt", guard,
    )
