"""Production provider boundary for exact, builder-claimed Flynn unit planning."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import flynn_agents_sdk as flynn
from flynn_agents_sdk import deepseek

from vfx_harness.agents import unit_planning_session
from vfx_harness.infrastructure.config import Settings
from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.orchestration.builder_execution_fence import (
    BuilderExecutionFenceLease,
    require_builder_execution_lease,
)
from vfx_harness.orchestration.work_unit_plan_transaction import WorkUnitPlanTransaction

if TYPE_CHECKING:
    from vfx_harness.agents.builder.attempt_guard import UnitAttemptGuard


async def execute(
    *, layout: RunLayout, target: Path, charter: str, kickoff: str,
    model: str, blender: str, max_turns: int, fence_lease: BuilderExecutionFenceLease,
    attempt_guard: UnitAttemptGuard, transaction: WorkUnitPlanTransaction,
) -> unit_planning_session.UnitPlanningResult:
    def check():
        require_builder_execution_lease(fence_lease, layout.shot)
        if attempt_guard.folder.resolve() != layout.shot.resolve() or attempt_guard.claim.phase != "planning":
            raise ValueError("native unit planning requires this shot's exact planning claim")
        attempt_guard.check("native unit planning")

    check()
    settings = Settings.from_environment(load_dotenv_file=False)
    if model != deepseek.VISION_MODEL:
        raise ValueError(f"native unit planning requires model {deepseek.VISION_MODEL!r}; got {model!r}")
    if settings.run_max_usd is not None:
        raise ValueError("native unit planning usage is unpriced; cannot enforce VFXH_RUN_MAX_USD")
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key.strip():
        raise ValueError("native unit planning requires DEEPSEEK_API_KEY in the configured environment")
    limits = flynn.RunLimits(max_turns, max_turns, max_turns, settings.unit_plan_seconds,
                            output_tokens=settings.unit_plan_output_tokens)
    with fence_lease.operation(layout.shot):
        async with deepseek.DeepSeekAdapter(api_key=api_key, model=model, max_tokens=8192,
                                            timeout_seconds=settings.unit_plan_seconds) as adapter:
            return await unit_planning_session.execute(
                layout=layout, target=target, invocation=f"unit-{uuid4().hex}",
                layer_id=attempt_guard.layer_id, unit_id=attempt_guard.unit.id,
                selected_authority=attempt_guard.selected_authority, transaction=transaction,
                context=(flynn.ContextItem("unit-charter", charter, required=True),
                         flynn.ContextItem("unit-authority", kickoff, required=True)),
                inference=adapter, limits=limits, check_current=check,
                hold_current=lambda: attempt_guard.hold("publish native unit plan"), blender=blender,
            )
