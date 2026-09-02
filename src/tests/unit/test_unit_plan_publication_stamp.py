"""publish_unit_plan stamps the bundle-pinned integrity sidecar it publishes (HIR-0174).

The consumer view admits a JIT plan only through that sidecar, so a session's own
gate_preview could never see the plan it had just published: every fresh unit plan was
reported absent with a retired remedy (run 20260902T165518Z-004470). Gate attestation
still belongs to the terminal gate alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration.test_judgment_debt_public_pipeline import _public_fixture_root
from vfx_harness.agents.plan_tools import _publish_unit_plan_content
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.authority_selection import resolve_selected_authority
from vfx_harness.orchestration.layer_plans import (
    UNIT_PLAN_AUTHORITY_SCHEMA,
    validate_work_unit_plan_authority,
    work_unit_plan_authority_path,
)
from vfx_harness.orchestration.plan_authority import publish_current

_CONTENT = "# Bounded unit plan\n\n" + ("one atomic execution ticket per line\n" * 12)


def _selected_shot(tmp_path: Path) -> Path:
    root = _public_fixture_root(tmp_path)
    publish_current(
        root,
        run_artifacts.create(root, "unit-plan-stamp-fixture"),
        outcome="clean_with_deferred",
    )
    return root


def test_publication_stamps_integrity_without_gate_attestation(tmp_path: Path) -> None:
    root = _selected_shot(tmp_path)
    selected = resolve_selected_authority(root)
    target = root / "plans" / "units" / "aim_target.md"

    published, lines = _publish_unit_plan_content(
        root, target, _CONTENT, selected_authority=selected
    )

    assert published == target
    assert lines == _CONTENT.count("\n") + 1
    sidecar = work_unit_plan_authority_path(target)
    record = json.loads(sidecar.read_text(encoding="utf-8"))
    assert record["schema"] == UNIT_PLAN_AUTHORITY_SCHEMA
    assert record["bundle_hash"] == selected.plan.bundle.content_hash
    assert record["path"] == "plans/units/aim_target.md"
    assert "gate_clean" not in record
    # Exactly the admission check the consumer-view projection applies before the gate.
    validate_work_unit_plan_authority(
        root, target, require_gate=False, selected_authority=selected
    )
    with pytest.raises(ValueError, match="clean-gate attestation"):
        validate_work_unit_plan_authority(
            root, target, require_gate=True, selected_authority=selected
        )


def test_republication_restamps_the_new_bytes(tmp_path: Path) -> None:
    root = _selected_shot(tmp_path)
    selected = resolve_selected_authority(root)
    target = root / "plans" / "units" / "aim_target.md"
    _publish_unit_plan_content(root, target, _CONTENT, selected_authority=selected)
    first = json.loads(work_unit_plan_authority_path(target).read_text(encoding="utf-8"))

    _publish_unit_plan_content(
        root, target, _CONTENT + "one more ticket\n", selected_authority=selected
    )

    second = json.loads(work_unit_plan_authority_path(target).read_text(encoding="utf-8"))
    assert second["plan_sha256"] != first["plan_sha256"]
    validate_work_unit_plan_authority(
        root, target, require_gate=False, selected_authority=selected
    )


def test_publication_without_selected_authority_pins_to_the_current_pointer(
    tmp_path: Path,
) -> None:
    root = _selected_shot(tmp_path)
    target = root / "plans" / "units" / "aim_target.md"

    _publish_unit_plan_content(root, target, _CONTENT)

    record = json.loads(work_unit_plan_authority_path(target).read_text(encoding="utf-8"))
    assert record["bundle_hash"] == resolve_selected_authority(root).plan.bundle.content_hash
