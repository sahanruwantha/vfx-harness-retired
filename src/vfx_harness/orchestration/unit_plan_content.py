"""Content publication for a harness-selected work-unit plan.

This writer stamps input integrity only. The owning planning transaction must still
validate the plan, run its terminal gate and decide publication or rollback.
"""

from __future__ import annotations

from pathlib import Path

from vfx_harness.observability import provenance
from vfx_harness.orchestration import layer_plans


def publish_unit_plan_content(
    shot_folder: str | Path,
    target_path: str | Path,
    content: str,
    *,
    selected_authority=None,
) -> tuple[Path, int]:
    """Write content and its integrity stamp to one harness-selected unit-plan target.

    These are separate file publications; the outer work-unit plan transaction owns
    rollback of the pair. The bundle-pinned integrity sidecar is stamped so the
    consumer view admits the draft: a session's own gate_preview reported every fresh
    unit plan as absent while only the post-session stamp made it visible (run
    20260902T165518Z-004470). Gate attestation still comes only from the terminal gate.
    """
    root = Path(shot_folder).resolve()
    target = Path(target_path).resolve()
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError("target is not under the active shot") from exc
    if relative.parts[:1] != ("plans",):
        raise ValueError("target is not under the shot plans directory")
    if len(content.strip()) < 200:
        raise ValueError("unit plan must contain at least 200 non-whitespace characters")
    lines = content.count("\n") + 1
    if lines > 160:
        raise ValueError(
            f"unit plan has {lines} lines; maximum is 160 — keep evidence in "
            "machine contracts and publish only the execution index"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    provenance.atomic_write(target, content.rstrip() + "\n")
    layer_plans.stamp_work_unit_plan(root, target, selected_authority=selected_authority)
    return target, lines
