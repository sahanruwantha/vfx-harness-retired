"""Production unit routing by typed evidence needs, with no failure fallback."""

from __future__ import annotations

import os

import flynn_agents_sdk as flynn
from flynn_agents_sdk import deepseek

from vfx_harness.agents.builder import evidence, flynn_unit, unit_loop
from vfx_harness.infrastructure.config import Settings
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.builder_execution_fence import require_builder_execution_lease


async def build_unit(shot, m, script_rel, prior_paths, session, *, fence_lease=None, **kwargs):
    """Use Flynn for procedural executable units; other evidence routes remain explicit.

    Provider, budget, authority and replay failures propagate from the selected engine.
    This function never retries through the other engine or authorizes completion.
    """
    require_builder_execution_lease(fence_lease, shot.folder)
    with fence_lease.operation(shot.folder):
        unit = kwargs.get("active_unit")
        guard = kwargs.get("attempt_guard")
        selected = kwargs.get("selected_authority")
        if unit is None or guard is None or selected is None:
            raise ValueError("production builder requires an exact unit, claim and selected authority")
        if guard.folder.resolve() != shot.folder.resolve() or guard.claim.phase != "building":
            raise ValueError("production builder requires this shot's building claim")
        guard.require_unit_boundary(m, unit, layer=kwargs.get("layer"), script_rel=script_rel)
        guard.check("select production unit engine")
        if selected != guard.selected_authority:
            raise ValueError("production builder selected authority must match the exact claim")
        if kwargs.get("resume_ok"):
            raise ValueError("builder resume requires a complete VFX receipt; start a reviewed new attempt")
        if unit.construction.route != "procedural" or evidence._unit_requires_raster(
            shot, unit, selected_authority=selected,
        ):
            return await unit_loop.build_unit(
                shot, m, script_rel, prior_paths, session, fence_lease=fence_lease, **kwargs,
            )
        layout = run_artifacts.active(shot.folder)
        if layout is None or layout.run_id != guard.claim.run_id:
            raise ValueError("native builder requires the exact claim's active run")
        settings = Settings.from_environment(load_dotenv_file=False)
        if settings.executable_builder_model != deepseek.VISION_MODEL:
            raise ValueError(f"native executable builder requires model {deepseek.VISION_MODEL!r}; "
                             f"got {settings.executable_builder_model!r}")
        if settings.run_max_usd is not None:
            raise ValueError("native executable builder usage is unpriced; cannot enforce VFXH_RUN_MAX_USD")
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key.strip():
            raise ValueError("native executable builder requires DEEPSEEK_API_KEY in the configured environment")
        steps = settings.executable_builder_max_steps
        limits = flynn.RunLimits(steps, steps, steps, settings.executable_builder_seconds,
                                output_tokens=settings.executable_builder_output_tokens)
        async with deepseek.DeepSeekAdapter(
            api_key=api_key, model=settings.executable_builder_model, max_tokens=8192,
            timeout_seconds=settings.executable_builder_seconds,
        ) as adapter:
            return await flynn_unit.build_unit(
                shot, m, script_rel, prior_paths, session, inference=adapter, limits=limits,
                fence_lease=fence_lease, **kwargs,
            )
