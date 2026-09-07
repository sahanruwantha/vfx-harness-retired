"""Run global planning in the process holding the driver's live root-owner lease."""

from __future__ import annotations

import anyio

from vfx_harness.agents import planner
from vfx_harness.agents.planner.types import PlanGateFailure
from vfx_harness.infrastructure.config import Settings
from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.orchestration import run_owner_boundary


def run(layout: RunLayout) -> int:
    run_owner_boundary.require_current_owner(layout)
    settings = Settings.from_environment(load_dotenv_file=False)
    try:
        # The inherited boundary publishes typed stops; only the driver terminalizes.
        with run_owner_boundary.invocation(layout.shot, "plan"):
            result = anyio.run(lambda: planner.generate_plan_until_clean(
                layout.shot, max_turns=settings.plan_max_turns,
            ))
            run_owner_boundary.require_current_owner(layout)
            if not result.clean:
                raise PlanGateFailure(result)
    except PlanGateFailure as exc:
        return int(exc.code)
    return 0
