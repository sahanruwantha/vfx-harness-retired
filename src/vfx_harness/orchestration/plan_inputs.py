"""Authored-input provenance and revision-bound global-plan workspaces."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.orchestration import plan_bundle_integrity
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    AuthoritySelectionToken,
)

WORKSPACE_SCHEMA = "vfx-harness.plan-workspace/v3"
PROVENANCE_SCHEMA = "vfx-harness.plan-provenance/v2"
WORKSPACE_FIELDS = frozenset(
    {
        "schema",
        "run_id",
        "shot",
        "authored_inputs",
        "decision_inputs",
        "base_selection",
    }
)
DECISION_INPUT_FIELDS = frozenset({"size", "sha256"})
DECISION_INPUT_PATHS = (
    Path("plan_amendments.jsonl"),
    Path("state/plan-resolutions.jsonl"),
)
PlanPublicationError = plan_bundle_integrity.PlanPublicationError


def authored_input_bytes(root: Path) -> dict[str, bytes]:
    brief = root / "brief.md"
    inputs = {
        "brief.md": plan_bundle_integrity.read_real_file(
            root,
            brief,
            "authored brief input",
        )
    }
    for path in plan_bundle_integrity.regular_files_under(
        root,
        root / "refs",
        "authored refs input",
    ):
        inputs[path.relative_to(root).as_posix()] = plan_bundle_integrity.read_real_file(
            root,
            path,
            "authored reference input",
        )
    return inputs


def authored_inputs(root: Path) -> dict[str, str]:
    """Content identity that selected authority must continue to match."""

    return {
        name: plan_bundle_integrity.digest(data)
        for name, data in authored_input_bytes(root).items()
    }


def decision_input_bytes(root: Path) -> dict[str, bytes]:
    inputs: dict[str, bytes] = {}
    for relative in DECISION_INPUT_PATHS:
        path = root / relative
        if path.parent != root and (
            path.parent.is_symlink()
            or (path.parent.exists() and not path.parent.is_dir())
        ):
            plan_bundle_integrity.require_real_directory(
                root,
                path.parent,
                "planning decision input parent",
            )
        if not path.exists() and not path.is_symlink():
            continue
        inputs[relative.as_posix()] = plan_bundle_integrity.read_real_file(
            root,
            path,
            "planning decision input",
        )
    return inputs


def decision_inputs(root: Path) -> dict[str, dict[str, str | int]]:
    """Append-only cross-run decisions folded into a planning transaction."""

    return {
        name: {"size": len(data), "sha256": plan_bundle_integrity.digest(data)}
        for name, data in decision_input_bytes(root).items()
    }


def exact_planning_input_identity(
    root: Path,
) -> tuple[dict[str, str], dict[str, dict[str, str | int]]]:
    """Return the complete current identity consumed by a deterministic plan gate.

    Plan provenance deliberately treats decision ledgers as append-only prefixes. A gate
    snapshot and the publication it authorizes need the stronger identity here: exact
    authored bytes and the exact full length/hash of every decision input.
    """

    return authored_inputs(root), decision_inputs(root)


def planning_input_identity_digest(
    authored: dict[str, str],
    decisions: dict[str, dict[str, str | int]],
) -> str:
    """Bind the complete gate-input maps without duplicating them in every receipt."""

    payload = json.dumps(
        {
            "schema": "vfx-harness.exact-planning-input-identity/v1",
            "authored_inputs": authored,
            "decision_inputs": decisions,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return plan_bundle_integrity.digest(payload)


def exact_planning_input_identity_digest(root: Path) -> str:
    """Return the digest of the exact authored and full decision bytes at ``root``."""

    authored, decisions = exact_planning_input_identity(root)
    return planning_input_identity_digest(authored, decisions)


def require_exact_planning_input_identity(
    root: Path,
    *,
    expected_authored_inputs: Any,
    expected_decision_inputs: Any,
    where: str = "planning input snapshot",
) -> None:
    """Fail unless live planning inputs exactly equal one immutable gate snapshot."""

    observed_authored, observed_decisions = exact_planning_input_identity(root)
    if observed_authored != expected_authored_inputs:
        raise PlanPublicationError(f"{where} authored inputs changed")
    if observed_decisions != expected_decision_inputs:
        raise PlanPublicationError(f"{where} decision inputs changed")


def verify_decision_inputs(root: Path, expected: Any) -> None:
    if not isinstance(expected, dict):
        raise PlanPublicationError("plan bundle decision-input provenance must be an object")
    allowed = {path.as_posix() for path in DECISION_INPUT_PATHS}
    unsupported = sorted(name for name in expected if name not in allowed)
    if unsupported:
        raise PlanPublicationError(
            "plan bundle decision-input provenance names unsupported paths: "
            + ", ".join(unsupported)
        )
    live = decision_input_bytes(root)
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


def read_workspace_marker(anchor: Path, marker: Path) -> dict[str, Any]:
    return plan_bundle_integrity.read_schema_object(
        anchor,
        marker,
        "plan workspace marker",
        schema=WORKSPACE_SCHEMA,
        fields=WORKSPACE_FIELDS,
    )


def workspace_base_selection(record: dict[str, Any]) -> AuthoritySelectionToken:
    try:
        return AuthoritySelectionToken.from_dict(
            record.get("base_selection"),
            "plan workspace marker.base_selection",
        )
    except AuthoritySelectionConflict as exc:
        raise PlanPublicationError(str(exc)) from exc


def prepare_staging(layout: RunLayout) -> Path:
    """Create one run workspace bound to its exact starting authority selection."""

    workspace = layout.scratch / "plan-workspace"
    marker = workspace / ".plan-workspace.json"
    if workspace.exists() or workspace.is_symlink():
        record = read_workspace_marker(layout.scratch, marker)
        base_selection = workspace_base_selection(record)
        identity = {
            "schema": WORKSPACE_SCHEMA,
            "run_id": layout.run_id,
            "shot": str(layout.shot),
            "authored_inputs": authored_inputs(workspace),
            "decision_inputs": decision_inputs(workspace),
            "base_selection": base_selection.to_dict(),
        }
        if record != identity:
            raise PlanPublicationError(f"plan workspace ownership mismatch: {workspace}")
        return workspace

    # Delayed import avoids the plan-authority/selection resolver cycle. The resolver
    # supplies a fully verified plan/JIT pair, not just unchecked pointer bytes.
    from vfx_harness.orchestration.authority_selection import (  # noqa: PLC0415
        resolve_selected_authority,
    )

    base_selection = resolve_selected_authority(layout.shot).selection_token
    authored = authored_input_bytes(layout.shot)
    decisions = decision_input_bytes(layout.shot)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".plan-workspace.tmp-", dir=workspace.parent))
    try:
        (temp / "brief.md").write_bytes(authored["brief.md"])
        (temp / "refs").mkdir()
        for name, data in authored.items():
            if name == "brief.md":
                continue
            target = temp / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        for name, data in decisions.items():
            target = temp / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (temp / ".plan-workspace.json").write_text(
            json.dumps(
                {
                    "schema": WORKSPACE_SCHEMA,
                    "run_id": layout.run_id,
                    "shot": str(layout.shot),
                    "authored_inputs": authored_inputs(temp),
                    "decision_inputs": decision_inputs(temp),
                    "base_selection": base_selection.to_dict(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temp, workspace)
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return workspace
