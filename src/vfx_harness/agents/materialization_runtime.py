"""Production provider boundary for the native, fenced materialization session."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import flynn_agents_sdk as flynn
from flynn_agents_sdk import deepseek

from vfx_harness.agents import materialization_session
from vfx_harness.infrastructure.config import Settings
from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.orchestration.builder_execution_fence import (
    BuilderExecutionFenceLease,
    require_builder_execution_lease,
)


async def execute(
    *, layout: RunLayout, candidate: Path, charter: str, kickoff: str,
    model: str, blender: str, max_turns: int, fence_lease: BuilderExecutionFenceLease,
    overlay_root: Path | None = None,
) -> materialization_session.MaterializationResult:
    def check():
        require_builder_execution_lease(fence_lease, layout.shot)

    check()
    settings = Settings.from_environment(load_dotenv_file=False)
    if model != deepseek.VISION_MODEL:
        raise ValueError(f"native materialization requires model {deepseek.VISION_MODEL!r}; got {model!r}")
    if settings.run_max_usd is not None:
        raise ValueError("native materialization usage is unpriced; cannot enforce VFXH_RUN_MAX_USD")
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key.strip():
        raise ValueError("native materialization requires DEEPSEEK_API_KEY in the configured environment")
    limits = flynn.RunLimits(max_turns, max_turns, max_turns, settings.materialization_seconds,
                            output_tokens=settings.materialization_output_tokens)
    with fence_lease.operation(layout.shot):
        async with deepseek.DeepSeekAdapter(
            api_key=api_key, model=model, max_tokens=8192,
            timeout_seconds=settings.materialization_seconds,
        ) as adapter:
            return await materialization_session.execute(
                layout=layout, candidate=candidate, invocation=f"layer-{uuid4().hex}",
                context=(flynn.ContextItem("materialization-charter", charter, required=True),
                         flynn.ContextItem("layer-authority", kickoff, required=True)),
                inference=adapter, limits=limits, check_current=check,
                overlay_root=overlay_root, blender=blender,
            )
