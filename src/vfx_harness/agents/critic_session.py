"""Production critic inference through Flynn; VFX supplies current authority and claims."""

from __future__ import annotations

import os
from collections.abc import Callable

from flynn_agents_sdk import deepseek

from vfx_harness.agents import critic_transport
from vfx_harness.infrastructure.config import Settings
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import authority_selection, run_owner_boundary


async def execute(*, shot, milestone, phase, prompt, axes, frames, images, allow_na,
                  selected_authority, execution_guard, claims, check_inputs: Callable[[], None]):
    """Return a recorded opinion and the exact claim ids admitted by native proof checks.

    An unqualified observation is diagnostic only. Its caller must not promote scores
    into autonomous acceptance or repair. Failed native calls never use another engine.
    """
    def current():
        if execution_guard is not None:
            execution_guard.check(f"native {phase} critic for {execution_guard.label} at frame {milestone.frame}")
        else:
            layout = run_artifacts.active(shot.folder)
            if layout is None:
                raise ValueError("native critic requires an active owning run")
            run_owner_boundary.require_current_owner(layout)
        if authority_selection.resolve_selected_authority(shot.folder) != selected_authority:
            raise ValueError("critic selected authority changed; prepare a new owned judgment")
        check_inputs()

    current()
    settings = Settings.from_environment(load_dotenv_file=False)
    model = settings.critic_model
    if model != deepseek.VISION_MODEL:
        raise ValueError(f"native critic requires VFXH_CRITIC_MODEL={deepseek.VISION_MODEL!r}; got {model!r}")
    if settings.run_max_usd is not None:
        raise ValueError("native critic usage is unpriced; cannot enforce VFXH_RUN_MAX_USD")
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key.strip():
        raise ValueError("native critic requires DEEPSEEK_API_KEY")
    qualified = tuple(claim for claim in claims if claim.required
                      and claim.authority == "qualified_qualitative_required" and claim.qualification is not None)
    async with deepseek.DeepSeekAdapter(
        api_key=api_key, model=model, max_tokens=8192, timeout_seconds=critic_transport.LIMITS.wall_time_seconds,
    ) as adapter:
        return await critic_transport.execute(
            folder=shot.folder, scope_id=str(milestone.id), phase=phase,
            requested_provider="deepseek", requested_model=model, prompt=prompt,
            axes=tuple(axes), frames=tuple(frames), images=tuple(images), allow_na=allow_na,
            inference=adapter, check_current=current, qualification_claims=qualified,
        )
