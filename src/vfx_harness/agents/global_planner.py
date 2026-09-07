"""Production global planning through Flynn, under the live VFX root owner."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal
from uuid import uuid4

import flynn_agents_sdk as flynn
from flynn_agents_sdk import deepseek

from vfx_harness.agents import global_planning_session
from vfx_harness.agents.global_planning_policy import PLANNER_SYSTEM, frame_contract
from vfx_harness.agents.planning_workspace import PlanningWorkspace
from vfx_harness.domain.brief import load_shot
from vfx_harness.infrastructure.config import Settings
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.console import log
from vfx_harness.orchestration import plan_authoring, plan_inputs, run_owner_boundary
from vfx_harness.orchestration.plan_bundle_integrity import digest, read_real_file


def snapshot_candidate(layout: run_artifacts.RunLayout) -> Path:
    """Archive the complete draft outside the gate workspace, bound to its inputs."""
    binding = PlanningWorkspace(layout, lambda: run_owner_boundary.require_current_owner(layout))
    binding.require_closed_tree()
    if any(value is None for value in binding.owned.values()):
        raise ValueError("cannot snapshot an incomplete global planning candidate")
    sources = {
        name: read_real_file(layout.shot, binding.root / name, "global draft snapshot").decode("utf-8")
        for name in binding.owned
    }
    binding.check()
    return layout.write_report(f"global-plan-input-{uuid4().hex}", {
        "schema": "vfx-harness.global-plan-input/v1", **binding.identity(), "sources": sources,
    })


async def generate_plan(
    folder: str | Path,
    *,
    model: str | None = None,
    max_turns: int = 12,
    tag: str | None = None,
    role: Literal["draft", "verify", "repair"] = "draft",
    phase_input: Path | None = None,
    repair_feedback: str | None = None,
    workspace: str | Path | None = None,
) -> Path:
    """Execute one native role. A sweep result never selects plan authority."""
    shot = load_shot(folder)
    layout = run_artifacts.active(shot.folder)
    if layout is None:
        raise ValueError("global planning requires an active public VFX invocation")
    run_owner_boundary.require_current_owner(layout)
    settings = Settings.from_environment(load_dotenv_file=False)
    model = model or settings.global_planner_model
    if model != deepseek.VISION_MODEL:
        raise ValueError(f"global planning requires VFXH_GLOBAL_PLANNER_MODEL={deepseek.VISION_MODEL}; got {model!r}")
    if settings.run_max_usd is not None:
        raise ValueError("global planning usage is unpriced; cannot enforce VFXH_RUN_MAX_USD for this role")
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key.strip():
        raise ValueError("global planning requires DEEPSEEK_API_KEY in the configured environment")
    if type(max_turns) is not int or max_turns < 1:
        raise ValueError("global planning max_turns must be a positive integer")
    if role not in {"draft", "verify", "repair"}:
        raise ValueError("global planning role must be draft, verify or repair")
    if (role == "draft") != (phase_input is None):
        raise ValueError("verify/repair require an exact phase snapshot; draft accepts no prior snapshot")
    if (role == "repair") != (repair_feedback is not None):
        raise ValueError("repair requires explicit gate feedback; other roles accept none")
    root = layout.scratch / "plan-workspace"
    if workspace is None:
        plan_inputs.prepare_staging(layout)
    elif Path(workspace).resolve() != root:
        raise ValueError("global planning accepts only this run's exact planning workspace")
    snapshot_bytes = None

    def check():
        run_owner_boundary.require_current_owner(layout)
        if snapshot_bytes is not None and (
            read_real_file(layout.shot, phase_input, "global planning phase input") != snapshot_bytes
        ):
            raise ValueError("global planning phase snapshot changed; stop the attempt")

    binding = PlanningWorkspace(layout, check)
    items = [
        flynn.ContextItem("global-charter", PLANNER_SYSTEM, required=True),
        flynn.ContextItem("authored-brief", read_real_file(
            layout.shot, root / "brief.md", "global planning brief",
        ).decode("utf-8"), required=True),
        flynn.ContextItem("clause-registry", plan_authoring.registry_prompt_block(
            plan_authoring.clause_registry(root / "brief.md"),
        ), required=True),
        flynn.ContextItem("shot-frame-contract", frame_contract(shot), required=True),
    ]
    for name in sorted(binding.record["decision_inputs"]):
        items.append(flynn.ContextItem(f"decision:{name}", read_real_file(
            layout.shot, root / name, "global planning decision",
        ).decode("utf-8"), required=True))
    if phase_input is not None:
        phase_input = Path(phase_input)
        if phase_input.parent != layout.reports:
            raise ValueError("global planning phase input must be an exact run-owned report")
        snapshot_bytes = read_real_file(layout.shot, phase_input, "global planning phase input")
        snapshot = json.loads(snapshot_bytes)
        if (not isinstance(snapshot, dict) or set(snapshot) != {"schema", "sources", *binding.identity()}
                or snapshot["schema"] != "vfx-harness.global-plan-input/v1"):
            raise ValueError("unsupported global planning phase snapshot schema")
        if {key: snapshot.get(key) for key in binding.identity()} != binding.identity():
            raise ValueError("global planning phase snapshot does not bind the current draft")
        sources = snapshot.get("sources")
        if not isinstance(sources, dict) or set(sources) != set(binding.owned):
            raise ValueError("global planning phase snapshot requires the complete draft source set")
        if any(not isinstance(text, str) or digest(text.encode()) != binding.owned[name]
               for name, text in sources.items()):
            raise ValueError("global planning phase snapshot source bytes differ from the current draft")
        items.extend((
            flynn.ContextItem("phase-input", json.dumps({
                "role": role, "report": str(phase_input.relative_to(layout.root)),
                "sha256": digest(snapshot_bytes), "artifacts": snapshot["artifacts"],
            }), required=True),
            flynn.ContextItem("baseline-mapping", sources["ownership_mapping.json"], required=True),
            flynn.ContextItem("phase-charter", (
                "Audit owner defensibility, causal dependencies, namespace separation, judge frames, and genuine "
                "client blockers. Preserve settled decisions verbatim. Submit the audited complete mapping even "
                "when unchanged, then gate it. Do not design future units or weaken intent to silence findings."
                if role == "verify" else
                "Repair the mapping fields responsible for the supplied gate findings. Preserve other ownership "
                "and settled intent. Sweep sibling instances of the same structural defect, then gate the mapping."
            ), required=True),
        ))
    if repair_feedback is not None:
        items.append(flynn.ContextItem("repair-findings", repair_feedback, required=True))
    binding.check()
    invocation = f"{role}-{uuid4().hex}"
    log(f"global planner: {role}, model {model}, at most {max_turns} steps" + (f", label {tag!r}" if tag else ""))
    async with deepseek.DeepSeekAdapter(
        api_key=api_key, model=model, max_tokens=8192, timeout_seconds=settings.global_plan_seconds,
    ) as adapter:
        await global_planning_session.execute(
            layout=layout, invocation=invocation, role=role, context=tuple(items), inference=adapter,
            limits=flynn.RunLimits(max_turns, max_turns, max_turns, settings.global_plan_seconds,
                                   output_tokens=settings.global_plan_output_tokens), check_current=check,
        )
    check()
    return root / "plans/global.md"
